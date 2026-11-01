"""Scenario v2 loading, validation and normalised output.

Every error is a ``ValueError`` whose message starts with the RFC 6901 JSON pointer of the
offending value (``/time/step_s=0.0 must be positive``). Errors about the file rather than the
document (unreadable, not UTF-8, not JSON, too large) start with the file path instead.

Bounds are not repeated here: each value is checked against the metadata of the dataclass
field it fills (``openleo.scenario.model``), which is also what the JSON Schema is generated
from. Parsing reads no catalog; ``load_catalogs`` does, and checks hashes and NORAD ids.
"""

# Every malformed document is a ValueError (design spec section 3.2), including a wrong JSON type.
# ruff: noqa: TRY004

import copy
import dataclasses
import functools
import json
import shlex
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from json import JSONDecodeError
from math import isclose, isfinite
from os.path import relpath
from pathlib import Path
from typing import Any, Literal, get_args, get_origin, get_type_hints

from openleo._validation import (
    _field,
    _finite_number,
    _object,
    _read_scenario_bytes,
    _safe_repr,
    _sha256,
    _string,
    _url,
    _utc,
)
from openleo.physics.gases import MAX_FREQUENCY_HZ, MIN_FREQUENCY_HZ
from openleo.physics.hydrometeor_path import (
    HydrometeorConfig,
    HydrometeorStation,
    hydrometeor_document,
)
from openleo.scenario.model import (
    NOT_DOCUMENT_FIELDS,
    Adaptation,
    CatalogSource,
    ConvergenceSettings,
    EventSettings,
    GroundSegment,
    IslConfig,
    NetworkConfig,
    OutputConfig,
    Propagation,
    RadioConfig,
    RoutePair,
    Scenario,
    SpaceSegment,
    Station,
    Terminal,
    TimeConfig,
)

SCENARIO_SCHEMA_ID = "openleo.scenario/2"
MAX_WINDOW = timedelta(days=31)
MAX_GRID_SAMPLES = 1_000_000
ITU_MINIMUM_ELEVATION_DEG = 5.0
HYDROMETEOR_FREQUENCY_HZ = (1e9, 200e9)
HYDROMETEOR_MODEL = "declared_uniform_layers"
# Bounds of the declared state of one station: those of HydrometeorStation, restated here so the
# parser can name the offending field (the physics messages carry no pointer). A test holds this
# table to the kernel, and the JSON Schema generator reads it too.
HYDROMETEOR_STATE_BOUNDS: dict[str, dict[str, float]] = {
    "liquid_water_kg_m2": {"minimum": 0.0},
    "rain_rate_mm_h": {"minimum": 0.0},
    "rain_top_height_amsl_m": {"minimum": 0.0, "maximum": 20_000.0},
    "polarization_tilt_deg": {"minimum": 0.0, "maximum": 180.0},
}
# Top-level keys of a v1 scenario or constellation file (v1 files carry no schema key).
_V1_KEYS = frozenset(("orbit", "ground_station", "stations", "time_window", "radio_link"))


def load_scenario(path: str | Path) -> Scenario:
    """Read a bounded UTF-8 scenario file, rejecting duplicate keys, and parse it."""
    scenario_path = Path(path)
    raw, digest = _load_document(scenario_path)
    return parse_scenario(raw, scenario_path, digest)


def _load_document(path: Path) -> tuple[Any, str]:
    """A bounded UTF-8 JSON file without repeated keys: (the document, SHA-256 of its bytes)."""
    try:
        source = _read_scenario_bytes(path)
    except OSError as exc:
        raise ValueError(f"{path}: could not load scenario: {exc}") from exc
    try:
        text = source.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{path}: scenario JSON must be UTF-8") from exc
    try:
        raw = json.loads(text, object_pairs_hook=_checked_pairs)
    except JSONDecodeError as exc:
        raise ValueError(f"{path}: invalid JSON: {exc.msg}") from exc
    except RecursionError as exc:
        raise ValueError(f"{path}: invalid JSON: nested too deeply") from exc
    except ValueError as exc:  # for example an integer beyond Python's digit limit
        raise ValueError(f"{path}: invalid JSON: {exc}") from exc
    _reject_repeated_keys(raw)
    return raw, sha256(source).hexdigest()


