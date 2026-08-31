import csv
from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha256
from itertools import pairwise
from math import asin, atan2, cos, degrees, radians, sin, sqrt
from pathlib import Path

import pytest
from skyfield.api import load, wgs84
from skyfield.framelib import itrs

from openleo.input import load_scenario
from openleo.model import OrbitSource, Provenance, TimeWindow
from openleo.orbit import load_orbit

FROZEN_ORBIT = Path("examples/data/iss_2026-08-30.csv")
FROZEN_SCENARIO = Path("examples/scenarios/iss_cartagena.json")
FROZEN_SHA256 = "ddb21d9a4a3a4812ee397751555b9d55d2767f30c33190afba90e4bbb8db13f9"


def _source(path: Path, checksum: str | None = None) -> OrbitSource:
    return OrbitSource(
        path=path,
        provenance=Provenance(
            source_url="https://celestrak.org/NORAD/elements/gp.php?CATNR=25544&FORMAT=CSV",
            retrieved_at_utc=datetime(2026, 8, 30, 22, 1, 11, tzinfo=UTC),
            terms_url="https://celestrak.org/usage-policy.php",
            sha256=checksum or sha256(path.read_bytes()).hexdigest(),
        ),
    )


def _copy_orbit(tmp_path: Path, rows: list[dict[str, str]]) -> Path:
    path = tmp_path / "orbit.csv"
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    return path


def _frozen_rows() -> list[dict[str, str]]:
    with FROZEN_ORBIT.open(encoding="utf-8", newline="") as file:
        return list(csv.DictReader(file))


def _scenario_with_window(
    start_utc: datetime,
    stop_utc: datetime,
    *,
    step_s: float = 10.0,
):
    scenario = load_scenario(FROZEN_SCENARIO)
    return replace(
        scenario,
        time_window=TimeWindow(
            start_utc=start_utc,
            stop_utc=stop_utc,
            step_s=step_s,
            minimum_elevation_deg=scenario.time_window.minimum_elevation_deg,
        ),
    )


def test_load_orbit_accepts_frozen_gp_record() -> None:
    loaded = load_orbit(_source(FROZEN_ORBIT, FROZEN_SHA256), load.timescale(builtin=True))

    assert loaded.satellite.name == "ISS (ZARYA)"
    assert loaded.actual_sha256 == FROZEN_SHA256
    assert loaded.epoch_utc.isoformat() == "2026-08-30T11:57:48.556224+00:00"


def test_frozen_orbit_matches_skyfield_vallado_epoch_state() -> None:
    loaded = load_orbit(_source(FROZEN_ORBIT, FROZEN_SHA256), load.timescale(builtin=True))
    epoch_state = loaded.satellite.at(loaded.satellite.epoch)

    assert epoch_state.position.km == pytest.approx(
        [2541.22048664, -6305.90893063, -6.41569470],
        abs=1e-8,
    )
    assert epoch_state.velocity.km_per_s == pytest.approx(
        [4.41995972, 1.78637061, 5.99594561],
        abs=1e-8,
    )


def test_load_orbit_rejects_checksum_mismatch(tmp_path) -> None:
    changed = tmp_path / "changed.csv"
    changed.write_bytes(FROZEN_ORBIT.read_bytes().replace(b"ISS (ZARYA)", b"ISS (ZARYB)"))

    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        load_orbit(_source(changed, FROZEN_SHA256), load.timescale(builtin=True))


def test_load_orbit_rejects_missing_required_gp_fields(tmp_path) -> None:
    rows = _frozen_rows()
    rows[0].pop("MEAN_MOTION")
    path = _copy_orbit(tmp_path, rows)

    with pytest.raises(ValueError, match="missing required GP fields: MEAN_MOTION"):
        load_orbit(_source(path), load.timescale(builtin=True))


def test_load_orbit_rejects_multiple_gp_records(tmp_path) -> None:
    rows = _frozen_rows()
    path = _copy_orbit(tmp_path, rows * 2)

    with pytest.raises(ValueError, match="exactly one GP record"):
        load_orbit(_source(path), load.timescale(builtin=True))


def test_load_orbit_rejects_oversized_gp_csv_before_hashing_or_decoding(tmp_path) -> None:
    path = tmp_path / "oversized-orbit.csv"
    path.write_bytes(b" " * 1_000_001)

    with pytest.raises(ValueError) as exc_info:
        load_orbit(_source(path, "0" * 64), load.timescale(builtin=True))

    assert str(path) in str(exc_info.value)
    assert "GP CSV exceeds 1000000 bytes" in str(exc_info.value)


