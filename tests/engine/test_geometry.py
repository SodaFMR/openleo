"""Batched satellite and topocentric geometry, checked against frozen OpenLEO v1.0.0 numbers."""

import csv
import functools
import gzip
import json
import re
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path

import numpy as np
import pytest
from skyfield.api import load, wgs84

from openleo.scenario import load_scenario
from openleo.scenario.model import CatalogSource, SpaceSegment, Station

ROOT = Path(__file__).resolve().parents[2]
V1_FIXTURES = ROOT / "tests" / "fixtures" / "v1"
V2_EXAMPLES = ROOT / "examples" / "v2"
ISS_CSV = ROOT / "examples" / "data" / "iss_2026-08-30.csv"
FIXTURE_NAMES = (
    "iss_cartagena",
    "iridium_global",
    "iridium_global_reference",
    "iridium_global_hydrometeors",
)
TIMESCALE = load.timescale(builtin=True)

# v1-equivalence tolerances (plan, Global Constraints).
ANGLE_DEG = 1e-8
RANGE_M = 1e-4
RANGE_RATE_MPS = 1e-6
SATELLITE_ECEF_M = 1e-4


@functools.cache
def _v1_fixture(name: str) -> dict:
    return json.loads(gzip.decompress((V1_FIXTURES / f"{name}.json.gz").read_bytes()))


def _max_error(actual, expected) -> float:
    return float(np.max(np.abs(np.asarray(actual) - np.asarray(expected)), initial=0.0))


def _max_angle_error(actual, expected) -> float:
    difference = np.abs(np.asarray(actual) - np.asarray(expected)) % 360.0
    return float(np.max(np.minimum(difference, 360.0 - difference), initial=0.0))


def _station(name: str, latitude_deg: float, longitude_deg: float, height_m: float) -> Station:
    return Station(name, latitude_deg, longitude_deg, height_m, None, "default", 10.0)


def _iss_orbits(tmp_path: Path, **fields: str):
    from openleo.engine.catalogs import load_catalogs

    with ISS_CSV.open(encoding="utf-8", newline="") as stream:
        row = next(csv.DictReader(stream))
    path = tmp_path / "iss.csv"
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(row))
        writer.writeheader()
        writer.writerow({**row, **fields})
    source = CatalogSource(
        path=path,
        format="celestrak_gp_csv",
        source_url="https://celestrak.org/NORAD/elements/gp.php?CATNR=25544&FORMAT=CSV",
        retrieved_at_utc=datetime(2026, 8, 30, 22, 1, 11, tzinfo=UTC),
        terms_url="https://celestrak.org/usage-policy.php",
        sha256=sha256(path.read_bytes()).hexdigest(),
    )
    return load_catalogs(SpaceSegment((source,), None, True), TIMESCALE)


