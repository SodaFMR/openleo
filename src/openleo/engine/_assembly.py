"""Tables and ``results.json`` blocks of a run (design spec sections 7.2 and 7.3).

Private to ``openleo.engine.run``: it turns the engine's per-pair results into the columnar
tables and writes the metadata blocks (software, scenario, provenance, models, warnings and
summary). Nothing here computes physics.

Column names carry their unit as a suffix, and every column records it in ``Column.unit``:
``1`` for dimensionless numbers and counts, ``bool`` for flags, ``""`` for text.
"""

from __future__ import annotations

import hashlib
import json
import math
import platform
from collections import Counter
from dataclasses import asdict
from importlib.metadata import PackageNotFoundError, version
from typing import TYPE_CHECKING, Any

import numpy as np
import skyfield
from skyfield.api import wgs84

from openleo.engine.timegrid import utc_text
from openleo.physics.adaptation import ETSI_SOURCE_URL, MODCODS
from openleo.physics.hydrometeors import CLOUD_SOURCE, RAIN_SOURCE
from openleo.physics.reference_atmosphere import PROFILE_SOURCE
from openleo.results.model import BOOL_UNIT, Column, Table
from openleo.scenario.parse import scenario_document

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from openleo.engine.catalogs import LoadedOrbit
    from openleo.engine.routing import RouteInterval
    from openleo.engine.timegrid import TimeGrid
    from openleo.scenario.model import CatalogSource, Scenario

ELEMENT_AGE_WARNING_DAYS = 14.0
SYNTHETIC_RF_WARNING = (
    "space_segment.synthetic_rf is true: the radio, terminal and adaptation parameters are "
    "declared research inputs, not the named operator's hardware or waveform"
)
EVENT_TYPES = ("aos", "modcod_decision", "modcod_effective", "culmination", "los")
# The ITU-R sources the v1 propagation metadata cites (openleo.propagation, v1.0.0).
P453_SOURCE = {
    "recommendation": "ITU-R P.453-14",
    "url": "https://www.itu.int/dms_pubrec/itu-r/rec/p/R-REC-P.453-14-201908-I!!PDF-E.pdf",
    "sha256": "fd163ef75cb4fd03848d4fe1fc0a043cb78e0edc164ba29a6bb69209204c17a0",
}
P676_SOURCE = {
    "recommendation": "ITU-R P.676-13",
    "url": "https://www.itu.int/dms_pubrec/itu-r/rec/p/R-REC-P.676-13-202208-I!!PDF-E.pdf",
    "sha256": "8c09b2d2c120bdae33f60c2a7abff873374d54075ce0dc71060de8a5507bbe2f",
}

# Units by column-name suffix, longest suffix first.
_UNITS = (
    ("_db_per_km", "dB/km"),
    ("_db_hz", "dB-Hz"),
    ("_kg_m2", "kg/m^2"),
    ("_mm_h", "mm/h"),
    ("_bits", "bit"),
    ("_bps", "bit/s"),
    ("_fraction", "1"),
    ("_mps", "m/s"),
    ("_days", "d"),
    ("_deg", "deg"),
    ("_dbi", "dBi"),
    ("_db", "dB"),
    ("_hz", "Hz"),
    ("_s", "s"),
    ("_m", "m"),
)
_COLUMN_DESCRIPTIONS = {
    "elevation_deg": "Geometric elevation of the satellite above the station horizon.",
    "azimuth_deg": "Azimuth from geodetic north, clockwise, in [0, 360).",
    "range_m": "Station-to-satellite range.",
    "range_rate_mps": "Range rate; positive when receding.",
    "doppler_hz": "First-order Doppler shift of the carrier; receding is negative.",
    "delay_s": "One-way delay: geometric range / c plus any atmospheric excess delay.",
    "cn0_db_hz": "Carrier-to-noise-density ratio after atmospheric loss.",
    "esn0_db": "Symbol energy to noise density, C/N0 - 10 log10(symbol rate).",
    "snr_db": "Signal-to-noise ratio in the channel bandwidth.",
    "shannon_upper_bound_bps": "Shannon AWGN capacity upper bound, not throughput.",
    "gaseous_dry_attenuation_db": "Dry-air attenuation along the ray (ITU-R P.676-13).",
    "gaseous_water_attenuation_db": "Water-vapour attenuation along the ray (ITU-R P.676-13).",
    "gaseous_attenuation_db": "Total gaseous attenuation along the ray (ITU-R P.676-13).",
    "free_space_cn0_db_hz": "C/N0 before atmospheric loss.",
    "geometric_delay_s": "Geometric range / c.",
    "atmospheric_excess_delay_s": "Non-dispersive atmospheric excess delay.",
    "apparent_elevation_deg": "Refracted (apparent) elevation from the ray trace.",
    "cloud_attenuation_db": "Declared-cloud attenuation (ITU-R P.840-9).",
    "rain_specific_attenuation_db_per_km": "Rain specific attenuation (ITU-R P.838-3).",
    "rain_path_length_m": "Slant path length through the declared rain layer.",
    "rain_attenuation_db": "Declared-rain attenuation.",
    "hydrometeor_attenuation_db": "Cloud plus rain attenuation.",
    "total_atmospheric_attenuation_db": "Gaseous plus hydrometeor attenuation.",
}


