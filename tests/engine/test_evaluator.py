"""The continuous link budget, checked against frozen OpenLEO v1.0.0 numbers and v1's arithmetic."""

import functools
import gzip
import json
from collections import defaultdict
from dataclasses import asdict, replace
from math import log2
from pathlib import Path

import numpy as np
import pytest
from skyfield.api import load

from openleo.physics.adaptation import MODCODS, select_modcod
from openleo.physics.atmospheric_path import build_reference_column
from openleo.physics.hydrometeor_path import evaluate_hydrometeor_path
from openleo.physics.link import (
    SPEED_OF_LIGHT_MPS,
    carrier_to_noise_density_db_hz,
    doppler_shift_hz,
    free_space_path_loss_db,
    noise_density_dbw_per_hz,
    propagation_delay_s,
    received_carrier_power_dbw,
    shannon_capacity_upper_bound_bps,
    signal_to_noise_ratio_db,
)
from openleo.scenario import load_scenario
from openleo.scenario.model import Terminal

ROOT = Path(__file__).resolve().parents[2]
V1_FIXTURES = ROOT / "tests" / "fixtures" / "v1"
FIXTURE_NAMES = (
    "iss_cartagena",
    "iridium_global",
    "iridium_global_reference",
    "iridium_global_hydrometeors",
)
TIMESCALE = load.timescale(builtin=True)
# Station 1 of the Iridium examples; it sees satellite 0 in the window (Madrid does not).
TROMSO = 1


def _example_path(name: str) -> Path:
    """A shipped example scenario: the ISS single pass or one of the Iridium constellations."""
    folder = "scenarios" if name == "iss_cartagena" else "constellations"
    return ROOT / "examples" / folder / f"{name}.json"


# v1-equivalence tolerances (spec section 11.1; excess delay as amended by the Task 2 ruling).
ANGLE_DEG = 1e-8
RANGE_M = 1e-4
RANGE_RATE_MPS = 1e-6
DOPPLER_HZ = 1e-3
DB = 1e-9
EXCESS_DELAY_S = 1e-13
APPARENT_ELEVATION_DEG = 1e-9
# Quantities the table does not list take the tolerance of their inputs carried through the
# equation: a delay is range / c (plus the excess delay), a rain path is a length.
GEOMETRIC_DELAY_S = RANGE_M / SPEED_OF_LIGHT_MPS
DELAY_S = GEOMETRIC_DELAY_S + EXCESS_DELAY_S
TOLERANCES = {
    "elevation_deg": ANGLE_DEG,
    "azimuth_deg": ANGLE_DEG,
    "range_m": RANGE_M,
    "range_rate_mps": RANGE_RATE_MPS,
    "doppler_hz": DOPPLER_HZ,
    "delay_s": DELAY_S,
    "cn0_db_hz": DB,
    "esn0_db": DB,
    "snr_db": DB,
    "gaseous_dry_attenuation_db": DB,
    "gaseous_water_attenuation_db": DB,
    "gaseous_attenuation_db": DB,
    "free_space_cn0_db_hz": DB,
    "geometric_delay_s": GEOMETRIC_DELAY_S,
    "atmospheric_excess_delay_s": EXCESS_DELAY_S,
    "apparent_elevation_deg": APPARENT_ELEVATION_DEG,
    "cloud_attenuation_db": DB,
    "rain_specific_attenuation_db_per_km": DB,
    "rain_path_length_m": RANGE_M,
    "rain_attenuation_db": DB,
    "hydrometeor_attenuation_db": DB,
    "total_atmospheric_attenuation_db": DB,
    # Replayed from the evaluator's Es/N0 with v1's MODCOD rule; see _v1_fixture_errors.
    "margin_db": DB,
}
# v1 single-pass (TraceRow) names of evaluator columns.
V1_TRACE_ROW_NAMES = {
    "propagation_delay_s": "delay_s",
    "carrier_to_noise_density_db_hz": "cn0_db_hz",
    "signal_to_noise_ratio_db": "snr_db",
    "capacity_upper_bound_bps": "shannon_upper_bound_bps",
}
# v1 link fields that are not link-budget values at an instant, so not evaluator columns:
# the record's keys, the adaptation state (replayed and compared exactly below), contact
# bookkeeping derived from visibility (whose sample sets are compared exactly below), and the
# single-pass intermediates that C/N0 is built from.
V1_NOT_COLUMNS = frozenset(
    (
        "time_index",
        "station_index",
        "satellite_index",
        "modcod",
        "rate_bps",
        "margin_db",
        "remaining_contact_s",
        "contact_truncated",
        "free_space_path_loss_db",
        "received_carrier_power_dbw",
        "noise_density_dbw_per_hz",
    )
)