def test_v1_fixtures_match_the_recorded_json_sha256() -> None:
    metadata = json.loads((V1_FIXTURES / "metadata.json").read_text(encoding="utf-8"))
    recorded = metadata["fixture_json_sha256"]

    assert sorted(recorded) == sorted(f"{name}.json.gz" for name in FIXTURE_NAMES)
    for file_name, digest in recorded.items():
        payload = gzip.decompress((V1_FIXTURES / file_name).read_bytes())
        assert sha256(payload).hexdigest() == digest, file_name
        # The payload is canonical JSON, so decoding and re-encoding reproduces it.
        canonical = json.dumps(
            json.loads(payload),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        assert canonical == payload, file_name


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_geometry_matches_v1_fixture(name: str) -> None:
    from openleo.engine.catalogs import load_catalogs
    from openleo.engine.geometry import Geometry
    from openleo.engine.timegrid import build_grid, utc_text

    fixture = _v1_fixture(name)
    scenario = load_scenario(V2_EXAMPLES / f"{name}.json")
    orbits = load_catalogs(scenario.space_segment, TIMESCALE)
    grid = build_grid(scenario.time)
    stations = scenario.ground_segment.stations
    geometry = Geometry(orbits, stations, TIMESCALE, grid.start_utc)
    index_of = {orbit.satellite.model.satnum: index for index, orbit in enumerate(orbits)}
    norad_of = {satellite["index"]: satellite["norad_id"] for satellite in fixture["satellites"]}

    assert [utc_text(grid.start_utc, offset) for offset in grid.offsets_s] == fixture[
        "timestamps_utc"
    ]
    assert geometry.station_ecef_m.shape == (len(stations), 3)
    for station in fixture["stations"]:
        assert stations[station["index"]].name == station["name"]
        error = _max_error(geometry.station_ecef_m[station["index"]], station["position_ecef_m"])
        assert error <= SATELLITE_ECEF_M, (station["name"], error)

    for satellite in fixture["satellites"]:
        position, velocity = geometry.satellite_ecef(
            index_of[satellite["norad_id"]], grid.offsets_s
        )
        assert position.shape == velocity.shape == (len(grid.offsets_s), 3)
        error = _max_error(position, satellite["positions_ecef_m"])
        assert error <= SATELLITE_ECEF_M, (satellite["norad_id"], error)

    pairs = defaultdict(list)
    for link in fixture["links"]:
        pairs[link["station_index"], link["satellite_index"]].append(link)
    assert pairs
    for (station_index, v1_satellite_index), links in pairs.items():
        times = np.array([link["time_index"] for link in links])
        topocentric = geometry.topocentric(
            station_index, index_of[norad_of[v1_satellite_index]], grid.offsets_s[times]
        )
        for actual, field, tolerance, measure in (
            (topocentric.elevation_deg, "elevation_deg", ANGLE_DEG, _max_error),
            (topocentric.azimuth_deg, "azimuth_deg", ANGLE_DEG, _max_angle_error),
            (topocentric.range_m, "range_m", RANGE_M, _max_error),
            (topocentric.range_rate_mps, "range_rate_mps", RANGE_RATE_MPS, _max_error),
        ):
            error = measure(actual, [link[field] for link in links])
            assert error <= tolerance, (station_index, v1_satellite_index, field, error)


def test_topocentric_matches_the_v1_skyfield_chain_off_grid() -> None:
    """All satellites and stations, below the horizon too, at times off the sample grid."""
    from openleo.engine.catalogs import load_catalogs
    from openleo.engine.geometry import Geometry
    from openleo.engine.timegrid import skyfield_times

    scenario = load_scenario(V2_EXAMPLES / "iridium_global.json")
    orbits = load_catalogs(scenario.space_segment, TIMESCALE)
    start = scenario.time.start_utc
    stations = scenario.ground_segment.stations
    geometry = Geometry(orbits, stations, TIMESCALE, start)
    offsets = np.linspace(0.0, 7200.0, 25) + 0.123457
    times = skyfield_times(TIMESCALE, start, offsets)

    for station_index, station in enumerate(stations):
        site = wgs84.latlon(
            station.latitude_deg, station.longitude_deg, elevation_m=station.height_ellipsoid_m
        )
        site_state = site.at(times)
        for satellite_index, orbit in enumerate(orbits):
            with np.errstate(over="ignore", invalid="ignore", divide="ignore"):
                elevation, azimuth, distance, _, _, range_rate = (
                    orbit.satellite.at(times) - site_state
                ).frame_latlon_and_rates(site)
            topocentric = geometry.topocentric(station_index, satellite_index, offsets)
            assert _max_error(topocentric.elevation_deg, elevation.degrees) <= ANGLE_DEG
            assert _max_angle_error(topocentric.azimuth_deg, azimuth.degrees) <= ANGLE_DEG
            assert _max_error(topocentric.range_m, distance.m) <= RANGE_M
            assert _max_error(topocentric.range_rate_mps, range_rate.m_per_s) <= RANGE_RATE_MPS
            np.testing.assert_array_equal(
                geometry.elevation_deg(station_index, satellite_index, offsets),
                topocentric.elevation_deg,
            )


def test_zenith_and_polar_station_finite(tmp_path: Path, monkeypatch) -> None:
    from openleo.engine.geometry import Geometry

    orbits = _iss_orbits(tmp_path)
    start = datetime(2026, 8, 30, 6, 0, tzinfo=UTC)
    stations = (
        _station("North Pole", 90.0, 0.0, 0.0),
        _station("South Pole", -90.0, 45.0, 2835.0),
        _station("Madrid", 40.4168, -3.7038, 650.0),
    )
    geometry = Geometry(orbits, stations, TIMESCALE, start)

    # Real geometry from both poles: every value finite, angles in range.
    offsets = np.arange(0.0, 6 * 3600.0, 30.0)
    for station_index in (0, 1):
        topocentric = geometry.topocentric(station_index, 0, offsets)
        for values in (
            topocentric.elevation_deg,
            topocentric.azimuth_deg,
            topocentric.range_m,
            topocentric.range_rate_mps,
        ):
            assert np.isfinite(values).all()
        assert ((-90.0 <= topocentric.elevation_deg) & (topocentric.elevation_deg <= 90.0)).all()
        assert ((0.0 <= topocentric.azimuth_deg) & (topocentric.azimuth_deg < 360.0)).all()

    # Synthetic: a satellite exactly on the station's geodetic normal, rho parallel to up.
    heights_m = np.array([100.0, 400e3, 1_000e3, 36_000e3])
    for station_index, station in enumerate(stations):
        latitude, longitude = np.radians([station.latitude_deg, station.longitude_deg])
        up = np.array(
            [
                np.cos(latitude) * np.cos(longitude),
                np.cos(latitude) * np.sin(longitude),
                np.sin(latitude),
            ]
        )
        position = geometry.station_ecef_m[station_index] + heights_m[:, None] * up
        velocity = np.tile([1000.0, -2000.0, 500.0], (len(heights_m), 1))
        monkeypatch.setattr(
            geometry, "satellite_ecef", lambda _index, _offsets, p=position, v=velocity: (p, v)
        )
        topocentric = geometry.topocentric(station_index, 0, np.zeros(len(heights_m)))

        # Adding rho to a station ~6.4e6 m from the geocentre rounds it by ~1e-9 m, which bounds
        # how close to the normal the synthetic satellite is.
        assert topocentric.azimuth_deg.tolist() == [0.0] * len(heights_m)
        np.testing.assert_allclose(topocentric.elevation_deg, 90.0, rtol=0, atol=1e-8)
        np.testing.assert_allclose(topocentric.range_m, heights_m, rtol=0, atol=RANGE_M)
        np.testing.assert_allclose(
            topocentric.range_rate_mps, velocity @ up, rtol=0, atol=RANGE_RATE_MPS
        )
        assert np.isfinite(topocentric.elevation_deg).all()


def test_sgp4_error_names_norad_and_time(tmp_path: Path) -> None:
    from openleo.engine.geometry import Geometry

    # A large drag term passes SGP4 initialisation at the epoch, then decays mid-window.
    # (An eccentricity of 1 or more never gets this far: the catalog loader rejects it.)
    orbits = _iss_orbits(tmp_path, BSTAR="0.2")
    start = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)
    offsets = np.arange(0.0, 3601.0, 60.0)
    geometry = Geometry(orbits, (_station("Madrid", 40.4168, -3.7038, 650.0),), TIMESCALE, start)

    datetimes = [start + timedelta(seconds=float(offset)) for offset in offsets]
    messages = orbits[0].satellite.at(TIMESCALE.from_datetimes(datetimes)).message
    first = next(index for index, message in enumerate(messages) if message)
    assert 0 < first < len(offsets) - 1
    first_text = datetimes[first].isoformat().replace("+00:00", "Z")

    for call in (geometry.satellite_ecef, lambda *args: geometry.topocentric(0, *args)):
        with pytest.raises(ValueError) as caught:
            call(0, offsets)
        message = str(caught.value)
        assert type(caught.value) is ValueError
        assert "NORAD 25544" in message
        assert first_text in message
        assert re.search(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d{6})?Z", message)
        assert "decayed" in message

    position, _ = geometry.satellite_ecef(0, offsets[:first])
    assert np.isfinite(position).all()