def parse_scenario(raw: Any, path: str | Path, source_sha256: str) -> Scenario:
    """Validate a parsed document; catalog paths resolve against ``path``'s directory."""
    scenario_path = Path(path).resolve()
    _sha256(source_sha256, "source_sha256")
    root = _root(raw, path)

    name = root.string("name")
    description = root.text("description")
    time = _time(root.object("time"))
    space_segment = _space_segment(root.object("space_segment"), scenario_path.parent)
    terminals = _terminals(root)
    radio_section = root.object("radio")
    radio = _radio(radio_section)
    adaptation = _adaptation(root.object("adaptation"))
    if radio.channel_bandwidth_hz < adaptation.symbol_rate_baud * (1 + adaptation.rolloff):
        raise ValueError(
            f"{radio_section.pointer_of('channel_bandwidth_hz')}="
            f"{_safe_repr(radio.channel_bandwidth_hz)} must be at least "
            "adaptation.symbol_rate_baud * (1 + adaptation.rolloff)"
        )
    propagation_section = root.object("propagation")
    model = propagation_section.choice("model")
    hydrometeors = propagation_section.value("hydrometeors")
    _check_frequency(radio, model, hydrometeors is not None)
    refinement = propagation_section.integer("refinement")
    ground_segment = _ground_segment(root.object("ground_segment"), terminals, model)
    stations = {station.name: station for station in ground_segment.stations}
    propagation = Propagation(
        model,
        refinement,
        _hydrometeors(
            hydrometeors, propagation_section.pointer_of("hydrometeors"), model, stations
        ),
    )
    network = _network(root.object("network", nullable=True), stations)
    outputs = _outputs(root.object("outputs"))
    return Scenario(
        name=name,
        description=description,
        time=time,
        space_segment=space_segment,
        terminals=terminals,
        ground_segment=ground_segment,
        radio=radio,
        adaptation=adaptation,
        propagation=propagation,
        network=network,
        outputs=outputs,
        source_path=scenario_path,
        source_sha256=source_sha256,
    )


def scenario_document(scenario: Scenario) -> dict[str, Any]:
    """The normalised document: every default explicit, catalog paths relative and POSIX.

    ``parse_scenario(scenario_document(s), s.source_path, s.source_sha256) == s``.
    """
    directory = scenario.source_path.parent
    network = scenario.network
    return {
        "schema": SCENARIO_SCHEMA_ID,
        "name": scenario.name,
        "description": scenario.description,
        "time": {
            "start_utc": _utc_text(scenario.time.start_utc),
            "stop_utc": _utc_text(scenario.time.stop_utc),
            "step_s": scenario.time.step_s,
            "events": dataclasses.asdict(scenario.time.events),
            "convergence": dataclasses.asdict(scenario.time.convergence),
        },
        "space_segment": {
            "catalogs": [
                {
                    "path": _relative_posix(catalog.path, directory),
                    "format": catalog.format,
                    "source_url": catalog.source_url,
                    "retrieved_at_utc": _utc_text(catalog.retrieved_at_utc),
                    "terms_url": catalog.terms_url,
                    "sha256": catalog.sha256,
                }
                for catalog in scenario.space_segment.catalogs
            ],
            "select_norad_ids": None
            if scenario.space_segment.select_norad_ids is None
            else list(scenario.space_segment.select_norad_ids),
            "synthetic_rf": scenario.space_segment.synthetic_rf,
        },
        "terminals": {name: dataclasses.asdict(terminal) for name, terminal in scenario.terminals},
        "ground_segment": {
            "default_minimum_elevation_deg": scenario.ground_segment.default_minimum_elevation_deg,
            "stations": [
                dataclasses.asdict(station) for station in scenario.ground_segment.stations
            ],
        },
        "radio": dataclasses.asdict(scenario.radio),
        "adaptation": dataclasses.asdict(scenario.adaptation),
        "propagation": {
            "model": scenario.propagation.model,
            "refinement": scenario.propagation.refinement,
            "hydrometeors": None
            if scenario.propagation.hydrometeors is None
            else hydrometeor_document(scenario.propagation.hydrometeors),
        },
        "network": None
        if network is None
        else {
            "pairs": [dataclasses.asdict(pair) for pair in network.pairs],
            "isl": dataclasses.asdict(network.isl),
            "fixed_capacity_bps": network.fixed_capacity_bps,
            "route_models": list(network.route_models),
        },
        "outputs": {
            "formats": list(scenario.outputs.formats),
            "satellite_positions": scenario.outputs.satellite_positions,
        },
    }


