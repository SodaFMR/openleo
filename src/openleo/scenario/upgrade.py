"""Upgrade an OpenLEO 1.x scenario to scenario v2 (design spec section 3.4).

``upgrade_v1`` maps a parsed v1 document, single-pass (``ground_station``) or constellation
(``stations``), to a v2 document. It checks the shape of the v1 file (keys, containers, names)
and carries every value over unchanged: the bounds of each value are the parser's, applied by
``upgrade_file`` to the result. The mapping:

- ``orbit`` becomes the one catalog of ``space_segment``, its path relative to the output file.
  ``synthetic_rf`` is true when an object name in the catalog contains IRIDIUM, STARLINK,
  ONEWEB or KUIPER (any case), so the archived catalog must be readable and match its hash;
- ``radio_link`` splits into ``radio`` (frequency, bandwidth, EIRP) and ``terminals.default``
  (receive gain, system noise temperature, miscellaneous loss);
- ``time_window`` becomes ``time`` and its elevation mask the default of ``ground_segment``;
  events are enabled and convergence is ``warn``, so that an upgraded file runs and reports its
  grid sensitivity instead of failing;
- a station's ``height_m`` becomes ``height_ellipsoid_m`` and its ``height_amsl_m`` comes from
  ``propagation.station_heights_amsl_m`` (null without propagation, which v1 read as free space);
- the one source-target pair of v1's ``network`` becomes ``pairs``, with all three route models;
- a single-pass file has no ``adaptation`` or ``network``: it gets an upgrade default adaptation
  and no network. Both defaults are recorded in ``description``.
"""

# Every malformed document is a ValueError, including a wrong JSON type, as in the v2 parser.
# ruff: noqa: TRY004

import copy
import csv
import io
import json
import math
from collections.abc import Mapping
from hashlib import sha256
from pathlib import Path
from typing import Any

from openleo._validation import _field, _object, _positive_number, _safe_repr, _string
from openleo.scenario.parse import (
    SCENARIO_SCHEMA_ID,
    _load_document,
    _relative_posix,
    _resolve_catalog_path,
    parse_scenario,
    scenario_document,
)

MAX_CATALOG_BYTES = 10_000_000  # the scenario v2 limit for one catalog
SYNTHETIC_RF_OPERATORS = ("IRIDIUM", "STARLINK", "ONEWEB", "KUIPER")
V1_SCHEMA_ID = "openleo.scenario/1"

# What v1 had no setting for. Fixed here, not read from the v2 model, so that a later change of a
# v2 default cannot change what an upgraded v1 file means.
EVENTS = {"enabled": True, "tolerance_s": 0.001}
CONVERGENCE = {
    "mode": "warn",
    "refinement_factor": 4,
    "relative_tolerance": 0.01,
    "absolute_tolerance_bits": 1.0,
}
OUTPUTS = {"formats": ["csv", "olc"], "satellite_positions": "grid"}
ROUTE_MODELS = ["minimum_delay", "maximum_rate", "fixed_capacity"]
DEFAULT_ROLLOFF = 0.2
DEFAULT_SYMBOL_RATE_DIVISOR = 1.2  # symbol_rate_baud = channel_bandwidth_hz / 1.2

_COMMON_KEYS = ("name", "orbit", "time_window", "radio_link")
_SINGLE_PASS_KEYS = frozenset((*_COMMON_KEYS, "ground_station"))
_CONSTELLATION_KEYS = frozenset((*_COMMON_KEYS, "stations", "adaptation", "network"))
_ORBIT_KEYS = frozenset(("path", "source_url", "retrieved_at_utc", "terms_url", "sha256"))
_STATION_KEYS = frozenset(("name", "latitude_deg", "longitude_deg", "height_m"))
_TIME_WINDOW_KEYS = frozenset(("start_utc", "stop_utc", "step_s", "minimum_elevation_deg"))
_RADIO_LINK_KEYS = frozenset(
    (
        "carrier_frequency_hz",
        "channel_bandwidth_hz",
        "eirp_dbw",
        "receiver_gain_dbi",
        "system_noise_temperature_k",
        "miscellaneous_loss_db",
    )
)
_ADAPTATION_KEYS = frozenset(
    ("symbol_rate_baud", "rolloff", "implementation_margin_db", "hysteresis_db")
)
_NETWORK_KEYS = frozenset(
    (
        "source_station",
        "target_station",
        "isl_max_range_m",
        "isl_capacity_bps",
        "fixed_capacity_bps",
    )
)
_PROPAGATION_KEYS = frozenset(("model", "station_heights_amsl_m", "refinement"))