def unit_of(name: str) -> str:
    """The unit a numeric column name's suffix declares (``1`` without one)."""
    for suffix, unit in _UNITS:
        if name.endswith(suffix):
            return unit
    return "1"


def _column(name: str, dtype: str, description: str, unit: str | None = None) -> Column:
    if unit is None:
        unit = "" if dtype == "str" else unit_of(name)
    return Column(name, dtype, unit, description)


def _f64(values) -> np.ndarray:
    return np.asarray(values, dtype=np.float64).reshape(-1)


def _i64(values) -> np.ndarray:
    return np.asarray(values, dtype=np.int64).reshape(-1)


def _flags(values) -> np.ndarray:
    return np.asarray(values, dtype=bool).reshape(-1).astype(np.uint8)


# --- tables ----------------------------------------------------------------------------------


def satellites_table(orbits: Sequence[LoadedOrbit], grid: TimeGrid) -> Table:
    columns = (
        _column("index", "i64", "Satellite index, in catalog then row order."),
        _column("name", "str", "Catalog OBJECT_NAME."),
        _column("norad_id", "i64", "NORAD catalog number."),
        _column("epoch_utc", "str", "Element-set epoch, ISO 8601 UTC."),
        _column(
            "max_abs_element_age_days",
            "f64",
            "Largest |time - epoch| over the window start and stop.",
        ),
    )
    return Table(
        "satellites",
        columns,
        {
            "index": _i64(range(len(orbits))),
            "name": tuple(orbit.satellite.name for orbit in orbits),
            "norad_id": _i64([orbit.satellite.model.satnum for orbit in orbits]),
            "epoch_utc": tuple(utc_text(orbit.epoch_utc, 0.0) for orbit in orbits),
            "max_abs_element_age_days": _f64([element_age_days(o, grid) for o in orbits]),
        },
    )


def element_age_days(orbit: LoadedOrbit, grid: TimeGrid) -> float:
    """v1's element age: the larger |t - epoch| over the window start and stop, in days."""
    return max(
        abs((moment - orbit.epoch_utc).total_seconds()) / 86_400.0
        for moment in (grid.start_utc, grid.stop_utc)
    )


def stations_table(scenario: Scenario, masks: Sequence[float], ecef_m: np.ndarray) -> Table:
    stations = scenario.ground_segment.stations
    columns = (
        _column("index", "i64", "Station index, in scenario order."),
        _column("name", "str", "Station name."),
        _column("latitude_deg", "f64", "Geodetic latitude (WGS84)."),
        _column("longitude_deg", "f64", "Geodetic longitude (WGS84)."),
        _column("height_ellipsoid_m", "f64", "Height above the WGS84 ellipsoid."),
        _column("height_amsl_m", "f64", "Height above mean sea level; empty when not declared."),
        _column("minimum_elevation_deg", "f64", "Elevation mask in effect."),
        _column("terminal", "str", "Receive terminal name."),
        _column("x_ecef_m", "f64", "ITRS (ECEF) x."),
        _column("y_ecef_m", "f64", "ITRS (ECEF) y."),
        _column("z_ecef_m", "f64", "ITRS (ECEF) z."),
    )
    return Table(
        "stations",
        columns,
        {
            "index": _i64(range(len(stations))),
            "name": tuple(s.name for s in stations),
            "latitude_deg": _f64([s.latitude_deg for s in stations]),
            "longitude_deg": _f64([s.longitude_deg for s in stations]),
            "height_ellipsoid_m": _f64([s.height_ellipsoid_m for s in stations]),
            "height_amsl_m": _f64(
                [math.nan if s.height_amsl_m is None else s.height_amsl_m for s in stations]
            ),
            "minimum_elevation_deg": _f64(masks),
            "terminal": tuple(s.terminal for s in stations),
            "x_ecef_m": _f64(ecef_m[:, 0]),
            "y_ecef_m": _f64(ecef_m[:, 1]),
            "z_ecef_m": _f64(ecef_m[:, 2]),
        },
    )