# --- sections ---------------------------------------------------------------------------------


def _root(raw: Any, path: str | Path) -> "_Section":
    if not isinstance(raw, Mapping):
        raise ValueError("/ must be a JSON object")
    if "schema" not in raw:
        if _V1_KEYS & set(raw):
            raise ValueError(_upgrade_message(path))
        raise ValueError("/schema is required")
    schema = raw["schema"]
    if schema == "openleo.scenario/1":
        raise ValueError(_upgrade_message(path))
    if schema != SCENARIO_SCHEMA_ID:
        raise ValueError(f"/schema={_safe_repr(schema)} must be exactly {SCENARIO_SCHEMA_ID!r}")
    return _Section(raw, "", Scenario, extra=("schema",))


def _upgrade_message(path: str | Path) -> str:
    return (
        "/schema: This is an OpenLEO 1.x scenario; "
        f"run `openleo scenario upgrade {shlex.quote(str(path))}`"
    )


def _time(section: "_Section") -> TimeConfig:
    start = section.timestamp("start_utc")
    stop = section.timestamp("stop_utc")
    step_s = section.number("step_s")
    events = _events(section.object("events"))
    convergence = _convergence(section.object("convergence"))
    window = stop - start
    if window <= timedelta(0):
        raise ValueError(
            f"{section.pointer_of('stop_utc')} must be after {section.pointer_of('start_utc')}"
        )
    if window > MAX_WINDOW:
        raise ValueError(
            f"{section.pointer_of('stop_utc')}: the window of {window} exceeds the "
            f"{MAX_WINDOW.days}-day limit"
        )
    step_pointer = section.pointer_of("step_s")
    if step_s > window.total_seconds():
        raise ValueError(
            f"{step_pointer}={_safe_repr(step_s)} must be no larger than the window "
            f"duration {window.total_seconds()}"
        )
    step = timedelta(seconds=step_s)  # no overflow: step_s is at most 31 days
    if step <= timedelta(0) or not isclose(
        step.total_seconds(), step_s, rel_tol=0.0, abs_tol=1e-12
    ):
        raise ValueError(
            f"{step_pointer}={_safe_repr(step_s)} must be exactly representable at "
            "microsecond resolution"
        )
    quotient, remainder = divmod(window, step)
    samples = quotient + 1 + bool(remainder)
    if samples > MAX_GRID_SAMPLES:
        raise ValueError(
            f"{step_pointer}={_safe_repr(step_s)} produces {samples} samples including the "
            f"stop point; the limit is {MAX_GRID_SAMPLES}"
        )
    return TimeConfig(start, stop, step_s, events, convergence)


def _events(section: "_Section") -> EventSettings:
    return EventSettings(section.boolean("enabled"), section.number("tolerance_s"))


def _convergence(section: "_Section") -> ConvergenceSettings:
    return ConvergenceSettings(
        section.choice("mode"),
        section.integer("refinement_factor"),
        section.number("relative_tolerance"),
        section.number("absolute_tolerance_bits"),
    )


def _space_segment(section: "_Section", directory: Path) -> SpaceSegment:
    return SpaceSegment(
        catalogs=section.array("catalogs", lambda pointer, raw: _catalog(raw, pointer, directory)),
        select_norad_ids=section.array(
            "select_norad_ids",
            lambda pointer, raw: _integer(raw, pointer, section.meta("select_norad_ids")["items"]),
        ),
        synthetic_rf=section.boolean("synthetic_rf"),
    )


def _catalog(raw: Any, pointer: str, directory: Path) -> CatalogSource:
    section = _Section(raw, pointer, CatalogSource)
    return CatalogSource(
        path=_resolve_catalog_path(directory, section.string("path"), section.pointer_of("path")),
        format=section.choice("format"),
        source_url=section.url("source_url"),
        retrieved_at_utc=section.timestamp("retrieved_at_utc"),
        terms_url=section.url("terms_url"),
        sha256=section.sha256("sha256"),
    )