def upgrade_v1(raw: Any, *, source_dir: Path, output_dir: Path) -> dict[str, Any]:
    """The v2 document for a parsed v1 scenario.

    ``source_dir`` is the directory of the v1 file (the catalog path is relative to it) and
    ``output_dir`` that of the file the result will be saved as. A ``ValueError`` starts with the
    JSON pointer, in the v1 document, of what cannot be upgraded. The catalog is read once, to
    decide ``synthetic_rf``. The result is not yet checked against the v2 bounds.
    """
    if not isinstance(raw, Mapping):
        raise ValueError("/ must be a JSON object")
    document = dict(raw)
    _drop_schema_marker(document)
    single_pass, constellation = "ground_station" in document, "stations" in document
    if single_pass == constellation:
        raise ValueError(
            "/ is not an OpenLEO 1.x scenario: it needs exactly one of ground_station "
            "(single pass) or stations (constellation)"
        )
    keys = _SINGLE_PASS_KEYS if single_pass else _CONSTELLATION_KEYS
    optional = frozenset(("propagation",)) if constellation else frozenset()
    _section(document, keys, "", optional)

    orbit = _section(document["orbit"], _ORBIT_KEYS, "/orbit")
    window = _section(document["time_window"], _TIME_WINDOW_KEYS, "/time_window")
    radio_link = _section(document["radio_link"], _RADIO_LINK_KEYS, "/radio_link")
    stations = (
        [_station(document["ground_station"], "/ground_station")]
        if single_pass
        else _stations(document["stations"])
    )
    propagation = document.get("propagation")
    heights = None if propagation is None else _heights(propagation, stations)

    catalog = _resolve_catalog_path(
        Path(source_dir).resolve(), _string(orbit["path"], "/orbit/path"), "/orbit/path"
    )
    synthetic_rf = _names_a_real_operator(catalog, orbit["sha256"])

    if single_pass:
        adaptation, adaptation_default = _default_adaptation(radio_link["channel_bandwidth_hz"])
        network = None
    else:
        adaptation = {
            "standard": "dvb-s2",
            "table": "v1-reference-subset",
            **_section(document["adaptation"], _ADAPTATION_KEYS, "/adaptation"),
            "feedback_delay_s": 0.0,
        }
        adaptation_default = "adaptation feedback delay 0 s"
        network = _network(_section(document["network"], _NETWORK_KEYS, "/network"))
    return {
        "schema": SCENARIO_SCHEMA_ID,
        "name": document["name"],
        "description": _description(
            "single-pass" if single_pass else "constellation", adaptation_default
        ),
        "time": {
            "start_utc": window["start_utc"],
            "stop_utc": window["stop_utc"],
            "step_s": window["step_s"],
            "events": dict(EVENTS),
            "convergence": dict(CONVERGENCE),
        },
        "space_segment": {
            "catalogs": [
                {
                    "path": _relative_posix(catalog, Path(output_dir).resolve()),
                    "format": "celestrak_gp_csv",
                    "source_url": orbit["source_url"],
                    "retrieved_at_utc": orbit["retrieved_at_utc"],
                    "terms_url": orbit["terms_url"],
                    "sha256": orbit["sha256"],
                }
            ],
            "select_norad_ids": None,
            "synthetic_rf": synthetic_rf,
        },
        "terminals": {
            "default": {
                "receive_gain_dbi": radio_link["receiver_gain_dbi"],
                "system_noise_temperature_k": radio_link["system_noise_temperature_k"],
                "miscellaneous_loss_db": radio_link["miscellaneous_loss_db"],
            }
        },
        "ground_segment": {
            "default_minimum_elevation_deg": window["minimum_elevation_deg"],
            "stations": [
                {
                    "name": station["name"],
                    "latitude_deg": station["latitude_deg"],
                    "longitude_deg": station["longitude_deg"],
                    "height_ellipsoid_m": station["height_m"],
                    "height_amsl_m": None if heights is None else heights[station["name"]],
                    "terminal": "default",
                    "minimum_elevation_deg": None,
                }
                for station in stations
            ],
        },
        "radio": {
            "carrier_frequency_hz": radio_link["carrier_frequency_hz"],
            "channel_bandwidth_hz": radio_link["channel_bandwidth_hz"],
            "eirp_dbw": radio_link["eirp_dbw"],
        },
        "adaptation": adaptation,
        "propagation": _propagation(propagation),
        "network": network,
        "outputs": copy.deepcopy(OUTPUTS),
    }