def positions_table(positions: Sequence[np.ndarray], grid: TimeGrid) -> Table:
    """Satellite ECEF positions at every grid time; ``positions[s]`` has shape (samples, 3)."""
    samples = grid.offsets_s.size
    stacked = np.concatenate(positions) if positions else np.empty((0, 3))
    columns = (
        _column("satellite_index", "i64", "Satellite index."),
        _column("time_index", "i64", "Grid sample index."),
        _column("t_offset_s", "f64", "Seconds after time.start_utc."),
        _column("x_ecef_m", "f64", "ITRS (ECEF) x at the grid time."),
        _column("y_ecef_m", "f64", "ITRS (ECEF) y at the grid time."),
        _column("z_ecef_m", "f64", "ITRS (ECEF) z at the grid time."),
    )
    return Table(
        "positions",
        columns,
        {
            "satellite_index": _i64(np.repeat(np.arange(len(positions)), samples)),
            "time_index": _i64(np.tile(np.arange(samples), len(positions))),
            "t_offset_s": _f64(np.tile(grid.offsets_s, len(positions))),
            "x_ecef_m": _f64(stacked[:, 0]),
            "y_ecef_m": _f64(stacked[:, 1]),
            "z_ecef_m": _f64(stacked[:, 2]),
        },
    )


SAMPLE_EXTRA = (
    ("modcod", "str", "MODCOD in effect; empty when there is none (no lock or frames lost)."),
    ("rate_bps", "f64", "Data rate of the MODCOD in effect (symbol rate x efficiency)."),
    (
        "margin_db",
        "f64",
        (
            "Es/N0 minus implementation margin minus the requirement of the MODCOD in effect "
            "(of the lowest MODCOD when there is none), as v1."
        ),
    ),
    ("remaining_contact_s", "f64", "Time from the sample to the end of its pass."),
    ("contact_truncated", "u8", "Whether the pass is cut by the window stop."),
)


def samples_table(evaluator_columns: Sequence[str], parts: Sequence[Mapping[str, Any]]) -> Table:
    """Visible (station, satellite, grid time) samples, from one mapping of columns per pair."""
    columns = [
        _column("station_index", "i64", "Station index."),
        _column("satellite_index", "i64", "Satellite index."),
        _column("time_index", "i64", "Grid sample index."),
        _column("t_offset_s", "f64", "Seconds after time.start_utc."),
    ]
    columns += [_column(name, "f64", _COLUMN_DESCRIPTIONS[name]) for name in evaluator_columns]
    columns += [
        _column(name, dtype, text, BOOL_UNIT if dtype == "u8" else None)
        for name, dtype, text in SAMPLE_EXTRA
    ]
    data: dict[str, Any] = {}
    for column in columns:
        if column.dtype == "str":
            data[column.name] = tuple(text for part in parts for text in part[column.name])
            continue
        convert = {"i64": _i64, "f64": _f64, "u8": _flags}[column.dtype]
        values = [part[column.name] for part in parts]
        data[column.name] = convert(np.concatenate(values) if values else [])
    return Table("samples", tuple(columns), data)