def _resolve_catalog_path(directory: Path, value: str, pointer: str) -> Path:
    try:
        return (directory / value).resolve()
    except (OSError, RuntimeError, ValueError) as exc:  # a NUL, a lone surrogate, a symlink loop
        raise ValueError(
            f"{pointer}={_safe_repr(value)} cannot be resolved as a file path: {exc}"
        ) from exc


def _terminals(root: "_Section") -> tuple[tuple[str, Terminal], ...]:
    pointer = root.pointer_of("terminals")
    raw = root.value("terminals")
    if not isinstance(raw, Mapping):
        raise ValueError(f"{pointer} must be a JSON object")
    terminals = []
    for key, value in raw.items():
        entry = _field(pointer, key)
        _string(key, entry)
        section = _Section(value, entry, Terminal)
        terminals.append(
            (
                key,
                Terminal(
                    section.number("receive_gain_dbi"),
                    section.number("system_noise_temperature_k"),
                    section.number("miscellaneous_loss_db"),
                ),
            )
        )
    return tuple(terminals)


def _radio(section: "_Section") -> RadioConfig:
    return RadioConfig(
        section.number("carrier_frequency_hz"),
        section.number("channel_bandwidth_hz"),
        section.number("eirp_dbw"),
    )


def _adaptation(section: "_Section") -> Adaptation:
    adaptation = Adaptation(
        section.choice("standard"),
        section.choice("table"),
        section.number("symbol_rate_baud"),
        section.number("rolloff"),
        section.number("implementation_margin_db"),
        section.number("hysteresis_db"),
        section.number("feedback_delay_s"),
    )
    try:
        adaptation.kernel()
    except ValueError as exc:  # every other bound was checked above; this is the overflow guard
        raise ValueError(f"{section.pointer_of('symbol_rate_baud')}: {exc}") from exc
    return adaptation


def _check_frequency(radio: RadioConfig, model: str, with_hydrometeors: bool) -> None:
    pointer = _field("/radio", "carrier_frequency_hz")
    frequency = radio.carrier_frequency_hz
    domains = []
    if model == "itu_reference":
        domains.append((MIN_FREQUENCY_HZ, MAX_FREQUENCY_HZ, "propagation.model itu_reference"))
    if with_hydrometeors:
        domains.append((*HYDROMETEOR_FREQUENCY_HZ, "propagation.hydrometeors"))
    for lower, upper, reason in domains:
        if not lower <= frequency <= upper:
            raise ValueError(
                f"{pointer}={_safe_repr(frequency)} must be in [{lower!r}, {upper!r}] Hz for {reason}"
            )


def _ground_segment(
    section: "_Section", terminals: tuple[tuple[str, Terminal], ...], model: str
) -> GroundSegment:
    default_mask = section.number("default_minimum_elevation_deg")
    default_pointer = section.pointer_of("default_minimum_elevation_deg")
    terminal_names = {name for name, _ in terminals}
    names: set[str] = set()

    def station(pointer: str, raw: Any) -> Station:
        item = _Section(raw, pointer, Station)
        name = item.string("name")
        if "/" in name:
            raise ValueError(
                f"{item.pointer_of('name')}={_safe_repr(name)} must not contain '/': metric ids "
                "use it as a separator"
            )
        if name in names:
            raise ValueError(
                f"{item.pointer_of('name')}={_safe_repr(name)} duplicates a station name"
            )
        names.add(name)
        latitude = item.number("latitude_deg")
        longitude = item.number("longitude_deg")
        height_ellipsoid = item.number("height_ellipsoid_m")
        height_amsl = item.number("height_amsl_m")
        terminal = item.string("terminal")
        if terminal not in terminal_names:
            raise ValueError(
                f"{item.pointer_of('terminal')}={_safe_repr(terminal)} is not a name in /terminals"
            )
        mask = item.number("minimum_elevation_deg")
        mask_pointer = item.pointer_of("minimum_elevation_deg")
        if mask is None:
            mask, mask_pointer = default_mask, default_pointer
        if model == "itu_reference":
            if height_amsl is None:
                raise ValueError(
                    f"{item.pointer_of('height_amsl_m')} is required when propagation.model "
                    "is itu_reference"
                )
            if mask < ITU_MINIMUM_ELEVATION_DEG:
                raise ValueError(
                    f"{mask_pointer}={_safe_repr(mask)} must be at least "
                    f"{ITU_MINIMUM_ELEVATION_DEG} when propagation.model is itu_reference"
                )
        return Station(name, latitude, longitude, height_ellipsoid, height_amsl, terminal, mask)

    return GroundSegment(default_mask, section.array("stations", station))