def _shannon_tolerance_bps(bandwidth_hz: float) -> float:
    """The dB tolerance carried through C = B log2(1 + s): |dC/dSNR_dB| <= B log2(10) / 10."""
    return bandwidth_hz * log2(10.0) / 10.0 * DB


@functools.cache
def _v1_fixture(name: str) -> dict:
    return json.loads(gzip.decompress((V1_FIXTURES / f"{name}.json.gz").read_bytes()))


@functools.cache
def _engine(name: str):
    """Scenario, orbits, grid and geometry of an upgraded v1 example."""
    from openleo.engine.catalogs import load_catalogs
    from openleo.engine.geometry import Geometry
    from openleo.engine.timegrid import build_grid

    scenario = load_scenario(_example_path(name))
    orbits = load_catalogs(scenario.space_segment, TIMESCALE)
    grid = build_grid(scenario.time)
    geometry = Geometry(orbits, scenario.ground_segment.stations, TIMESCALE, grid.start_utc)
    return scenario, orbits, grid, geometry


def _error(column: str, actual, expected) -> float:
    difference = np.abs(np.asarray(actual, dtype=np.float64) - np.asarray(expected))
    if column == "azimuth_deg":
        difference = np.minimum(difference % 360.0, 360.0 - difference % 360.0)
    return float(np.max(difference, initial=0.0))


def _replay_v1_modcods(esn0_db, visible, adaptation):
    """v1 ``constellation._links``: select_modcod along grid order, reset when not visible.

    ``esn0_db`` holds the values at the visible samples, in grid order. Returns one MODCOD (or
    None, unlocked) per visible sample.
    """
    values = iter(esn0_db.tolist())
    previous, selected = None, []
    for is_visible in visible.tolist():
        if not is_visible:
            previous = None
            continue
        previous = select_modcod(next(values), adaptation, previous)
        selected.append(previous)
    return selected