def events_table(rows: Sequence[tuple]) -> Table:
    """Rows: (station, satellite, t, type, modcod_from, modcod_to, elevation, decision_t)."""
    columns = (
        _column("station_index", "i64", "Station index."),
        _column("satellite_index", "i64", "Satellite index."),
        _column("t_offset_s", "f64", "Event time, seconds after time.start_utc."),
        _column("type", "str", "aos, los, culmination, modcod_decision or modcod_effective."),
        _column("modcod_from", "str", "MODCOD state before a MODCOD event; empty for no lock."),
        _column("modcod_to", "str", "MODCOD state after a MODCOD event; empty for no lock."),
        _column("elevation_deg", "f64", "Culmination: the highest elevation; empty otherwise."),
        _column(
            "decision_offset_s",
            "f64",
            "MODCOD events: the decision time (the effective time is the decision time plus "
            "the feedback delay); empty otherwise.",
        ),
    )
    names = [c.name for c in columns]
    data = {name: [row[k] for row in rows] for k, name in enumerate(names)}
    return Table(
        "events",
        columns,
        {
            "station_index": _i64(data["station_index"]),
            "satellite_index": _i64(data["satellite_index"]),
            "t_offset_s": _f64(data["t_offset_s"]),
            "type": tuple(data["type"]),
            "modcod_from": tuple(data["modcod_from"]),
            "modcod_to": tuple(data["modcod_to"]),
            "elevation_deg": _f64(data["elevation_deg"]),
            "decision_offset_s": _f64(data["decision_offset_s"]),
        },
    )


def passes_table(rows: Sequence[Mapping[str, Any]]) -> Table:
    spec = (
        ("station_index", "i64", "Station index."),
        ("satellite_index", "i64", "Satellite index."),
        ("pass_index", "i64", "Pass number within the pair, in time order."),
        ("aos_offset_s", "f64", "Start of visibility (root-found AOS, or 0 when truncated)."),
        ("los_offset_s", "f64", "End of visibility (root-found LOS, or the window stop)."),
        ("culmination_offset_s", "f64", "Time of the highest elevation inside the window."),
        ("truncated_start", "u8", "Visible at the window start."),
        ("truncated_end", "u8", "Visible at the window stop."),
        ("duration_s", "f64", "Visible duration, LOS - AOS."),
        ("rf_usable_duration_s", "f64", "Time with a non-zero data rate."),
        ("integrated_rate_bits", "f64", "Data rate integrated over the pass."),
        (
            "integrated_shannon_upper_bound_bits",
            "f64",
            "Shannon upper bound integrated over the pass.",
        ),
        ("max_elevation_deg", "f64", "Highest elevation inside the window."),
    )
    return _record_table("passes", spec, rows)


def _record_table(name: str, spec, rows: Sequence[Mapping[str, Any]]) -> Table:
    columns = tuple(
        _column(key, dtype, text, BOOL_UNIT if dtype == "u8" else None) for key, dtype, text in spec
    )
    data: dict[str, Any] = {}
    for column in columns:
        values = [row[column.name] for row in rows]
        if column.dtype == "str":
            data[column.name] = tuple(values)
        else:
            data[column.name] = {"i64": _i64, "f64": _f64, "u8": _flags}[column.dtype](values)
    return Table(name, columns, data)


_METRIC_DESCRIPTIONS = {
    "pass_count": "Passes of positive duration.",
    "visible_duration_s": "Time measure of visibility.",
    "rf_usable_duration_s": "Time measure of a non-zero data rate.",
    "integrated_rate_bits": "Data rate integrated exactly over the rate segments.",
    "integrated_shannon_upper_bound_bits": "Shannon upper bound integrated over the passes.",
    "max_elevation_deg": "Highest elevation over the passes; empty when never visible.",
    "min_range_m": "Shortest range over the passes; empty when never visible.",
    "mean_rate_visible_bps": "Integrated rate / visible duration; empty when never visible.",
    "visible_fraction": "Time measure with at least one satellite visible / window length.",
    "rf_usable_fraction": "Time measure with a non-zero best-link rate / window length.",
    "best_link_integrated_bits": "Best-link envelope integrated over the window.",
    "best_link_handovers": "Changes of best satellite between adjacent envelope segments.",
    "best_link_mean_rate_bps": "Best-link integral / window length.",
    "fixed_capacity_integrated_bits": "fixed_capacity_bps x visible time measure.",
    "connected_fraction": "Time measure with a route / window length.",
    "integrated_bottleneck_bits": "Route bottleneck integrated over connected time.",
    "route_changes": "Changes of node sequence between adjacent connected intervals.",
    "mean_delay_connected_s": "Time-weighted mean path delay; empty when never connected.",
}
_COUNTS = frozenset(("pass_count", "best_link_handovers", "route_changes"))


def _metric_spec(key: str) -> tuple[str, str, str]:
    if key.startswith("residence_"):
        return key, "f64", "Time this MODCOD is in effect with a non-zero rate."
    return key, "i64" if key in _COUNTS else "f64", _METRIC_DESCRIPTIONS[key]