def _hydrometeors(
    raw: Any, pointer: str, model: str, stations: Mapping[str, Station]
) -> HydrometeorConfig | None:
    if raw is None:
        return None
    if model != "itu_reference":
        raise ValueError(f"{pointer} requires propagation.model itu_reference")
    if not isinstance(raw, Mapping):
        raise ValueError(f"{pointer} must be a JSON object")
    _object(raw, frozenset(("model", "stations")), pointer)
    _choice(raw["model"], _field(pointer, "model"), (HYDROMETEOR_MODEL,))
    states_pointer = _field(pointer, "stations")
    declared = raw["stations"]
    if not isinstance(declared, Mapping):
        raise ValueError(f"{states_pointer} must be a JSON object")
    missing = [name for name in stations if name not in declared]
    unknown = [name for name in declared if name not in stations]
    if missing or unknown:
        raise ValueError(
            f"{states_pointer} must name every ground station exactly once "
            f"(missing: {_safe_repr(missing)}, not a ground station: {_safe_repr(unknown)})"
        )
    states = []
    for name, station in stations.items():  # in ground-station order, as v1 stores them
        state_pointer = _field(states_pointer, name)
        state = _hydrometeor_state(declared[name], state_pointer)
        if state.rain_top_height_amsl_m < station.height_amsl_m:
            raise ValueError(
                f"{_field(state_pointer, 'rain_top_height_amsl_m')}="
                f"{_safe_repr(state.rain_top_height_amsl_m)} must be at least the "
                f"station's height_amsl_m {station.height_amsl_m!r}"
            )
        states.append((name, state))
    return HydrometeorConfig(HYDROMETEOR_MODEL, tuple(states))


def _hydrometeor_state(raw: Any, pointer: str) -> HydrometeorStation:
    """One station's declared state, every error carrying the station's pointer."""
    if not isinstance(raw, Mapping):
        raise ValueError(f"{pointer} must be a JSON object")
    _object(raw, frozenset(HYDROMETEOR_STATE_BOUNDS), pointer)
    return HydrometeorStation(
        **{
            key: _number(raw[key], _field(pointer, key), bounds)
            for key, bounds in HYDROMETEOR_STATE_BOUNDS.items()
        }
    )


def _network(section: "_Section | None", stations: Mapping[str, Station]) -> NetworkConfig | None:
    if section is None:
        return None

    def pair(pointer: str, raw: Any) -> RoutePair:
        item = _Section(raw, pointer, RoutePair)
        source, target = item.string("source"), item.string("target")
        for key, station in (("source", source), ("target", target)):
            if station not in stations:
                raise ValueError(
                    f"{item.pointer_of(key)}={_safe_repr(station)} is not a ground station name"
                )
        if source == target:
            raise ValueError(f"{item.pointer_of('target')}={_safe_repr(target)} equals the source")
        return RoutePair(source, target)

    pairs = section.array("pairs", pair)
    isl_section = section.object("isl")
    isl = IslConfig(
        isl_section.choice("rule"),
        isl_section.number("max_range_m"),
        isl_section.number("capacity_bps"),
    )
    route_models = section.array(
        "route_models",
        lambda pointer, raw: _choice(raw, pointer, section.meta("route_models")["items"]["enum"]),
    )
    fixed = section.number("fixed_capacity_bps")
    pointer = section.pointer_of("fixed_capacity_bps")
    if "fixed_capacity" in route_models and fixed is None:
        raise ValueError(f"{pointer} is required when route_models lists fixed_capacity")
    if "fixed_capacity" not in route_models and fixed is not None:
        raise ValueError(
            f"{pointer}={_safe_repr(fixed)} is only allowed when route_models lists fixed_capacity"
        )
    return NetworkConfig(pairs, isl, fixed, route_models)