def _v1_fixture_errors(name: str):
    """Largest error per v1 field, the tolerance of each, and the exact MODCOD/rate tallies.

    Every pair of the fixture's selection (stations 0-2, satellites 0-9; all of it for the single
    pass) is evaluated at its visible grid samples.
    """
    from openleo.engine.evaluator import LinkEvaluator

    fixture = _v1_fixture(name)
    scenario, orbits, grid, geometry = _engine(name)
    evaluator = LinkEvaluator(scenario, geometry)
    stations = scenario.ground_segment.stations
    index_of = {orbit.satellite.model.satnum: index for index, orbit in enumerate(orbits)}
    adaptation = scenario.adaptation.kernel()
    tolerances = {
        **TOLERANCES,
        "shannon_upper_bound_bps": _shannon_tolerance_bps(scenario.radio.channel_bandwidth_hz),
    }
    links = defaultdict(dict)
    for link in fixture["links"]:
        links[link["station_index"], link["satellite_index"]][link["time_index"]] = link
    v1_fields = {field for link in fixture["links"] for field in link}
    columns = {V1_TRACE_ROW_NAMES.get(field, field) for field in v1_fields} - V1_NOT_COLUMNS
    assert columns <= set(evaluator.columns), columns - set(evaluator.columns)
    # Every v1 field is compared; only v1's single pass lacks a column (it had no Es/N0).
    assert v1_fields - set(V1_TRACE_ROW_NAMES) - set(evaluator.columns) <= V1_NOT_COLUMNS
    assert set(evaluator.columns) - columns == ({"esn0_db"} if name == "iss_cartagena" else set())

    errors = dict.fromkeys(sorted(columns | ({"margin_db"} & v1_fields)), 0.0)
    modcods = {"samples": 0, "modcod_exact": 0, "rate_exact": 0}
    for station in fixture["stations"]:
        for satellite in fixture["satellites"]:
            station_index = station["index"]
            satellite_index = index_of[satellite["norad_id"]]
            pair = links[station_index, satellite["index"]]
            elevation = geometry.elevation_deg(station_index, satellite_index, grid.offsets_s)
            visible = elevation >= stations[station_index].minimum_elevation_deg
            assert np.flatnonzero(visible).tolist() == sorted(pair), (station_index, satellite)
            if not pair:
                continue
            values = evaluator.evaluate(station_index, satellite_index, grid.offsets_s[visible])
            expected = [pair[time_index] for time_index in sorted(pair)]
            for field in v1_fields - V1_NOT_COLUMNS:
                column = V1_TRACE_ROW_NAMES.get(field, field)
                error = _error(column, values[column], [link[field] for link in expected])
                errors[column] = max(errors[column], error)
            if "modcod" not in v1_fields:
                continue
            replayed = _replay_v1_modcods(values["esn0_db"], visible, adaptation)
            for modcod, esn0_db, link in zip(replayed, values["esn0_db"], expected, strict=True):
                rate_bps = (
                    scenario.adaptation.symbol_rate_baud * modcod.efficiency_bits_per_symbol
                    if modcod
                    else 0.0
                )
                margin_db = (
                    esn0_db
                    - scenario.adaptation.implementation_margin_db
                    - (modcod or MODCODS[0]).required_esn0_db
                )
                modcods["samples"] += 1
                modcods["modcod_exact"] += (modcod.name if modcod else None) == link["modcod"]
                modcods["rate_exact"] += rate_bps == link["rate_bps"]
                errors["margin_db"] = max(errors["margin_db"], abs(margin_db - link["margin_db"]))
    return errors, tolerances, modcods, len(fixture["links"])


def _v1_link_budget(scenario, station, terminal, column, hydrometeors, geometry):
    """One sample of v1.0.0's ``constellation._links``/``_radio_metrics``, with scalar kernels.

    A transcription in v1's operation order, kept here because Task 19 retires the v1 module.
    ``geometry`` is (elevation_deg, azimuth_deg, range_m, range_rate_mps).
    """
    elevation_deg, azimuth_deg, range_m, range_rate_mps = geometry
    radio = scenario.radio
    path = column.for_geometry(elevation_deg, range_m) if column else None
    hydrometeor_path = (
        evaluate_hydrometeor_path(
            radio.carrier_frequency_hz, elevation_deg, station.height_amsl_m, hydrometeors
        )
        if hydrometeors
        else None
    )
    path_loss_db = free_space_path_loss_db(range_m, radio.carrier_frequency_hz)
    carrier_power_dbw = received_carrier_power_dbw(
        radio.eirp_dbw, terminal.receive_gain_dbi, path_loss_db, terminal.miscellaneous_loss_db
    )
    free_space_cn0_db_hz = carrier_to_noise_density_db_hz(
        carrier_power_dbw, noise_density_dbw_per_hz(terminal.system_noise_temperature_k)
    )
    total_atmospheric_db = (path.total_db if path else 0.0) + (
        hydrometeor_path.hydrometeor_attenuation_db if hydrometeor_path else 0.0
    )
    cn0_db_hz = free_space_cn0_db_hz - total_atmospheric_db
    geometric_delay_s = propagation_delay_s(range_m)
    snr_db = signal_to_noise_ratio_db(cn0_db_hz, radio.channel_bandwidth_hz)
    link = {
        "elevation_deg": elevation_deg,
        "azimuth_deg": azimuth_deg,
        "range_m": range_m,
        "range_rate_mps": range_rate_mps,
        "doppler_hz": doppler_shift_hz(radio.carrier_frequency_hz, range_rate_mps),
        "delay_s": geometric_delay_s + path.excess_delay_s if path else geometric_delay_s,
        "cn0_db_hz": cn0_db_hz,
        "esn0_db": signal_to_noise_ratio_db(cn0_db_hz, scenario.adaptation.symbol_rate_baud),
        "snr_db": snr_db,
        "shannon_upper_bound_bps": shannon_capacity_upper_bound_bps(
            snr_db, radio.channel_bandwidth_hz
        ),
    }
    if hydrometeor_path:
        link |= asdict(hydrometeor_path) | {
            "total_atmospheric_attenuation_db": total_atmospheric_db
        }
    if path:
        link |= {
            "gaseous_dry_attenuation_db": path.dry_air_db,
            "gaseous_water_attenuation_db": path.water_vapour_db,
            "gaseous_attenuation_db": path.total_db,
            "free_space_cn0_db_hz": free_space_cn0_db_hz,
            "geometric_delay_s": geometric_delay_s,
            "atmospheric_excess_delay_s": path.excess_delay_s,
            "apparent_elevation_deg": path.apparent_elevation_deg,
        }
    return link


