"""The tabulated gaseous loss of the Shannon integrand agrees with the ray trace to 1e-10 dB.

``GasTable`` (Task 18 ruling, method (b)) replaces the ray trace only inside the Shannon-bound
quadrature and the MODCOD root-finding. These tests prove its error over the whole domain the
engine evaluates: geometric elevations from 5 to 90 degrees and every range whose endpoint lies
above the 100 km column, for every station height of the shipped examples and the frequencies
and refinements of the kernel equivalence tests.
"""

import re
from dataclasses import FrozenInstanceError, replace
from itertools import product

import numpy as np
import pytest

from openleo.physics.atmospheric_path import (
    EARTH_RADIUS_KM,
    GAS_TABLE_ACCEPT_DB,
    GAS_TABLE_MAX_ERROR_DB,
    build_gas_table,
    build_reference_column,
)

SEED = 20261102
SAMPLES = 6_000
# The four example stations (Madrid 650 m, Tromso and Singapore 0 m, Quito 2850 m) and the
# frequencies and refinements of tests/physics/test_path_arrays.py.
LOWER_HEIGHTS_KM = (0.0, 0.65, 2.85)
CONFIGURATIONS = tuple(product((12e9, 20e9, 30e9), (1, 2), LOWER_HEIGHTS_KM))
TABLE_ATOL_DB = 1e-10