def _outputs(section: "_Section") -> OutputConfig:
    return OutputConfig(
        section.array(
            "formats", lambda pointer, raw: _choice(raw, pointer, section.allowed("formats"))
        ),
        section.choice("satellite_positions"),
    )


# --- metadata-driven readers --------------------------------------------------------------------


@functools.cache
def _hints(owner: type) -> dict[str, Any]:
    return get_type_hints(owner)


@functools.cache
def _metadata(owner: type) -> dict[str, Mapping[str, Any]]:
    return {item.name: item.metadata for item in dataclasses.fields(owner)}


def _bounded(number: float, value: Any, pointer: str, meta: Mapping[str, Any]) -> None:
    lower, upper = meta.get("minimum"), meta.get("maximum")
    above, below = meta.get("exclusiveMinimum"), meta.get("exclusiveMaximum")
    inside = (
        (lower is None or number >= lower)
        and (upper is None or number <= upper)
        and (above is None or number > above)
        and (below is None or number < below)
        and ("enum" not in meta or number in meta["enum"])
    )
    if inside:
        return
    where = f"{pointer}={_safe_repr(value)}"
    if "enum" in meta:
        raise ValueError(f"{where} must be one of {list(meta['enum'])}")
    if above == 0.0 and lower is upper is below is None:
        raise ValueError(f"{where} must be positive")
    if lower == 0.0 and above is upper is below is None:
        raise ValueError(f"{where} must be non-negative")
    start = f"[{lower!r}" if lower is not None else f"({above!r}" if above is not None else "(-inf"
    end = f"{upper!r}]" if upper is not None else f"{below!r})" if below is not None else "inf)"
    raise ValueError(f"{where} must be in {start}, {end}")


def _number(value: Any, pointer: str, meta: Mapping[str, Any]) -> float:
    try:
        number = _finite_number(value, pointer)
    except OverflowError:  # an integer too large for a float
        raise ValueError(f"{pointer}={_safe_repr(value)} must be finite") from None
    _bounded(number, value, pointer, meta)
    return number


def _integer(value: Any, pointer: str, meta: Mapping[str, Any]) -> int:
    # JSON Schema counts an integral float such as 4.0 as an integer, so the parser does too.
    integral = isinstance(value, float) and isfinite(value) and value == int(value)
    if isinstance(value, bool) or not (isinstance(value, int) or integral):
        raise ValueError(f"{pointer}={_safe_repr(value)} must be an integer")
    _bounded(int(value), value, pointer, meta)
    return int(value)


def _choice(value: Any, pointer: str, allowed: tuple[Any, ...]) -> Any:
    if not isinstance(value, str) or value not in allowed:
        raise ValueError(f"{pointer}={_safe_repr(value)} must be one of {list(allowed)}")
    return value


