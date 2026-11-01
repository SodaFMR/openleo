"""Vectorised ray tracer and hydrometeor path reproduce their scalar v1 kernels."""

import re
from dataclasses import FrozenInstanceError, fields, replace
from itertools import product

import numpy as np
import pytest

from openleo.physics.atmospheric_path import (
    EARTH_RADIUS_KM,
    GroundPathArrays,
    GroundPathResult,
    build_reference_column,
)
from openleo.physics.hydrometeor_path import (
    HydrometeorPathArrays,
    HydrometeorPathResult,
    HydrometeorStation,
    evaluate_hydrometeor_path,
    evaluate_hydrometeor_path_array,
)

SEED = 20261101
SAMPLES = 10_000
FREQUENCIES_HZ = (12e9, 20e9, 30e9)
REFINEMENTS = (1, 2)
LOWER_HEIGHTS_KM = (0.0, 0.65, 2.85)
CONFIGURATIONS = tuple(product(FREQUENCIES_HZ, REFINEMENTS, LOWER_HEIGHTS_KM))
DB_FIELDS = ("dry_air_db", "water_vapour_db", "total_db")
# Array-vs-scalar tolerances. The array kernel solves the apparent elevation by Newton
# iteration to its rounding floor (~1e-12 deg), the scalar v1 kernel by 40 bisection steps
# (within ~4e-11 deg); numpy may also evaluate float64 transcendentals with SIMD code (SVML on
# AVX-512) that differs from libm by an ulp. Measured worst cases over every configuration
# below: 1.7e-11 dB, 3.9e-11 deg and 1.2e-14 s.
DB_ATOL = 1e-10
APPARENT_ELEVATION_ATOL_DEG = 1e-9
EXCESS_DELAY_ATOL_S = 1e-13


def _ground_geometry(lower_height_km, count):
    """Seeded (elevation, range) draws whose endpoint lies above the 100 km column."""
    rng = np.random.default_rng(SEED)
    elevation = rng.uniform(5.0, 90.0, 2 * count)
    range_m = rng.uniform(600e3, 3_000e3, 2 * count)
    radians = np.radians(elevation)
    endpoint_km = np.hypot(
        EARTH_RADIUS_KM + lower_height_km + range_m / 1_000 * np.sin(radians),
        range_m / 1_000 * np.cos(radians),
    )
    above = endpoint_km > EARTH_RADIUS_KM + 100.0 + 1e-3
    return elevation[above][:count], range_m[above][:count]


def _assert_ground_paths_match(column, elevation, range_m):
    arrays = column.for_geometry_array(elevation, range_m)
    scalars = [
        column.for_geometry(float(e), float(r)) for e, r in zip(elevation, range_m, strict=True)
    ]
    for name in DB_FIELDS:
        expected = [getattr(scalar, name) for scalar in scalars]
        np.testing.assert_allclose(getattr(arrays, name), expected, rtol=0, atol=DB_ATOL)
    np.testing.assert_allclose(
        arrays.apparent_elevation_deg,
        [scalar.apparent_elevation_deg for scalar in scalars],
        rtol=0,
        atol=APPARENT_ELEVATION_ATOL_DEG,
    )
    np.testing.assert_allclose(
        arrays.excess_delay_s,
        [scalar.excess_delay_s for scalar in scalars],
        rtol=0,
        atol=EXCESS_DELAY_ATOL_S,
    )


@pytest.mark.parametrize(
    ("index", "frequency_hz", "refinement", "lower_height_km"),
    [(index, *configuration) for index, configuration in enumerate(CONFIGURATIONS)],
)
def test_for_geometry_array_matches_scalar(index, frequency_hz, refinement, lower_height_km):
    # The 10,000 seeded geometries are shared out across the 18 configurations, because one
    # scalar ray trace costs about a millisecond (40 bisection steps over ~1,000 layers).
    # The slow test below checks all 10,000 in every configuration.
    share = np.array_split(np.arange(SAMPLES), len(CONFIGURATIONS))[index]
    elevation, range_m = _ground_geometry(lower_height_km, SAMPLES)
    column = build_reference_column(frequency_hz, lower_height_km, refinement=refinement)

    _assert_ground_paths_match(column, elevation[share], range_m[share])