def test_simulate_scenario_reproduces_frozen_pass() -> None:
    from openleo.simulation import simulate_scenario

    scenario = load_scenario(FROZEN_SCENARIO)
    result = simulate_scenario(scenario)

    assert result.scenario is scenario
    assert len(result.rows) == 40
    assert result.summary.sampled_aos_utc.isoformat() == "2026-08-30T06:15:30+00:00"
    assert result.summary.sampled_los_utc.isoformat() == "2026-08-30T06:22:00+00:00"
    assert result.summary.sampling_interval_s == 10.0
    assert result.summary.element_epoch_utc.isoformat() == "2026-08-30T11:57:48.556224+00:00"
    assert result.summary.start_element_age_days == pytest.approx(-0.23875643777777777)
    assert result.summary.stop_element_age_days == pytest.approx(-0.23181199333333333)
    assert result.summary.maximum_absolute_element_age_days == pytest.approx(0.23875643777777777)
    assert result.summary.leap_second_table_source == "skyfield-builtin"
    assert len(result.summary.leap_second_table_sha256) == 64
    assert set(result.summary.leap_second_table_sha256) <= set("0123456789abcdef")
    assert result.summary.sampled_duration_s == 390.0
    assert result.summary.maximum_elevation_deg == pytest.approx(61.5799734359, abs=1e-6)
    assert result.summary.minimum_range_m == pytest.approx(473_676.7193, abs=0.5)
    assert result.summary.maximum_capacity_upper_bound_bps == pytest.approx(
        6_336_955.3241, rel=1e-7
    )
    assert result.summary.integrated_capacity_upper_bound_bits == pytest.approx(
        1_867_325_705.98, rel=1e-7
    )

    closest = result.rows[20]
    assert closest.timestamp_utc.isoformat() == "2026-08-30T06:18:50+00:00"
    assert closest.elevation_deg == pytest.approx(61.5799734359, abs=1e-6)
    assert closest.range_m == pytest.approx(473_676.7193, abs=0.5)
    assert closest.range_rate_mps == pytest.approx(37.1481223, abs=0.01)
    assert closest.doppler_hz == pytest.approx(-272.6081553, abs=0.01)
    assert closest.free_space_path_loss_db == pytest.approx(152.805877641, abs=1e-8)

    with pytest.raises(TypeError):
        type(result)(rows=result.rows, summary=result.summary)


def test_frozen_pass_geometry_has_consistent_range_rate_and_doppler_signs() -> None:
    from openleo.simulation import simulate_scenario

    rows = simulate_scenario(load_scenario(FROZEN_SCENARIO)).rows
    ranges = [row.range_m for row in rows]
    closest_index = ranges.index(min(ranges))
    finite_difference_mps = (ranges[closest_index + 1] - ranges[closest_index - 1]) / 20.0

    assert rows[closest_index].range_rate_mps == pytest.approx(
        finite_difference_mps,
        abs=0.5,
    )
    assert rows[0].range_rate_mps < 0.0
    assert rows[0].doppler_hz > 0.0
    assert rows[-1].range_rate_mps > 0.0
    assert rows[-1].doppler_hz < 0.0
    assert all(left > right for left, right in pairwise(ranges[: closest_index + 1]))
    assert all(left < right for left, right in pairwise(ranges[closest_index:]))
    assert ranges.count(ranges[closest_index]) == 1


def test_closest_approach_topocentric_geometry_matches_independent_enu() -> None:
    from openleo.simulation import simulate_scenario

    scenario = load_scenario(FROZEN_SCENARIO)
    closest = min(simulate_scenario(scenario).rows, key=lambda row: row.range_m)
    timescale = load.timescale(builtin=True)
    orbit = load_orbit(scenario.orbit, timescale)
    station = wgs84.latlon(
        scenario.ground_station.latitude_deg,
        scenario.ground_station.longitude_deg,
        elevation_m=scenario.ground_station.height_m,
    )
    spacecraft_ecef_m = (
        orbit.satellite.at(timescale.from_datetime(closest.timestamp_utc)).frame_xyz(itrs).m
    )
    station_ecef_m = station.itrs_xyz.m
    delta_x_m, delta_y_m, delta_z_m = (
        spacecraft_ecef_m[index] - station_ecef_m[index] for index in range(3)
    )
    latitude_rad = radians(scenario.ground_station.latitude_deg)
    longitude_rad = radians(scenario.ground_station.longitude_deg)

    east_m = -sin(longitude_rad) * delta_x_m + cos(longitude_rad) * delta_y_m
    north_m = (
        -sin(latitude_rad) * cos(longitude_rad) * delta_x_m
        - sin(latitude_rad) * sin(longitude_rad) * delta_y_m
        + cos(latitude_rad) * delta_z_m
    )
    up_m = (
        cos(latitude_rad) * cos(longitude_rad) * delta_x_m
        + cos(latitude_rad) * sin(longitude_rad) * delta_y_m
        + sin(latitude_rad) * delta_z_m
    )
    range_m = sqrt(east_m**2 + north_m**2 + up_m**2)
    elevation_deg = degrees(asin(up_m / range_m))
    azimuth_deg = degrees(atan2(east_m, north_m)) % 360.0

    assert closest.range_m == pytest.approx(range_m, rel=0.0, abs=1e-5)
    assert closest.elevation_deg == pytest.approx(elevation_deg, rel=0.0, abs=1e-9)
    assert closest.azimuth_deg == pytest.approx(azimuth_deg, rel=0.0, abs=1e-9)


