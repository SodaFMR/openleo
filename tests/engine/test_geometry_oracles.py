"""Independent oracles of the ISS example's grid samples (ported from the v1 single-pass tests).

The engine's samples are checked against literal v1.0.0 values, against topocentric geometry
recomputed with standard-library trigonometry from Skyfield's full TEME-GCRS-ITRS chain, and for
range-rate and Doppler signs and a finite difference of the range.
"""

import functools
from datetime import UTC, datetime
from itertools import pairwise
from math import asin, atan2, cos, degrees, radians, sin, sqrt
from pathlib import Path

import numpy as np
import pytest
from skyfield.api import wgs84
from skyfield.framelib import itrs

from openleo.engine.catalogs import load_catalogs
from openleo.engine.run import run
from openleo.engine.timegrid import builtin_timescale
from openleo.scenario import load_scenario

ROOT = Path(__file__).resolve().parents[2]
ISS = ROOT / "examples" / "scenarios" / "iss_cartagena.json"


@functools.cache
def _samples():
    scenario = load_scenario(ISS)
    table = run(scenario).tables["samples"]
    return scenario, {column.name: np.asarray(table[column.name]) for column in table.columns}


def test_iss_samples_reproduce_the_v1_pass():
    _, samples = _samples()
    assert samples["t_offset_s"].size == 40  # v1: 40 visible rows, 06:15:30 to 06:22:00
    assert samples["t_offset_s"][0] == 90.0 and samples["t_offset_s"][-1] == 480.0
    closest = int(np.argmin(samples["range_m"]))
    assert samples["t_offset_s"][closest] == 290.0  # 06:18:50
    # Literal v1.0.0 values at the closest sample (v1 test_simulation).
    assert samples["elevation_deg"][closest] == pytest.approx(61.5799734359, abs=1e-6)
    assert samples["range_m"][closest] == pytest.approx(473_676.7193, abs=0.5)
    assert samples["range_rate_mps"][closest] == pytest.approx(37.1481223, abs=0.01)
    assert samples["doppler_hz"][closest] == pytest.approx(-272.6081553, abs=0.01)
    assert samples["shannon_upper_bound_bps"].max() == pytest.approx(6_336_955.3241, rel=1e-7)


def test_range_rate_and_doppler_signs_and_finite_difference():
    _, samples = _samples()
    ranges = samples["range_m"]
    rate, doppler = samples["range_rate_mps"], samples["doppler_hz"]
    closest = int(np.argmin(ranges))
    finite_difference_mps = (ranges[closest + 1] - ranges[closest - 1]) / 20.0
    assert rate[closest] == pytest.approx(finite_difference_mps, abs=0.5)
    assert rate[0] < 0.0 < doppler[0]  # approaching: negative range rate, positive Doppler
    assert doppler[-1] < 0.0 < rate[-1]  # receding
    assert all(left > right for left, right in pairwise(ranges[: closest + 1]))
    assert all(left < right for left, right in pairwise(ranges[closest:]))


def test_closest_sample_matches_independent_enu_geometry():
    scenario, samples = _samples()
    closest = int(np.argmin(samples["range_m"]))
    station = scenario.ground_segment.stations[0]
    timescale = builtin_timescale()
    (orbit,) = load_catalogs(scenario.space_segment, timescale)
    moment = scenario.time.start_utc.timestamp() + float(samples["t_offset_s"][closest])
    t = timescale.from_datetime(datetime.fromtimestamp(moment, UTC))
    spacecraft_m = orbit.satellite.at(t).frame_xyz(itrs).m
    station_m = wgs84.latlon(
        station.latitude_deg, station.longitude_deg, elevation_m=station.height_ellipsoid_m
    ).itrs_xyz.m
    dx, dy, dz = (spacecraft_m[i] - station_m[i] for i in range(3))
    lat, lon = radians(station.latitude_deg), radians(station.longitude_deg)
    east = -sin(lon) * dx + cos(lon) * dy
    north = -sin(lat) * cos(lon) * dx - sin(lat) * sin(lon) * dy + cos(lat) * dz
    up = cos(lat) * cos(lon) * dx + cos(lat) * sin(lon) * dy + sin(lat) * dz
    range_m = sqrt(east**2 + north**2 + up**2)
    assert samples["range_m"][closest] == pytest.approx(range_m, rel=0.0, abs=1e-5)
    assert samples["elevation_deg"][closest] == pytest.approx(
        degrees(asin(up / range_m)), rel=0.0, abs=1e-9
    )
    assert samples["azimuth_deg"][closest] == pytest.approx(
        degrees(atan2(east, north)) % 360.0, rel=0.0, abs=1e-9
    )