@pytest.mark.slow
@pytest.mark.parametrize(("frequency_hz", "refinement", "lower_height_km"), CONFIGURATIONS)
def test_for_geometry_array_matches_scalar_for_every_sample_in_every_configuration(
    frequency_hz, refinement, lower_height_km
):
    elevation, range_m = _ground_geometry(lower_height_km, SAMPLES)
    column = build_reference_column(frequency_hz, lower_height_km, refinement=refinement)

    assert elevation.size == SAMPLES
    _assert_ground_paths_match(column, elevation, range_m)


def test_for_geometry_array_matches_scalar_at_domain_edges():
    column = build_reference_column(20e9, 0.0)
    elevation = np.array([5.0, 5.0, 90.0, 90.0, 89.999999, 45.0])
    range_m = np.array([3_000e3, 800e3, 600e3, 3_000e3, 700e3, 1_234_567.0])

    _assert_ground_paths_match(column, elevation, range_m)
    assert column.for_geometry_array(elevation, range_m).apparent_elevation_deg[2] == 90.0


def test_ground_path_arrays_mirror_the_scalar_result_and_are_read_only():
    column = build_reference_column(20e9, 1.3)
    arrays = column.for_geometry_array(np.array([[30.0, 60.0]]), np.array([2e6]))

    assert [f.name for f in fields(GroundPathArrays)] == [f.name for f in fields(GroundPathResult)]
    for f in fields(GroundPathArrays):
        values = getattr(arrays, f.name)
        assert values.shape == (1, 2)
        assert values.dtype == np.float64
        assert not values.flags.writeable
    with pytest.raises(FrozenInstanceError):
        arrays.total_db = arrays.dry_air_db


def test_for_geometry_array_accepts_empty_input():
    arrays = build_reference_column(20e9, 99.0).for_geometry_array(np.array([]), np.array([]))

    assert arrays.total_db.shape == (0,)


@pytest.mark.parametrize(
    ("elevation", "range_m"),
    [
        (4.99, 2e6),
        (90.1, 2e6),
        (float("nan"), 2e6),
        (float("inf"), 2e6),
        (30.0, 0.0),
        (30.0, -1.0),
        (30.0, float("inf")),
        (90.0, 500.0),
    ],
)
def test_for_geometry_array_rejects_any_invalid_element_like_the_scalar(elevation, range_m):
    column = build_reference_column(20e9, 99.0)
    with pytest.raises(ValueError) as scalar_error:
        column.for_geometry(elevation, range_m)

    with pytest.raises(ValueError, match=re.escape(str(scalar_error.value))):
        column.for_geometry_array(np.array([30.0, elevation, 60.0]), np.array([2e6, range_m, 2e6]))


@pytest.mark.parametrize(
    ("indices", "elevation"),
    [
        ("raised_ground", 20.0),  # impact parameter exceeds the column top
        ("dropped_top", 20.0),  # ray trapped below the top layer
        ("dropped_top", 60.0),  # bent ray shorter than the chord
        ("lowered_ground", 20.0),  # endpoint below the ray at geometric elevation
    ],
)
def test_for_geometry_array_reports_ray_failures_like_the_scalar(indices, elevation):
    column = build_reference_column(20e9, 99.0)
    upper = np.ones(len(column.radii_km) - 1)
    medium = {
        "raised_ground": np.r_[1.1, upper],
        "dropped_top": np.r_[upper, 0.9],
        "lowered_ground": np.r_[0.99, upper],
    }[indices]
    column = replace(column, refractive_indices=medium)
    with pytest.raises(ValueError) as scalar_error:
        column.for_geometry(elevation, 2e6)

    with pytest.raises(ValueError, match=re.escape(str(scalar_error.value))):
        column.for_geometry_array(np.full(3, elevation), np.full(3, 2e6))


def test_for_geometry_array_rejects_bool_arrays_and_truncated_columns():
    column = build_reference_column(20e9, 99.0)
    with pytest.raises(ValueError, match="geometric_elevation_deg must be finite and not a bool"):
        column.for_geometry_array(np.array([True]), np.array([2e6]))
    with pytest.raises(ValueError, match="range_m must be finite and not a bool"):
        column.for_geometry_array(np.array([30.0]), np.array([True]))
    with pytest.raises(ValueError, match="ending at 100 km"):
        build_reference_column(20e9, 1.3, 8.0).for_geometry_array(np.array([30.0]), np.array([2e6]))