def _visible_offsets(geometry, station_index, satellite_index, offsets, mask_deg):
    elevation = geometry.elevation_deg(station_index, satellite_index, offsets)
    return offsets[elevation >= mask_deg]


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_evaluator_matches_v1_link_fixture(name: str) -> None:
    from openleo.engine.evaluator import (
        BASE_COLUMNS,
        HYDROMETEOR_COLUMNS,
        REFERENCE_COLUMNS,
        LinkEvaluator,
    )

    scenario, _, _, geometry = _engine(name)
    propagation = scenario.propagation
    assert LinkEvaluator(scenario, geometry).columns == (
        BASE_COLUMNS
        + (REFERENCE_COLUMNS if propagation.model == "itu_reference" else ())
        + (HYDROMETEOR_COLUMNS if propagation.hydrometeors is not None else ())
    )

    errors, tolerances, modcods, links = _v1_fixture_errors(name)

    assert links > 0
    for column, error in errors.items():
        assert error <= tolerances[column], (column, error, tolerances[column])
    if name != "iss_cartagena":
        assert modcods["samples"] == links
        assert modcods["modcod_exact"] == modcods["rate_exact"] == links


def test_available_db_equals_esn0_minus_margin() -> None:
    from openleo.engine.evaluator import LinkEvaluator

    scenario, _, grid, geometry = _engine("iridium_global_hydrometeors")
    evaluator = LinkEvaluator(scenario, geometry)
    margin_db = scenario.adaptation.implementation_margin_db
    assert margin_db > 0.0
    offsets = np.concatenate([grid.offsets_s, grid.offsets_s[:-1] + 17.25])
    checked = 0
    for station_index in range(len(scenario.ground_segment.stations)):
        mask_deg = scenario.ground_segment.stations[station_index].minimum_elevation_deg
        for satellite_index in range(4):
            visible = _visible_offsets(geometry, station_index, satellite_index, offsets, mask_deg)
            esn0_db = evaluator.evaluate(station_index, satellite_index, visible)["esn0_db"]
            available_db = evaluator.available_db(station_index, satellite_index, visible)
            # select_modcod's own subtraction, so thresholds see identical numbers.
            np.testing.assert_array_equal(available_db, esn0_db - margin_db)
            checked += visible.size
    assert checked > 10