def upgrade_file(path: Path, output: Path | None = None) -> str:
    """Upgrade the v1 file at ``path`` and return the v2 text; write it to ``output`` if given.

    The text is deterministic: the normalised document (``scenario_document`` of the parsed
    upgrade, so every default is explicit), 2-space indented, with a trailing newline. Catalog
    paths are relative to ``output``'s directory, or to ``path``'s when there is no ``output``.
    """
    path = Path(path)
    raw, source_sha256 = _load_document(path)
    target = path if output is None else Path(output)
    # Resolved, as the parser resolves the scenario path, so a symlinked file relates to its own
    # directory: the catalog paths written here are the ones parse_scenario resolves back.
    document = upgrade_v1(raw, source_dir=path.resolve().parent, output_dir=target.resolve().parent)
    try:
        scenario = parse_scenario(document, target, source_sha256)
    except ValueError as exc:
        raise ValueError(f"{path}: the upgraded scenario is not valid scenario v2: {exc}") from exc
    text = json.dumps(scenario_document(scenario), indent=2, ensure_ascii=False) + "\n"
    if output is not None:
        try:
            target.write_text(text, encoding="utf-8")
        except OSError as exc:
            raise ValueError(f"{target}: could not write the upgraded scenario: {exc}") from exc
    return text


# --- the shape of a v1 file -------------------------------------------------------------------


def _drop_schema_marker(document: dict[str, Any]) -> None:
    """v1 files carry no ``schema`` key; the parser also names ``openleo.scenario/1``."""
    if "schema" not in document:
        return
    marker = document["schema"]
    if marker == SCENARIO_SCHEMA_ID:
        raise ValueError(f"/schema: already an {SCENARIO_SCHEMA_ID} scenario, nothing to upgrade")
    if marker != V1_SCHEMA_ID:
        raise ValueError(f"/schema={_safe_repr(marker)} is not a scenario version this can upgrade")
    del document["schema"]


def _section(
    raw: Any, keys: frozenset[str], pointer: str, optional: frozenset[str] = frozenset()
) -> Mapping[str, Any]:
    if not isinstance(raw, Mapping):
        raise ValueError(f"{pointer or '/'} must be a JSON object")
    return _object(raw, keys, pointer, optional)


def _station(raw: Any, pointer: str) -> Mapping[str, Any]:
    station = _section(raw, _STATION_KEYS, pointer)
    _string(station["name"], f"{pointer}/name")
    return station


def _stations(raw: Any) -> list[Mapping[str, Any]]:
    if not isinstance(raw, list):
        raise ValueError("/stations must be an array of ground stations")
    return [_station(station, f"/stations/{index}") for index, station in enumerate(raw)]


def _heights(propagation: Any, stations: list[Mapping[str, Any]]) -> Mapping[str, Any]:
    section = _section(propagation, _PROPAGATION_KEYS, "/propagation", frozenset(("hydrometeors",)))
    model = section["model"]
    if model != "itu_reference":
        raise ValueError(
            f"/propagation/model={_safe_repr(model)} must be 'itu_reference' in an OpenLEO 1.x scenario"
        )
    heights = section["station_heights_amsl_m"]
    pointer = "/propagation/station_heights_amsl_m"
    if not isinstance(heights, Mapping):
        raise ValueError(f"{pointer} must be a JSON object")
    names = [station["name"] for station in stations]
    for name in names:
        if name not in heights:
            raise ValueError(f"{_field(pointer, name)} is required")
    for name in heights:
        if name not in names:
            raise ValueError(f"{_field(pointer, name)} is not a station")
    return heights