def test_non_finite_state_rejected(tmp_path: Path, monkeypatch) -> None:
    from openleo.engine import geometry as geometry_module
    from openleo.engine.geometry import Geometry

    orbits = _iss_orbits(tmp_path)
    propagate = geometry_module._teme_states

    def corrupted(model, frame):
        errors, position, velocity = propagate(model, frame)
        position[1, -1] = np.nan
        return errors, position, velocity

    monkeypatch.setattr(geometry_module, "_teme_states", corrupted)
    geometry = Geometry(
        orbits,
        (_station("Madrid", 40.4168, -3.7038, 650.0),),
        TIMESCALE,
        datetime(2026, 8, 30, 6, 0, tzinfo=UTC),
    )
    with pytest.raises(ValueError, match="non-finite.*NORAD 25544"):
        geometry.satellite_ecef(0, np.array([0.0, 10.0]))


def test_satellite_at_the_station_rejected(tmp_path: Path, monkeypatch) -> None:
    from openleo.engine.geometry import Geometry

    orbits = _iss_orbits(tmp_path)
    geometry = Geometry(
        orbits,
        (_station("Madrid", 40.4168, -3.7038, 650.0),),
        TIMESCALE,
        datetime(2026, 8, 30, 6, 0, tzinfo=UTC),
    )
    monkeypatch.setattr(
        geometry,
        "satellite_ecef",
        lambda _index, _offsets: (geometry.station_ecef_m[:1].copy(), np.ones((1, 3))),
    )
    with pytest.raises(ValueError, match="non-finite topocentric geometry for station Madrid"):
        geometry.topocentric(0, 0, np.zeros(1))