@pytest.mark.parametrize("refinement", [1, 2])
def test_evaluate_off_grid_matches_the_v1_link_budget(refinement: int) -> None:
    """Root-finding and quadrature evaluate between grid points; v1 arithmetic must hold there.

    Quito gets its own terminal, so each station's terminal and AMSL column are checked.
    """
    from openleo.engine.catalogs import load_catalogs
    from openleo.engine.evaluator import LinkEvaluator
    from openleo.engine.geometry import Geometry

    base = load_scenario(ROOT / "examples" / "constellations" / "iridium_global_hydrometeors.json")
    stations = base.ground_segment.stations
    stations = (*stations[:3], replace(stations[3], terminal="dish"))
    scenario = replace(
        base,
        terminals=(*base.terminals, ("dish", Terminal(32.5, 150.0, 0.7))),
        ground_segment=replace(base.ground_segment, stations=stations),
        propagation=replace(base.propagation, refinement=refinement),
    )
    orbits = load_catalogs(scenario.space_segment, TIMESCALE)
    geometry = Geometry(orbits, stations, TIMESCALE, scenario.time.start_utc)
    evaluator = LinkEvaluator(scenario, geometry)
    frequency_hz = scenario.radio.carrier_frequency_hz
    hydrometeors = dict(scenario.propagation.hydrometeors.stations)
    tolerances = {
        **TOLERANCES,
        "shannon_upper_bound_bps": _shannon_tolerance_bps(scenario.radio.channel_bandwidth_hz),
    }
    offsets = np.arange(0.0, 7200.0, 61.3) + 0.123457

    for station_index, station in enumerate(stations):
        terminal = scenario.terminal_for(station)
        column = build_reference_column(
            frequency_hz, station.height_amsl_m / 1000, refinement=refinement
        )
        compared = 0
        for satellite_index in range(len(orbits)):
            visible = _visible_offsets(
                geometry, station_index, satellite_index, offsets, station.minimum_elevation_deg
            )[:3]
            if visible.size == 0:
                continue
            values = evaluator.evaluate(station_index, satellite_index, visible)
            assert tuple(values) == evaluator.columns
            for sample in range(visible.size):
                expected = _v1_link_budget(
                    scenario,
                    station,
                    terminal,
                    column,
                    hydrometeors[station.name],
                    tuple(
                        float(values[name][sample])
                        for name in ("elevation_deg", "azimuth_deg", "range_m", "range_rate_mps")
                    ),
                )
                assert set(expected) == set(evaluator.columns)
                for name, value in expected.items():
                    error = abs(float(values[name][sample]) - value)
                    assert error <= tolerances[name], (station.name, name, error)
            compared += visible.size
            if compared >= 6:
                break
        assert compared >= 6, station.name


def test_evaluate_rejects_offsets_below_the_station_mask() -> None:
    from openleo.engine.evaluator import LinkEvaluator

    scenario, orbits, grid, geometry = _engine("iridium_global_reference")
    evaluator = LinkEvaluator(scenario, geometry)
    elevation = geometry.elevation_deg(TROMSO, 0, grid.offsets_s)
    above = grid.offsets_s[elevation >= 10.0][0]
    below_index = int(np.flatnonzero(elevation < 10.0)[-1])
    below = grid.offsets_s[below_index]
    timestamp = _v1_fixture("iridium_global_reference")["timestamps_utc"][below_index]

    evaluator.evaluate(TROMSO, 0, [above])
    with pytest.raises(ValueError) as caught:
        evaluator.evaluate(TROMSO, 0, [above, below])
    message = str(caught.value)
    assert "Tromso" in message
    assert f"NORAD {orbits[0].satellite.model.satnum}" in message
    assert timestamp in message
    assert "10.0 deg" in message
    assert "np.float64" not in message
    assert f"is at {float(elevation[below_index])!r} deg" in message