HYDROMETEOR_CASES = (
    (0.0, HydrometeorStation(0.5, 25.0, 5_000.0, 45.0)),
    (650.0, HydrometeorStation(0.0, 0.0, 3_000.0, 0.0)),
    (2_850.0, HydrometeorStation(2.0, 60.0, 7_000.0, 90.0)),
)


@pytest.mark.parametrize("frequency_hz", FREQUENCIES_HZ)
@pytest.mark.parametrize(("station_height_amsl_m", "station"), HYDROMETEOR_CASES)
def test_hydrometeor_path_array_matches_scalar(frequency_hz, station_height_amsl_m, station):
    rng = np.random.default_rng(SEED)
    elevation = np.concatenate(([5.0, 90.0], rng.uniform(5.0, 90.0, SAMPLES)))

    arrays = evaluate_hydrometeor_path_array(
        frequency_hz, elevation, station_height_amsl_m, station
    )

    scalars = [
        evaluate_hydrometeor_path(frequency_hz, float(value), station_height_amsl_m, station)
        for value in elevation
    ]
    assert [f.name for f in fields(HydrometeorPathArrays)] == [
        f.name for f in fields(HydrometeorPathResult)
    ]
    for f in fields(HydrometeorPathResult):
        np.testing.assert_allclose(
            getattr(arrays, f.name),
            [getattr(scalar, f.name) for scalar in scalars],
            rtol=1e-14,
            # Lengths are metres: one ulp of sin(elevation) moves them by up to ~7e-12 m.
            atol=1e-9 if f.name == "rain_path_length_m" else 1e-12,
        )


def test_hydrometeor_path_array_rejects_non_numeric_elevations():
    station = HydrometeorStation(0.5, 25.0, 5_000.0, 45.0)
    with pytest.raises(ValueError, match="geometric_elevation_deg must be a real-valued array"):
        evaluate_hydrometeor_path_array(28e9, np.array([True]), 1_000.0, station)


def test_hydrometeor_path_arrays_are_read_only_and_keep_the_input_shape():
    station = HydrometeorStation(0.5, 25.0, 5_000.0, 45.0)

    arrays = evaluate_hydrometeor_path_array(28e9, np.full((2, 2), 30.0), 1_000.0, station)

    for f in fields(HydrometeorPathArrays):
        assert getattr(arrays, f.name).shape == (2, 2)
        assert not getattr(arrays, f.name).flags.writeable
    with pytest.raises(FrozenInstanceError):
        arrays.rain_path_length_m = arrays.cloud_attenuation_db


@pytest.mark.parametrize(
    ("frequency_hz", "elevation", "station_height_amsl_m", "station"),
    [
        (28e9, 4.0, 1_000.0, HydrometeorStation(0.5, 25.0, 5_000.0, 45.0)),
        (28e9, 90.5, 1_000.0, HydrometeorStation(0.5, 25.0, 5_000.0, 45.0)),
        (28e9, float("nan"), 1_000.0, HydrometeorStation(0.5, 25.0, 5_000.0, 45.0)),
        (201e9, 30.0, 1_000.0, HydrometeorStation(0.5, 25.0, 5_000.0, 45.0)),
        (28e9, 30.0, 6_000.0, HydrometeorStation(0.5, 25.0, 5_000.0, 45.0)),
        (28e9, 30.0, 1_000.0, None),
        (28e9, 30.0, 1_000.0, HydrometeorStation(1.7e308, 25.0, 5_000.0, 45.0)),
        (12e9, 30.0, 1_000.0, HydrometeorStation(0.5, 1e308, 5_000.0, 45.0)),
        (10**400, 30.0, 1_000.0, HydrometeorStation(0.5, 25.0, 5_000.0, 45.0)),
        (28e9, 30.0, 10**400, HydrometeorStation(0.5, 25.0, 5_000.0, 45.0)),
    ],
)
def test_hydrometeor_path_array_rejects_invalid_inputs_like_the_scalar(
    frequency_hz, elevation, station_height_amsl_m, station
):
    with pytest.raises(ValueError) as scalar_error:
        evaluate_hydrometeor_path(frequency_hz, elevation, station_height_amsl_m, station)

    with pytest.raises(ValueError, match=re.escape(str(scalar_error.value))):
        evaluate_hydrometeor_path_array(
            frequency_hz, np.array([30.0, elevation, 60.0]), station_height_amsl_m, station
        )