class _Section:
    """One JSON object read against the dataclass whose fields it fills.

    The document keys are the dataclass fields (less ``exclude``, plus ``extra``). A field with
    a ``default`` in its metadata may be omitted, and a ``None`` default also allows null.
    """

    __slots__ = ("owner", "pointer", "raw")

    def __init__(
        self,
        raw: Any,
        pointer: str,
        owner: type,
        *,
        extra: tuple[str, ...] = (),
    ) -> None:
        if not isinstance(raw, Mapping):
            raise ValueError(f"{pointer or '/'} must be a JSON object")
        fields = [name for name in _metadata(owner) if name not in NOT_DOCUMENT_FIELDS]
        optional = frozenset(name for name in fields if "default" in _metadata(owner)[name])
        _object(raw, frozenset(fields) - optional | frozenset(extra), pointer, optional)
        self.raw, self.pointer, self.owner = raw, pointer, owner

    def meta(self, name: str) -> Mapping[str, Any]:
        return _metadata(self.owner)[name]

    def pointer_of(self, name: str) -> str:
        return _field(self.pointer, name)

    def value(self, name: str) -> Any:
        if name in self.raw:
            return self.raw[name]
        return copy.deepcopy(self.meta(name)["default"])

    def _nullable(self, name: str) -> bool:
        return "default" in self.meta(name) and self.meta(name)["default"] is None

    def _is_null(self, name: str) -> bool:
        return self.value(name) is None and self._nullable(name)

    def object(self, name: str, *, nullable: bool = False) -> Any:
        owner = _hints(self.owner)[name]
        if nullable:
            if self._is_null(name):
                return None
            owner = next(arg for arg in get_args(owner) if arg is not type(None))
        return _Section(self.value(name), self.pointer_of(name), owner)

    def number(self, name: str) -> float | None:
        if self._is_null(name):
            return None
        return _number(self.value(name), self.pointer_of(name), self.meta(name))

    def integer(self, name: str) -> int:
        return _integer(self.value(name), self.pointer_of(name), self.meta(name))

    def boolean(self, name: str) -> bool:
        value = self.value(name)
        if not isinstance(value, bool):
            raise ValueError(f"{self.pointer_of(name)}={_safe_repr(value)} must be true or false")
        return value

    def string(self, name: str) -> str:
        return _string(self.value(name), self.pointer_of(name))

    def text(self, name: str) -> str:
        value = self.value(name)
        if not isinstance(value, str):
            raise ValueError(f"{self.pointer_of(name)}={_safe_repr(value)} must be a string")
        return value

    def url(self, name: str) -> str:
        return _url(self.value(name), self.pointer_of(name))

    def timestamp(self, name: str) -> datetime:
        return _utc(self.value(name), self.pointer_of(name))

    def sha256(self, name: str) -> str:
        return _sha256(self.value(name), self.pointer_of(name))

    def allowed(self, name: str) -> tuple[Any, ...]:
        """The strings a ``Literal`` field, or a tuple of ``Literal``, may hold."""
        annotation = _hints(self.owner)[name]
        if get_origin(annotation) is tuple:
            annotation = get_args(annotation)[0]
        assert get_origin(annotation) is Literal, name
        return get_args(annotation)

    def choice(self, name: str) -> str:
        return _choice(self.value(name), self.pointer_of(name), self.allowed(name))

    def array(self, name: str, parse: Callable[[str, Any], Any]) -> tuple[Any, ...] | None:
        """Parse a JSON array into a tuple, enforcing the item count and uniqueness bounds."""
        if self._is_null(name):
            return None
        pointer, meta, raw = self.pointer_of(name), self.meta(name), self.value(name)
        if not isinstance(raw, list):
            raise ValueError(f"{pointer} must be an array")
        lower, upper = meta.get("minItems", 0), meta.get("maxItems", len(raw))
        if not lower <= len(raw) <= upper:
            limit = f"at least {lower}" if upper >= len(raw) else f"{lower} to {upper}"
            raise ValueError(f"{pointer} must hold {limit} items, not {len(raw)}")
        parsed: list[Any] = []
        seen: dict[Any, int] = {}
        for index, element in enumerate(raw):
            entry = _field(pointer, str(index))
            item = parse(entry, element)
            if meta.get("uniqueItems"):
                if item in seen:
                    raise ValueError(f"{entry} duplicates {_field(pointer, str(seen[item]))}")
                seen[item] = index
            parsed.append(item)
        return tuple(parsed)


# --- duplicate keys, output formatting --------------------------------------------------------


class _RepeatedKeys(dict):
    """A parsed JSON object that repeated a key; ``load_scenario`` reports it with its pointer."""

    key: str


def _checked_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    repeated = None
    for key, value in pairs:
        if key in result and repeated is None:
            repeated = key
        result[key] = value
    if repeated is None:
        return result
    marked = _RepeatedKeys(result)
    marked.key = repeated
    return marked


def _reject_repeated_keys(root: Any) -> None:
    stack = [("", root)]
    while stack:  # iterative, so a deeply nested file cannot exhaust the recursion limit
        pointer, node = stack.pop()
        if isinstance(node, _RepeatedKeys):
            raise ValueError(f"{_field(pointer, node.key)} is a duplicate JSON object key")
        if isinstance(node, dict):
            children = [(_field(pointer, key), value) for key, value in node.items()]
        elif isinstance(node, list):
            children = [(_field(pointer, str(index)), value) for index, value in enumerate(node)]
        else:
            continue
        stack.extend(reversed(children))


def _utc_text(timestamp: datetime) -> str:
    return timestamp.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _relative_posix(path: Path, directory: Path) -> str:
    try:
        return Path(relpath(path, directory)).as_posix()
    except ValueError:  # Windows cannot express a relative path across drives
        return path.as_posix()