def _geometry(lower_height_km, count, seed=SEED):
    """Elevations over [5, 90] (a sixth packed near 5 deg, where the loss is steepest) and
    ranges from just above the column to 3000 km, with the domain edges included."""
    rng = np.random.default_rng(seed)
    elevation = np.concatenate(
        [
            [5.0, 5.0, 90.0, 90.0, 89.9999999, 5.0000001],
            rng.uniform(5.0, 90.0, 5 * count // 6),
            5.0 + rng.exponential(0.3, count // 6),
        ]
    )
    elevation = np.minimum(elevation, 90.0)
    range_m = np.concatenate(
        [[3_000e3, 200e3, 3_000e3, 101e3, 600e3, 400e3], rng.uniform(100e3, 3_000e3, count)]
    )[: elevation.size]
    radians = np.radians(elevation)
    endpoint_km = np.hypot(
        EARTH_RADIUS_KM + lower_height_km + range_m / 1_000 * np.sin(radians),
        range_m / 1_000 * np.cos(radians),
    )
    above = endpoint_km > EARTH_RADIUS_KM + 100.0 + 1e-3
    return elevation[above], range_m[above]


@pytest.mark.parametrize(("frequency_hz", "refinement", "lower_height_km"), CONFIGURATIONS)
def test_table_matches_the_ray_trace_over_the_whole_domain(
    frequency_hz, refinement, lower_height_km
):
    column = build_reference_column(frequency_hz, lower_height_km, refinement=refinement)
    table = build_gas_table(column)
    elevation, range_m = _geometry(lower_height_km, SAMPLES)

    exact = column.for_geometry_array(elevation, range_m).total_db
    tabulated = table.total_db_for_geometry(elevation, range_m)

    assert table.verified
    assert table.error_bound_db <= GAS_TABLE_ACCEPT_DB < GAS_TABLE_MAX_ERROR_DB == TABLE_ATOL_DB
    assert table.max_measured_error_db <= table.error_bound_db
    assert table.max_loss_error_db <= table.error_bound_db
    np.testing.assert_array_less(np.abs(tabulated - exact), table.error_bound_db)


@pytest.mark.parametrize("lower_height_km", LOWER_HEIGHTS_KM)
def test_table_matches_the_scalar_v1_kernel(lower_height_km):
    column = build_reference_column(12e9, lower_height_km)
    elevation, range_m = _geometry(lower_height_km, 300, seed=SEED + 1)

    scalar = [column.for_geometry(float(e), float(r)).total_db for e, r in zip(elevation, range_m)]

    np.testing.assert_allclose(
        build_gas_table(column).total_db_for_geometry(elevation, range_m),
        scalar,
        rtol=0,
        atol=TABLE_ATOL_DB,
    )


def test_table_error_bound_covers_the_measured_error_between_its_check_points():
    column = build_reference_column(30e9, 0.0)
    table = build_gas_table(column)
    # Apparent elevations everywhere in the table's domain, not only at its check points.
    apparent = np.linspace(5.0, 90.0, 20_001)
    total, central = column._column_rows(apparent)
    series_total = table._series(table.total_db, apparent)
    series_central, _ = table._series(table.central_angle_rad, apparent)

    assert np.max(np.abs(series_total - total)) <= table.error_bound_db
    assert np.max(np.abs(series_central - central)) <= 2 * table.max_central_angle_error_rad


def test_unverifiable_tables_are_flagged():
    # The 60 GHz oxygen complex: losses of order 1000 dB, whose float64 layer sums alone carry
    # more than 1e-10 dB of rounding. The evaluator then traces the ray exactly.
    table = build_gas_table(build_reference_column(60e9, 0.0))

    assert not table.verified
    assert table.error_bound_db > GAS_TABLE_MAX_ERROR_DB


def _sweep_geometry(lower_height_km):
    """A dense elevation grid, packed in 5.0-5.5 deg, at satellite altitudes 110-1200 km."""
    elevation = np.unique(
        np.concatenate(
            [
                5.0 + np.array([0.0, 1e-12, 1e-9, 1e-6, 1e-4, 1e-3]),
                np.linspace(5.0, 5.5, 251),
                np.geomspace(5.0, 90.0, 400),
                [89.999999, 90.0],
            ]
        )
    )
    altitude_km = np.array([110.0, 200.0, 340.0, 550.0, 800.0, 1200.0])
    elevation, altitude_km = (a.ravel() for a in np.meshgrid(elevation, altitude_km))
    station_km = EARTH_RADIUS_KM + lower_height_km
    satellite_km = EARTH_RADIUS_KM + altitude_km
    radians = np.radians(elevation)
    range_m = (
        np.sqrt(satellite_km**2 - (station_km * np.cos(radians)) ** 2)
        - station_km * np.sin(radians)
    ) * 1_000
    return elevation, range_m


def _check_sweep(frequency_hz, lower_height_km, refinement):
    """An accepted table is within its recorded bound, itself within 1e-10 dB, everywhere."""
    column = build_reference_column(frequency_hz, lower_height_km, refinement=refinement)
    table = build_gas_table(column)
    elevation, range_m = _sweep_geometry(lower_height_km)
    exact = column.for_geometry_array(elevation, range_m).total_db
    measured = float(np.max(np.abs(table.total_db_for_geometry(elevation, range_m) - exact)))
    assert table.max_measured_error_db <= table.error_bound_db or not table.verified
    if table.verified:
        assert table.error_bound_db <= GAS_TABLE_ACCEPT_DB
        assert measured <= table.error_bound_db <= GAS_TABLE_MAX_ERROR_DB, (measured, table)
    return table.verified


# 12 GHz (the examples' band, accepted) and 55 GHz (the oxygen wing, refused: its bound is
# about 8e-10 dB) at Quito's 2.85 km, where the review found the old bound exceeded.
@pytest.mark.parametrize(("frequency_hz", "accepted"), [(12e9, True), (55e9, False)])
def test_accepted_tables_are_within_their_bound_on_a_dense_sweep(frequency_hz, accepted):
    assert _check_sweep(frequency_hz, 2.85, 1) is accepted


@pytest.mark.slow
@pytest.mark.parametrize(
    ("frequency_hz", "lower_height_km", "refinement"),
    list(
        product(
            (1e9, 12e9, 20e9, 30e9, 40e9, 50e9, 55e9, 60e9, 90e9, 183e9),
            LOWER_HEIGHTS_KM,
            (1, 2),
        )
    ),
)
def test_every_accepted_table_is_within_its_bound_across_the_band(
    frequency_hz, lower_height_km, refinement
):
    _check_sweep(frequency_hz, lower_height_km, refinement)


def test_table_is_read_only_and_keeps_the_input_shape():
    table = build_gas_table(build_reference_column(20e9, 1.3))
    values = table.total_db_for_geometry(np.array([[30.0, 60.0]]), np.array([2e6]))

    assert values.shape == (1, 2)
    assert not values.flags.writeable
    for coefficients in (table.edges_deg, table.total_db, table.central_angle_rad):
        assert not coefficients.flags.writeable
    with pytest.raises(FrozenInstanceError):
        table.verified = False
    assert table.total_db_for_geometry(np.array([]), np.array([])).shape == (0,)


@pytest.mark.parametrize(
    ("elevation", "range_m"),
    [
        (4.99, 2e6),
        (90.1, 2e6),
        (float("nan"), 2e6),
        (30.0, 0.0),
        (30.0, float("inf")),
        (90.0, 500.0),
    ],
)
def test_table_rejects_any_invalid_element_like_the_ray_trace(elevation, range_m):
    column = build_reference_column(20e9, 0.0)
    with pytest.raises(ValueError) as exact_error:
        column.for_geometry_array(np.array([30.0, elevation]), np.array([2e6, range_m]))

    with pytest.raises(ValueError, match=re.escape(str(exact_error.value))):
        build_gas_table(column).total_db_for_geometry(
            np.array([30.0, elevation]), np.array([2e6, range_m])
        )


def test_table_reports_an_unbracketed_endpoint_like_the_ray_trace():
    column = build_reference_column(20e9, 99.0)
    upper = np.ones(len(column.radii_km) - 1)
    column = replace(column, refractive_indices=np.r_[0.99, upper])
    with pytest.raises(ValueError, match="not bracketed") as exact_error:
        column.for_geometry_array(np.full(2, 20.0), np.full(2, 2e6))

    with pytest.raises(ValueError, match=re.escape(str(exact_error.value))):
        build_gas_table(column).total_db_for_geometry(np.full(2, 20.0), np.full(2, 2e6))


def test_table_requires_a_ground_to_space_column():
    with pytest.raises(ValueError, match="ending at 100 km"):
        build_gas_table(build_reference_column(20e9, 1.3, 8.0))