def pair_metrics_table(rows: Sequence[Mapping[str, Any]], keys: Sequence[str]) -> Table:
    spec = (
        ("station_index", "i64", "Station index."),
        ("satellite_index", "i64", "Satellite index."),
        ("norad_id", "i64", "NORAD catalog number."),
        *(_metric_spec(key) for key in keys),
    )
    return _record_table("pair_metrics", spec, rows)


def station_metrics_table(rows: Sequence[Mapping[str, Any]], keys: Sequence[str]) -> Table:
    spec = (
        ("station_index", "i64", "Station index."),
        ("name", "str", "Station name."),
        *(_metric_spec(key) for key in keys),
    )
    return _record_table("station_metrics", spec, rows)


def routes_table(intervals: Sequence[RouteInterval], scenario: Scenario) -> Table:
    models = list(scenario.network.route_models)
    rows = []
    for interval in intervals:
        pair = scenario.network.pairs[interval.pair_index]
        connected = interval.nodes is not None
        rows.append(
            {
                "pair_index": interval.pair_index,
                "model_index": models.index(interval.model),
                "source": pair.source,
                "target": pair.target,
                "model": interval.model,
                "start_offset_s": interval.start_s,
                "end_offset_s": interval.end_s,
                "connected": connected,
                "nodes": " ".join(str(node) for node in interval.nodes) if connected else "",
                "delay_s": interval.delay_s if connected else math.nan,
                "bottleneck_bps": interval.bottleneck_bps if connected else math.nan,
            }
        )
    spec = (
        ("pair_index", "i64", "Index of the pair in network.pairs."),
        ("model_index", "i64", "Index of the model in network.route_models."),
        ("source", "str", "Source station."),
        ("target", "str", "Target station."),
        ("model", "str", "Route model."),
        ("start_offset_s", "f64", "Interval start, seconds after time.start_utc."),
        ("end_offset_s", "f64", "Interval end, seconds after time.start_utc."),
        ("connected", "u8", "Whether a route exists on the interval."),
        (
            "nodes",
            "str",
            "Route node indices, space separated: satellites 0..N-1, then station k as N+k.",
        ),
        ("delay_s", "f64", "Path delay; empty when disconnected."),
        ("bottleneck_bps", "f64", "Path bottleneck capacity; empty when disconnected."),
    )
    return _record_table("routes", spec, rows)


def route_metrics_table(
    metrics: Mapping[tuple[int, str], Mapping[str, Any]], scenario: Scenario
) -> Table:
    models = list(scenario.network.route_models)
    rows = []
    keys = list(next(iter(metrics.values())))  # every pair and model has the same metrics
    for (pair_index, model), values in metrics.items():
        pair = scenario.network.pairs[pair_index]
        rows.append(
            {
                "pair_index": pair_index,
                "model_index": models.index(model),
                "source": pair.source,
                "target": pair.target,
                "model": model,
                **values,
            }
        )
    spec = (
        ("pair_index", "i64", "Index of the pair in network.pairs."),
        ("model_index", "i64", "Index of the model in network.route_models."),
        ("source", "str", "Source station."),
        ("target", "str", "Target station."),
        ("model", "str", "Route model."),
        *(_metric_spec(key) for key in keys),
    )
    return _record_table("route_metrics", spec, rows)


def rate_segments_table(rows: Sequence[Mapping[str, Any]]) -> Table:
    spec = (
        ("station_index", "i64", "Station index."),
        ("satellite_index", "i64", "Satellite index."),
        ("start_offset_s", "f64", "Segment start, seconds after time.start_utc."),
        ("end_offset_s", "f64", "Segment end (exclusive), seconds after time.start_utc."),
        ("modcod", "str", "MODCOD in effect; empty when there is none."),
        ("rate_bps", "f64", "Constant data rate on the segment."),
    )
    return _record_table("rate_segments", spec, rows)


# --- metadata --------------------------------------------------------------------------------