def test_evaluate_accepts_scalar_and_empty_offsets() -> None:
    from openleo.engine.evaluator import LinkEvaluator

    scenario, _, grid, geometry = _engine("iridium_global_hydrometeors")
    evaluator = LinkEvaluator(scenario, geometry)

    empty = evaluator.evaluate(1, 2, np.array([]))
    assert tuple(empty) == evaluator.columns
    assert all(values.shape == (0,) for values in empty.values())
    assert evaluator.available_db(1, 2, []).shape == (0,)

    visible = _visible_offsets(geometry, TROMSO, 0, grid.offsets_s, 10.0)
    one = evaluator.evaluate(TROMSO, 0, float(visible[0]))
    many = evaluator.evaluate(TROMSO, 0, visible)
    tolerances = {
        **TOLERANCES,
        "shannon_upper_bound_bps": _shannon_tolerance_bps(scenario.radio.channel_bandwidth_hz),
    }
    for name in evaluator.columns:
        assert one[name].shape == (1,)
        assert one[name].dtype == np.float64
        assert not one[name].flags.writeable
        assert not many[name].flags.writeable
        # One sample or a batch: SIMD lanes may differ from a lone element by an ulp.
        assert abs(one[name][0] - many[name][0]) <= tolerances[name], name


def test_evaluator_rejects_inconsistent_inputs() -> None:
    from openleo.engine.evaluator import LinkEvaluator
    from openleo.engine.geometry import Geometry

    scenario, orbits, _, geometry = _engine("iridium_global_hydrometeors")
    stations = scenario.ground_segment.stations
    start = scenario.time.start_utc

    with pytest.raises(ValueError, match="geometry"):
        LinkEvaluator(scenario, Geometry(orbits, stations[::-1], TIMESCALE, start))
    no_height = (replace(stations[0], height_amsl_m=None), *stations[1:])
    with pytest.raises(ValueError, match="^/ground_segment/stations/0/height_amsl_m"):
        LinkEvaluator(
            replace(scenario, ground_segment=replace(scenario.ground_segment, stations=no_height)),
            Geometry(orbits, no_height, TIMESCALE, start),
        )
    with pytest.raises(ValueError, match="^/propagation/hydrometeors.*itu_reference"):
        LinkEvaluator(
            replace(scenario, propagation=replace(scenario.propagation, model="free_space")),
            geometry,
        )
    partial = replace(
        scenario.propagation.hydrometeors, stations=scenario.propagation.hydrometeors.stations[:3]
    )
    with pytest.raises(ValueError, match="^/propagation/hydrometeors/stations.*Quito"):
        LinkEvaluator(
            replace(scenario, propagation=replace(scenario.propagation, hydrometeors=partial)),
            geometry,
        )
    with pytest.raises(ValueError, match="names unknown terminal"):
        LinkEvaluator(replace(scenario, terminals=()), geometry)

    evaluator = LinkEvaluator(scenario, geometry)
    for station_index, satellite_index in ((4, 0), (-1, 0), (True, 0), (0.0, 0), (0, len(orbits))):
        with pytest.raises(ValueError, match="index"):
            evaluator.evaluate(station_index, satellite_index, [0.0])
    evaluator.evaluate(np.int64(3), np.int64(len(orbits) - 1), [])


def test_non_finite_link_values_rejected(monkeypatch) -> None:
    import openleo.engine.evaluator as evaluator_module
    from openleo.engine.evaluator import LinkEvaluator

    scenario, _, grid, geometry = _engine("iridium_global_hydrometeors")
    visible = _visible_offsets(geometry, TROMSO, 0, grid.offsets_s, 10.0)
    assert visible.size
    LinkEvaluator(scenario, geometry).evaluate(TROMSO, 0, visible)

    # v1 refuses a Shannon bound that overflows (absurd but finite radio inputs).
    absurd = replace(
        scenario, radio=replace(scenario.radio, eirp_dbw=10_000.0, channel_bandwidth_hz=1e306)
    )
    with pytest.raises(ValueError, match="shannon_upper_bound_bps.*finite"):
        LinkEvaluator(absurd, geometry).evaluate(TROMSO, 0, visible)

    # v1 refuses a non-finite total atmospheric loss; the kernels never produce one.
    evaluate_path = evaluator_module.evaluate_hydrometeor_path_array

    def infinite(*args):
        path = evaluate_path(*args)
        return replace(
            path, hydrometeor_attenuation_db=np.full_like(path.rain_attenuation_db, np.inf)
        )

    monkeypatch.setattr(evaluator_module, "evaluate_hydrometeor_path_array", infinite)
    with pytest.raises(ValueError, match="total_atmospheric_attenuation_db.*finite"):
        LinkEvaluator(scenario, geometry).evaluate(TROMSO, 0, visible)