def _propagation(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    if raw is None:
        return {"model": "free_space", "refinement": 1, "hydrometeors": None}
    hydrometeors = raw.get("hydrometeors")
    if "hydrometeors" in raw and not isinstance(hydrometeors, Mapping):
        raise ValueError("/propagation/hydrometeors must be a JSON object")
    return {
        "model": raw["model"],
        "refinement": raw["refinement"],
        "hydrometeors": copy.deepcopy(hydrometeors),
    }


def _network(network: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "pairs": [{"source": network["source_station"], "target": network["target_station"]}],
        "isl": {
            "rule": "all_pairs_within_range",
            "max_range_m": network["isl_max_range_m"],
            "capacity_bps": network["isl_capacity_bps"],
        },
        "fixed_capacity_bps": network["fixed_capacity_bps"],
        "route_models": list(ROUTE_MODELS),
    }


# --- what v1 left unsaid --------------------------------------------------------------------------


def _default_adaptation(bandwidth: Any) -> tuple[dict[str, Any], str]:
    """The single-pass adaptation (v1 had none) and the phrase that records it as a default."""
    pointer = "/radio_link/channel_bandwidth_hz"
    try:
        bandwidth = _positive_number(bandwidth, pointer)
    except OverflowError:  # an integer too large for a float
        raise ValueError(f"{pointer}={_safe_repr(bandwidth)} must be finite") from None
    exact = bandwidth / DEFAULT_SYMBOL_RATE_DIVISOR
    rate = exact
    # The parser needs bandwidth >= rate * (1 + rolloff) in floating point, which the rounding
    # of the division can miss by a unit in the last place.
    while rate * (1 + DEFAULT_ROLLOFF) > bandwidth:
        rate = math.nextafter(rate, 0.0)
    lowered = (
        ""
        if rate == exact
        else " (lowered by whole floating-point steps so that the channel holds rate * (1 + rolloff))"
    )
    adaptation = {
        "standard": "dvb-s2",
        "table": "v1-reference-subset",
        "symbol_rate_baud": rate,
        "rolloff": DEFAULT_ROLLOFF,
        "implementation_margin_db": 0.0,
        "hysteresis_db": 0.0,
        "feedback_delay_s": 0.0,
    }
    phrase = (
        "adaptation dvb-s2 with the v1-reference-subset table, "
        f"symbol_rate_baud = channel_bandwidth_hz / 1.2{lowered}, rolloff 0.2, "
        "implementation margin 0 dB, hysteresis 0 dB and feedback delay 0 s"
    )
    return adaptation, phrase


def _description(kind: str, adaptation_default: str) -> str:
    """The free text of an upgraded scenario: what came from the upgrade, not from the v1 file."""
    return (
        f"Upgraded from an OpenLEO 1.x {kind} scenario by `openleo scenario upgrade`. "
        f"Upgrade defaults, not OpenLEO 1.x inputs: {adaptation_default}; "
        "events enabled (tolerance 0.001 s); convergence mode warn (refinement factor 4, "
        "relative tolerance 0.01, absolute tolerance 1 bit)."
    )


# --- the catalog ----------------------------------------------------------------------------------


def _names_a_real_operator(catalog: Path, declared_sha256: Any) -> bool:
    """Whether any OBJECT_NAME of the archived catalog contains a real operator's name."""
    pointer = "/orbit/path"
    try:
        with catalog.open("rb") as stream:
            raw = stream.read(MAX_CATALOG_BYTES + 1)
    except OSError as exc:
        raise ValueError(
            f"{pointer}: could not read the catalog {catalog} to decide space_segment.synthetic_rf: "
            f"{exc}"
        ) from exc
    if len(raw) > MAX_CATALOG_BYTES:
        raise ValueError(f"{pointer}: the catalog {catalog} exceeds {MAX_CATALOG_BYTES} bytes")
    actual = sha256(raw).hexdigest()
    if actual != declared_sha256:
        raise ValueError(
            f"/orbit/sha256: the catalog {catalog} has SHA-256 {actual}, "
            f"not the declared {_safe_repr(declared_sha256)}"
        )
    try:
        reader = csv.DictReader(io.StringIO(raw.decode("utf-8"), newline=""), strict=True)
        if "OBJECT_NAME" not in (reader.fieldnames or ()):
            raise ValueError("it has no OBJECT_NAME column")
        names = [row["OBJECT_NAME"] or "" for row in reader]
    except UnicodeDecodeError as exc:
        raise ValueError(f"{pointer}: the catalog {catalog} must be UTF-8") from exc
    except (csv.Error, ValueError) as exc:
        raise ValueError(f"{pointer}: the catalog {catalog} is invalid: {exc}") from exc
    return any(operator in name.upper() for name in names for operator in SYNTHETIC_RF_OPERATORS)