def test_offsets_are_validated_and_empty_offsets_allowed(tmp_path: Path) -> None:
    from openleo.engine.geometry import Geometry

    orbits = _iss_orbits(tmp_path)
    geometry = Geometry(
        orbits,
        (_station("Madrid", 40.4168, -3.7038, 650.0),),
        TIMESCALE,
        datetime(2026, 8, 30, 6, 0, tzinfo=UTC),
    )

    position, velocity = geometry.satellite_ecef(0, np.array([]))
    assert position.shape == velocity.shape == (0, 3)
    topocentric = geometry.topocentric(0, 0, np.array([]))
    assert topocentric.elevation_deg.shape == topocentric.range_rate_mps.shape == (0,)
    for offsets in (np.array([0.0, np.nan]), np.array([np.inf]), np.zeros((2, 2))):
        with pytest.raises(ValueError, match="offsets"):
            geometry.satellite_ecef(0, offsets)
    # A scalar offset is a one-sample array.
    assert geometry.elevation_deg(0, 0, 30.0).shape == (1,)


def test_geometry_requires_a_utc_start(tmp_path: Path) -> None:
    from openleo.engine.geometry import Geometry

    orbits = _iss_orbits(tmp_path)
    station = (_station("Madrid", 40.4168, -3.7038, 650.0),)
    with pytest.raises(ValueError, match="start_utc"):
        Geometry(orbits, station, TIMESCALE, datetime(2026, 8, 30, 6, 0))  # noqa: DTZ001


def test_shared_times_give_identical_states(tmp_path: Path) -> None:
    from openleo.engine.geometry import Geometry

    orbits = _iss_orbits(tmp_path)
    geometry = Geometry(
        orbits,
        (_station("Madrid", 40.4168, -3.7038, 650.0),),
        TIMESCALE,
        datetime(2026, 8, 30, 6, 0, tzinfo=UTC),
    )
    offsets = np.arange(0.0, 600.0, 10.0)
    first = geometry.satellite_ecef(0, offsets)
    again = geometry.satellite_ecef(0, offsets.copy())
    subset = geometry.satellite_ecef(0, offsets[::7])

    np.testing.assert_array_equal(first[0], again[0])
    np.testing.assert_array_equal(first[1], again[1])
    np.testing.assert_array_equal(first[0][::7], subset[0])