def json_safe(value: Any) -> Any:
    """``value`` with every non-finite float replaced by None (JSON null)."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {key: json_safe(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [json_safe(item) for item in value]
    return value


def _package_version(name: str) -> str:
    try:
        return version(name)
    except PackageNotFoundError:  # pragma: no cover - every dependency is installed
        return "unknown"


def software() -> dict[str, str]:
    from openleo import __version__

    return {
        "openleo": __version__,
        "numpy": np.__version__,
        "skyfield": skyfield.__version__,
        "sgp4": _package_version("sgp4"),
        "python": platform.python_version(),
        "platform": platform.platform(),
    }


def canonical_sha256(document: Any) -> str:
    """SHA-256 of a document's canonical JSON: sorted keys, compact, UTF-8."""
    text = json.dumps(
        document, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def scenario_block(scenario: Scenario) -> dict[str, Any]:
    """The normalised scenario document, plus the original file's and its own SHA-256.

    The two hashes are the only keys that are not scenario keys; drop them to re-parse it.
    """
    document = scenario_document(scenario)
    return {
        **document,
        "source_sha256": scenario.source_sha256,
        "normalised_sha256": canonical_sha256(document),
    }


def archive_names(catalogs: Sequence[CatalogSource]) -> list[str]:
    """``<basename>`` per catalog; ``<index>-<basename>`` for basenames shared by several."""
    names = [catalog.path.name for catalog in catalogs]
    return [
        name if names.count(name) == 1 else f"{index}-{name}" for index, name in enumerate(names)
    ]


def leap_second_table_sha256(timescale) -> str:
    """v1's fingerprint of Skyfield's leap-second table (``simulation._leap_second_table_sha256``)."""
    payload = {
        "leap_dates": [float(value) for value in timescale.leap_dates],
        "leap_offsets": [float(value) for value in timescale.leap_offsets],
    }
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def provenance(
    scenario: Scenario,
    orbits: Sequence[LoadedOrbit],
    record_counts: Sequence[int],
    grid: TimeGrid,
    timescale,
) -> dict[str, Any]:
    selected = Counter(orbit.actual_sha256 for orbit in orbits)
    paths = [entry["path"] for entry in scenario_document(scenario)["space_segment"]["catalogs"]]
    archived = archive_names(scenario.space_segment.catalogs)
    catalogs = []
    for catalog, path, name, count in zip(
        scenario.space_segment.catalogs, paths, archived, record_counts, strict=True
    ):
        catalogs.append(
            {
                "path": path,
                # Where ``save_run`` archives it, relative to the run's ``inputs/`` directory.
                "archived_path": f"catalogs/{name}",
                "format": catalog.format,
                "source_url": catalog.source_url,
                "retrieved_at_utc": utc_text(catalog.retrieved_at_utc, 0.0),
                "terms_url": catalog.terms_url,
                "sha256": catalog.sha256,
                "record_count": count,
                "selected_count": selected.get(catalog.sha256, 0),
            }
        )
    return {
        "catalogs": catalogs,
        "leap_second_table_source": "skyfield-builtin",
        "leap_second_table_sha256": leap_second_table_sha256(timescale),
        "elements": [
            {
                "satellite_index": index,
                "norad_id": int(orbit.satellite.model.satnum),
                "name": orbit.satellite.name,
                "epoch_utc": utc_text(orbit.epoch_utc, 0.0),
                "max_abs_element_age_days": element_age_days(orbit, grid),
            }
            for index, orbit in enumerate(orbits)
        ],
    }


def warnings(scenario: Scenario, orbits: Sequence[LoadedOrbit], grid: TimeGrid) -> list[str]:
    """Element age above 14 days (v1's wording) and the synthetic-RF flag."""
    result = []
    for orbit in orbits:
        age = element_age_days(orbit, grid)
        if age > ELEMENT_AGE_WARNING_DAYS:
            result.append(
                f"NORAD {orbit.satellite.model.satnum}: maximum requested time is more than "
                f"14 days from the element epoch ({age:.6g} days)"
            )
    if scenario.space_segment.synthetic_rf:
        result.append(SYNTHETIC_RF_WARNING)
    return result


def models(
    scenario: Scenario,
    timescale,
    events_on: bool,
    gas_table_bounds_db: Sequence[float | None] | None = None,
) -> dict[str, Any]:
    """One entry per kernel with its standard and version; v1's model text where it applies.

    ``gas_table_bounds_db`` is ``LinkEvaluator.gas_tables``: per station, the verified loss error
    bound of the gas table the Shannon integrand and MODCOD root-finding read, or None where they
    trace the ray exactly.
    """
    propagation = scenario.propagation
    reference = propagation.model == "itu_reference"
    hydrometeors = propagation.hydrometeors is not None
    integration = (
        "Event segments: piecewise-constant rates integrated exactly between root-found AOS, "
        "LOS and MODCOD switch times; the Shannon bound by adaptive 5-point Gauss-Legendre on "
        "pieces of at most 10 s; one time measure for every duration and fraction"
        if events_on
        else "Grid left hold (v1 semantics): each sample's state held to the next sample; the "
        "final sample holds no time; one time measure for every duration and fraction"
    )
    entries: dict[str, Any] = {
        "orbit_propagation": {
            "standard": "SGP4",
            "version": f"sgp4 {_package_version('sgp4')}, WGS72 gravity",
            "method": "SGP4 with WGS72 gravity, TEME GP elements transformed by Skyfield",
            "teme_to_itrs": "rot_z(-GMST1982 at UT1) with Skyfield's theta_GMST1982, plus "
            "Skyfield's Earth-rotation rate term for the velocity: Skyfield's TEME-GCRS-ITRS "
            "chain without its nutation, which cancels; equal to it up to rounding (about "
            "5e-8 m and 6e-11 m/s), verified against the v1 fixtures",
            "earth_orientation": "Skyfield builtin time data; polar motion is not supplied",
        },
        "coordinates": {
            "standard": "WGS84",
            "version": f"Skyfield {skyfield.__version__}",
            "frame": "ITRS",
            "units": "metres",
            "equatorial_radius_m": float(wgs84.radius.m),
            "inverse_flattening": float(wgs84.inverse_flattening),
        },
        "time": {
            "standard": "UTC (POSIX datetimes), Skyfield builtin leap-second and Delta T tables",
            "version": f"Skyfield {skyfield.__version__}",
            "leap_second_table_source": "skyfield-builtin",
            "leap_second_table_sha256": leap_second_table_sha256(timescale),
            "sampling": "Inclusive UTC grid from start_utc at step_s, plus the exact stop "
            "(a shorter last interval when off the grid); whole microseconds",
            "integration": integration,
        },
        "free_space_path_loss": {
            "standard": "ITU-R P.525",
            "version": "P.525-5",
            "method": "20 log10(4 pi d f / c)",
        },
        "radio_link": {
            "standard": "AWGN link budget",
            "version": "OpenLEO v1.0.0 radio metrics",
            "channel": "Free-space plus reference gaseous and declared hydrometeor loss, "
            "fixed-noise AWGN"
            if hydrometeors
            else "Free-space plus reference gaseous loss, fixed-noise AWGN"
            if reference
            else "Free-space AWGN",
            "doppler": "First order: -carrier_frequency_hz * range_rate_mps / c; receding is "
            "negative",
            "delay": "One-way instantaneous geometric range / c plus non-dispersive atmospheric "
            "optical-path excess delay"
            if reference
            else "One-way instantaneous geometric range / c",
            "capacity": "Shannon AWGN upper bound, not traffic throughput",
            "constants": "c = 299792458 m/s; k = 1.380649e-23 J/K (exact SI)",
        },
        "adaptation": {
            "standard": "ETSI EN 302 307-1",
            "version": "V1.4.1 (2014-11), Table 13 subset (five rows)",
            "source_url": ETSI_SOURCE_URL,
            "assumptions": "DVB-S2 normal 64800-bit frames without pilots, ideal AWGN reference "
            "requirements; scenario implementation margin added",
            "modcod_table": [asdict(modcod) for modcod in MODCODS],
            "esn0": "C/N0 - 10*log10(symbol_rate_baud)",
            "rate": "symbol_rate_baud * spectral efficiency; no MAC, network or application "
            "overhead deduction",
            "hysteresis": "Upgrades of an existing lock only; immediate nominal-threshold "
            "downgrade; no-contact resets lock",
            "switching": "Exact switch times where Es/N0 - margin crosses a threshold"
            if events_on
            else "Selection at grid samples, held to the next sample",
            "feedback_delay_s": scenario.adaptation.feedback_delay_s,
        },
    }
    if reference:
        entries |= {
            "gaseous_attenuation": {
                "standard": "ITU-R P.676",
                "version": "P.676-13",
                **P676_SOURCE,
                "method": "Annex 1 equations (16)-(19), normalized exponential layers; midpoint "
                "attenuation and refractivity; split-layer refinement",
                "endpoint_solve": "Apparent elevation by safeguarded Newton iteration on the "
                "ray's endpoint angle, to steps of at most 1e-11 deg (v1: 40-step bisection, "
                "within 4e-11 deg). Equivalence with the v1 kernel is tested at 12, 20 and 30 "
                "GHz to 1e-10 dB, 1e-9 deg and 1e-13 s. At high loss (above about 50 GHz) the "
                "gaseous dB of samples differs from v1's by more than 1e-10 dB near 5 deg "
                "elevation, because v1's bisection is the less precise solve",
                "refinement": propagation.refinement,
                "earth_radius_km": 6371.0,
                "upper_height_km": 100.0,
            },
            "reference_atmosphere": {
                "standard": "ITU-R P.835",
                "version": "P.835-7",
                **PROFILE_SOURCE,
                "method": "Annex 1 global reference temperature, total pressure and water "
                "vapour; dry pressure = total minus vapour pressure",
            },
            "refractivity": {
                "standard": "ITU-R P.453",
                "version": "P.453-14",
                **P453_SOURCE,
                "method": "Annex 1 dry and wet radio refractivity; non-dispersive refractive index",
            },
        }
        if events_on and gas_table_bounds_db is not None:
            names = [station.name for station in scenario.ground_segment.stations]
            bounds = {name: bound for name, bound in zip(names, gas_table_bounds_db, strict=True)}
            tabulated = [bound for bound in bounds.values() if bound is not None]
            entries["shannon_integrand_atmosphere"] = {
                "standard": "OpenLEO numerical method",
                "version": "S1 per-station gas table",
                "use": "Shannon-bound quadrature nodes and MODCOD threshold root-finding: the "
                "rate segments, and so the modcod and rate_bps of every sample, come from this "
                "path; every other per-sample value (attenuations, C/N0, Es/N0, SNR, Shannon "
                "bound, delays) traces the ray exactly",
                "method": "Per station, the column's total gaseous loss and ray central angle "
                "as piecewise Chebyshev series in apparent elevation (16 segments of degree 16 "
                "on [5, 90] deg) from the exact layer sums; the endpoint solve keeps the exact "
                "vacuum geometry",
                "max_error_db": max(tabulated) if tabulated else None,
                "max_error_db_meaning": "The largest station's heuristic Chebyshev-level bound "
                "on |tabulated - ray-traced| gaseous loss (see docs/METHODS.md): twice the series "
                "errors found at the check points, the elevation shift they cause and both "
                "solves' 1e-11 deg stopping error, each times the local loss slope; it assumes "
                "the series error between check points is at most twice the error at them, which "
                "is not proved, and is checked end to end against the exact ray trace at "
                "construction",
                "station_error_bound_db": bounds,
                "fallback": "Stations whose bound exceeds 2.5e-11 dB (a quarter of the 1e-10 dB "
                "limit), or whose end-to-end check exceeds the bound, use the exact ray trace "
                "(null above)",
                "verification": "Bounded at construction against the exact layer sums at every "
                "segment end and between consecutive Chebyshev points, and traced end to end "
                "against the ray trace at fixed geometries from 5 to 90 deg and 110 to 1200 km "
                "altitude; tests/physics/test_gas_table.py sweeps 1-183 GHz",
            }
    if hydrometeors:
        entries |= {
            "rain_attenuation": {
                "standard": "ITU-R P.838",
                "version": "P.838-3",
                **RAIN_SOURCE,
                "method": "Annex 1 equations (1)-(5); k*R^alpha in dB/km times the declared "
                "uniform-layer path in km",
            },
            "cloud_attenuation": {
                "standard": "ITU-R P.840",
                "version": "P.840-9",
                **CLOUD_SOURCE,
                "method": "Annex 1 equations (11)-(12); KL(f)*L/sin(geometric elevation)",
            },
        }
    if scenario.network is not None:
        entries["network"] = {
            "standard": "OpenLEO snapshot routing",
            "version": "OpenLEO v1.0.0 graph rule and algorithms",
            "links": "Simultaneous reciprocal snapshots; range-limited ISLs with WGS84 Earth "
            "occlusion",
            "relay": "Only source and target ground stations participate; intermediate "
            "stations do not relay",
            "baseline": "fixed_capacity assigns fixed_capacity_bps to every geometrically "
            "visible ground link of the route endpoints regardless of the RF threshold (MODCOD "
            "lock), so it routes over links whose adaptive rate is zero; ISLs retain "
            "isl.capacity_bps",
            "breakpoints": "Grid times plus every pass and rate-segment boundary of the "
            "endpoint stations; routes held between them",
        }
    return entries