def test_simulation_rejects_oversized_grid_before_timestamp_iteration(monkeypatch) -> None:
    from openleo.simulation import simulate_scenario

    scenario = _scenario_with_window(
        datetime(2026, 8, 30, 6, 14, tzinfo=UTC),
        datetime(2026, 8, 30, 6, 14, 1, tzinfo=UTC),
        step_s=1e-5,
    )

    def fail_if_called(_scenario):
        pytest.fail("timestamp loop entered for rejected grid")

    monkeypatch.setattr("openleo.simulation._timestamps", fail_if_called)
    with pytest.raises(ValueError, match=r"100001.*100000"):
        simulate_scenario(scenario)


def test_simulation_rejects_programmatic_reversed_time_window() -> None:
    from openleo.simulation import simulate_scenario

    scenario = _scenario_with_window(
        datetime(2026, 8, 30, 6, 15, tzinfo=UTC),
        datetime(2026, 8, 30, 6, 14, tzinfo=UTC),
    )

    with pytest.raises(ValueError, match="stop_utc must be after"):
        simulate_scenario(scenario)


def test_simulation_rejects_programmatic_step_larger_than_window() -> None:
    from openleo.simulation import simulate_scenario

    scenario = _scenario_with_window(
        datetime(2026, 8, 30, 6, 14, tzinfo=UTC),
        datetime(2026, 8, 30, 6, 14, 5, tzinfo=UTC),
        step_s=10.0,
    )

    with pytest.raises(ValueError, match="step_s=.*no larger than the window duration"):
        simulate_scenario(scenario)


def test_simulation_rejects_programmatic_naive_time_window() -> None:
    from openleo.simulation import simulate_scenario

    naive_start = datetime(2026, 8, 30, 6, 14)  # noqa: DTZ001 - invalid input under test
    naive_stop = datetime(2026, 8, 30, 6, 24)  # noqa: DTZ001 - invalid input under test
    scenario = _scenario_with_window(
        naive_start,
        naive_stop,
    )

    with pytest.raises(ValueError, match="timezone-aware UTC"):
        simulate_scenario(scenario)


def test_simulate_scenario_rejects_no_visible_pass() -> None:
    from openleo.simulation import simulate_scenario

    scenario = _scenario_with_window(
        datetime(2026, 8, 30, 6, 0, tzinfo=UTC),
        datetime(2026, 8, 30, 6, 5, tzinfo=UTC),
    )

    with pytest.raises(ValueError, match="no visible pass"):
        simulate_scenario(scenario)


def test_simulate_scenario_rejects_window_starting_during_pass() -> None:
    from openleo.simulation import simulate_scenario

    scenario = _scenario_with_window(
        datetime(2026, 8, 30, 6, 16, tzinfo=UTC),
        datetime(2026, 8, 30, 6, 24, tzinfo=UTC),
    )

    with pytest.raises(ValueError, match="starts during a visible pass"):
        simulate_scenario(scenario)


def test_simulate_scenario_rejects_window_ending_during_pass() -> None:
    from openleo.simulation import simulate_scenario

    scenario = _scenario_with_window(
        datetime(2026, 8, 30, 6, 14, tzinfo=UTC),
        datetime(2026, 8, 30, 6, 20, tzinfo=UTC),
    )

    with pytest.raises(ValueError, match="ends during a visible pass"):
        simulate_scenario(scenario)


def test_simulate_scenario_rejects_multiple_visible_segments() -> None:
    from openleo.simulation import simulate_scenario

    scenario = _scenario_with_window(
        datetime(2026, 8, 30, 0, 0, tzinfo=UTC),
        datetime(2026, 8, 30, 23, 59, tzinfo=UTC),
        step_s=60.0,
    )

    with pytest.raises(ValueError, match="exactly one visible pass"):
        simulate_scenario(scenario)


def test_simulate_scenario_warns_when_window_is_far_from_element_epoch() -> None:
    from openleo.simulation import simulate_scenario

    scenario = _scenario_with_window(
        datetime(2026, 9, 20, 14, 0, tzinfo=UTC),
        datetime(2026, 9, 20, 14, 50, tzinfo=UTC),
        step_s=60.0,
    )

    result = simulate_scenario(scenario)

    assert result.summary.warnings == (
        "maximum requested time is more than 14 days from the element epoch",
    )


def test_public_api_exports_simulation_types() -> None:
    from openleo import (
        LoadedOrbit,
        SimulationResult,
        SimulationSummary,
        TraceRow,
        load_orbit,
        simulate_scenario,
    )

    assert LoadedOrbit.__name__ == "LoadedOrbit"
    assert TraceRow.__name__ == "TraceRow"
    assert SimulationSummary.__name__ == "SimulationSummary"
    assert SimulationResult.__name__ == "SimulationResult"
    assert callable(load_orbit)
    assert callable(simulate_scenario)