@pytest.mark.parametrize("name", ["iridium_global", "iridium_global_hydrometeors"])
def test_tabulated_integrand_matches_the_exact_link_budget(name: str) -> None:
    """The Shannon integrand and MODCOD inputs read the gas table: within 1e-10 dB of the ray
    trace, and the same arithmetic otherwise (free space is identical)."""
    from openleo.engine.evaluator import LinkEvaluator

    scenario, _, grid, geometry = _engine(name)
    evaluator = LinkEvaluator(scenario, geometry)
    reference = scenario.propagation.model == "itu_reference"
    bounds = evaluator.gas_tables
    assert len(bounds) == len(scenario.ground_segment.stations)
    if reference:
        assert all(bound is not None and bound <= 1e-10 for bound in bounds)
    else:
        assert bounds == (None,) * len(bounds)
    margin_db = scenario.adaptation.implementation_margin_db
    offsets = np.concatenate([grid.offsets_s, grid.offsets_s[:-1] + 17.25])
    checked = 0
    for station_index, station in enumerate(scenario.ground_segment.stations):
        for satellite_index in range(6):
            visible = _visible_offsets(
                geometry, station_index, satellite_index, offsets, station.minimum_elevation_deg
            )
            exact = evaluator.evaluate(station_index, satellite_index, visible)
            available = evaluator.tabulated_available_db(station_index, satellite_index, visible)
            shannon = evaluator.tabulated_shannon_bps(station_index, satellite_index, visible)
            if reference:
                np.testing.assert_allclose(
                    available, exact["esn0_db"] - margin_db, rtol=0, atol=1e-10
                )
                np.testing.assert_allclose(
                    shannon, exact["shannon_upper_bound_bps"], rtol=1e-10, atol=0
                )
            else:
                np.testing.assert_array_equal(available, exact["esn0_db"] - margin_db)
                np.testing.assert_array_equal(shannon, exact["shannon_upper_bound_bps"])
            checked += visible.size
    assert checked > 10


def test_unverified_gas_tables_fall_back_to_the_ray_trace() -> None:
    from openleo.engine.evaluator import LinkEvaluator

    scenario, _, grid, geometry = _engine("iridium_global_reference")
    oxygen = replace(scenario, radio=replace(scenario.radio, carrier_frequency_hz=60e9))
    evaluator = LinkEvaluator(oxygen, geometry)
    assert evaluator.gas_tables == (None,) * len(scenario.ground_segment.stations)
    # With events off nothing reads the tables, so none is built.
    events_off = replace(
        scenario, time=replace(scenario.time, events=replace(scenario.time.events, enabled=False))
    )
    assert LinkEvaluator(events_off, geometry).gas_tables == (None,) * 4
    assert all(station.table is None for station in LinkEvaluator(events_off, geometry)._stations)
    station = scenario.ground_segment.stations[0]
    for satellite_index in range(len(geometry.orbits)):
        visible = _visible_offsets(
            geometry, 0, satellite_index, grid.offsets_s, station.minimum_elevation_deg
        )
        if visible.size:
            break
    np.testing.assert_array_equal(
        evaluator.tabulated_shannon_bps(0, satellite_index, visible),
        evaluator.evaluate(0, satellite_index, visible)["shannon_upper_bound_bps"],
    )
