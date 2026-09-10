from dataclasses import FrozenInstanceError, replace
from math import asin, atan2, cos, degrees, hypot, pi, radians, sin, sqrt
from types import SimpleNamespace

import numpy as np
import pytest

from openleo import atmospheric_path
from openleo.atmospheric_path import build_reference_column
from openleo.gases import SpecificGaseousAttenuation


@pytest.mark.parametrize(
    ("upper_height_km", "total_db", "bending_rad"),
    [
        (8.0, 0.24376211236218553, 0.0002517972739610741),
        (100.0, 0.27744110604568128, 0.00045579353223956787),
    ],
)
def test_official_p676_13_section_b_reference(upper_height_km, total_db, bending_rad):
    # ITU validation workbook, Annex 1 section b; normalized Eq. 16 layers.
    result = build_reference_column(28e9, 1.3, upper_height_km).at_apparent_elevation(30.0)
    assert result.total_db == pytest.approx(total_db, rel=1e-8)
    assert result.bending_rad == pytest.approx(bending_rad, rel=1e-8)
    assert result.total_db == result.dry_air_db + result.water_vapour_db
    assert result.refractive_excess_path_m > 0


def test_zenith_is_vertical_column():
    column = build_reference_column(20e9, 1.3)
    result = column.at_apparent_elevation(90.0)
    assert result.geometric_path_m == pytest.approx(98_700.0, abs=1e-8)
    assert result.bending_rad == pytest.approx(0.0, abs=1e-15)
    assert result.central_angle_rad == pytest.approx(0.0, abs=1e-15)
    expected_dry = np.dot(column.thicknesses_km, column.dry_air_db_per_km)
    expected_water = np.dot(column.thicknesses_km, column.water_vapour_db_per_km)
    expected_excess = 1_000 * np.dot(column.thicknesses_km, column.refractive_indices - 1)
    assert result.dry_air_db == pytest.approx(expected_dry, rel=1e-12)
    assert result.water_vapour_db == pytest.approx(expected_water, rel=1e-12)
    assert result.refractive_excess_path_m == pytest.approx(expected_excess, rel=1e-12)
    ground = column.for_geometry(90.0, 600_000.0)
    assert ground.apparent_elevation_deg == 90.0
    assert ground.excess_delay_s == pytest.approx(expected_excess / 299_792_458, rel=1e-8)


def _constant_medium(monkeypatch, refractive_index, dry=0.1, water=0.2):
    monkeypatch.setattr(
        atmospheric_path,
        "reference_atmosphere",
        lambda _height: SimpleNamespace(
            temperature_k=288.15,
            dry_pressure_hpa=1_000.0,
            water_vapour_density_g_m3=7.5,
            refractive_index=refractive_index,
        ),
    )
    monkeypatch.setattr(
        atmospheric_path,
        "specific_gaseous_attenuation",
        lambda *_args: SpecificGaseousAttenuation(dry, water, dry + water),
    )


def test_homogeneous_shell_matches_analytic_chord(monkeypatch):
    _constant_medium(monkeypatch, 1.0003)
    column = build_reference_column(20e9, 1.3, 8.0)
    result = column.at_apparent_elevation(30.0)
    radius, top = 6372.3, 6379.0
    impact = radius * cos(radians(30))
    length = sqrt(top**2 - impact**2) - radius * sin(radians(30))
    assert result.geometric_path_m == pytest.approx(length * 1_000, rel=1e-12)
    assert result.total_db == pytest.approx(length * 0.3, rel=1e-12)
    assert result.refractive_excess_path_m == pytest.approx(length * 0.3, rel=1e-12)
    assert result.central_angle_rad == pytest.approx(radians(60) - asin(impact / top), abs=1e-14)
    assert result.bending_rad == pytest.approx(0, abs=1e-14)


def test_endpoint_solver_recovers_analytic_uniform_medium_ray(monkeypatch):
    index = 1.0003
    _constant_medium(monkeypatch, index)
    radius, top, endpoint = 6372.3, 6471.0, 6971.0
    apparent = radians(30.0)
    impact = radius * cos(apparent)
    vacuum_impact = index * impact
    angle = (
        pi / 2
        - apparent
        - asin(impact / top)
        + asin(vacuum_impact / top)
        - asin(vacuum_impact / endpoint)
    )
    transverse = endpoint * sin(angle)
    radial = endpoint * cos(angle) - radius
    range_m = 1_000 * hypot(transverse, radial)
    geometric_elevation = degrees(atan2(radial, transverse))
    atmospheric_length = sqrt(top**2 - impact**2) - radius * sin(apparent)
    vacuum_length = sqrt(endpoint**2 - vacuum_impact**2) - sqrt(top**2 - vacuum_impact**2)
    expected_delay = ((index * atmospheric_length + vacuum_length) * 1_000 - range_m) / 299_792_458

    result = build_reference_column(20e9, 1.3).for_geometry(geometric_elevation, range_m)

    assert result.apparent_elevation_deg == pytest.approx(30.0, abs=1e-9)
    assert result.total_db == pytest.approx(atmospheric_length * 0.3, rel=1e-11)
    assert result.excess_delay_s == pytest.approx(expected_delay, abs=1e-14)


@pytest.mark.parametrize("elevation", [5.0, 30.0, 90.0])
@pytest.mark.parametrize(("height", "refinement"), [(0.0, 1), (0.3, 2), (1.3, 4)])
def test_vacuum_geometry_has_no_invented_loss_or_delay(monkeypatch, elevation, height, refinement):
    _constant_medium(monkeypatch, 1.0, dry=0.0, water=0.0)
    column = build_reference_column(20e9, height, refinement=refinement)
    result = column.for_geometry(elevation, 2_000_000.0)
    assert result.apparent_elevation_deg == pytest.approx(elevation, abs=1e-9)
    assert result.total_db == 0.0
    assert result.excess_delay_s == pytest.approx(0.0, abs=1e-12)


def test_vacuum_layer_interfaces_do_not_introduce_bending(monkeypatch):
    _constant_medium(monkeypatch, 1.0, dry=0.0, water=0.0)
    path = build_reference_column(20e9, 0.0).at_apparent_elevation(5.0)
    assert path.bending_rad == pytest.approx(0.0, abs=1e-15)


def test_endpoint_solve_is_distinct_from_geometric_elevation_as_apparent():
    column = build_reference_column(20e9, 0.0)
    result = column.for_geometry(5.0, 2_000_000.0)
    naive = column.at_apparent_elevation(5.0)
    assert 5.0 < result.apparent_elevation_deg < 6.0
    assert 0.0 < result.total_db < naive.total_db
    assert result.excess_delay_s > 0.0
    path = column.at_apparent_elevation(result.apparent_elevation_deg)
    assert result.total_db == pytest.approx(path.total_db, rel=1e-13)


def test_refinement_splits_layers_and_converges():
    columns = [build_reference_column(20e9, 1.3, refinement=n) for n in (1, 2, 4)]
    assert [len(c.thicknesses_km) for c in columns] == [
        len(columns[0].thicknesses_km) * n for n in (1, 2, 4)
    ]
    paths = [c.at_apparent_elevation(5.0) for c in columns]
    losses = [path.total_db for path in paths]
    assert abs(losses[2] - losses[1]) < abs(losses[1] - losses[0])
    assert abs(losses[2] - losses[1]) / losses[2] < 1e-4
    bends = [path.bending_rad for path in paths]
    assert abs(bends[2] - bends[1]) < abs(bends[1] - bends[0])
    delays = [column.for_geometry(5.0, 2e6).excess_delay_s for column in columns]
    assert abs(delays[2] - delays[1]) < abs(delays[1] - delays[0])
    assert abs(delays[2] - delays[1]) / delays[2] < 1e-4


def test_column_and_results_are_immutable_and_reuse_profile(monkeypatch):
    column = build_reference_column(20e9, 0.0)
    with pytest.raises(FrozenInstanceError):
        column.frequency_hz = 30e9
    with pytest.raises(ValueError):
        column.refractive_indices[0] = 1.0
    with pytest.raises(ValueError):
        column.refractive_indices.flags.writeable = True
    monkeypatch.setattr(
        atmospheric_path, "reference_atmosphere", lambda _: pytest.fail("rebuilt profile")
    )
    monkeypatch.setattr(
        atmospheric_path, "specific_gaseous_attenuation", lambda *_: pytest.fail("rebuilt gamma")
    )
    first = column.at_apparent_elevation(30.0)
    assert first == column.at_apparent_elevation(30.0)
    with pytest.raises(FrozenInstanceError):
        first.total_db = 0.0
    second = column.for_geometry(30.0, 600_000.0)
    with pytest.raises(FrozenInstanceError):
        second.total_db = 0.0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"frequency_hz": True},
        {"frequency_hz": float("nan")},
        {"frequency_hz": 1e9 - 1},
        {"frequency_hz": 1e12 + 1},
        {"lower_height_km": -1.0},
        {"lower_height_km": 100.0},
        {"lower_height_km": False},
        {"upper_height_km": float("inf")},
        {"upper_height_km": 100.1},
        {"upper_height_km": 0.0},
        {"refinement": 3},
        {"refinement": True},
        {"refinement": 2.0},
    ],
)
def test_build_rejects_invalid_inputs(kwargs):
    with pytest.raises(ValueError):
        build_reference_column(**({"frequency_hz": 20e9, "lower_height_km": 0.0} | kwargs))


@pytest.mark.parametrize("elevation", [True, float("nan"), float("inf"), -0.1, 90.1])
def test_slant_rejects_invalid_apparent_elevation(elevation):
    with pytest.raises(ValueError):
        build_reference_column(20e9, 1.3, 1.4).at_apparent_elevation(elevation)


@pytest.mark.parametrize(
    ("elevation", "range_m"),
    [
        (4.99, 2e6),
        (90.1, 2e6),
        (True, 2e6),
        (float("nan"), 2e6),
        (30.0, 0.0),
        (30.0, -1.0),
        (30.0, True),
        (30.0, float("inf")),
        (90.0, 500.0),
    ],
)
def test_geometry_rejects_invalid_inputs(elevation, range_m):
    with pytest.raises(ValueError):
        build_reference_column(20e9, 99.0).for_geometry(elevation, range_m)


def test_nonnegative_apparent_horizon_is_supported():
    result = build_reference_column(20e9, 0.0).at_apparent_elevation(0.0)
    assert result.total_db > 0.0
    assert result.bending_rad > 0.0


def test_geometry_rejects_truncating_atmosphere_below_100_km():
    with pytest.raises(ValueError, match="ending at 100 km"):
        build_reference_column(20e9, 1.3, 8.0).for_geometry(30.0, 2e6)


@pytest.mark.parametrize(("lower", "upper"), [(0.0, 5e-324), (1.0, 1.0 + 1e-15)])
def test_unresolvable_layers_fail_loudly(lower, upper):
    with pytest.raises(ValueError, match="too small"):
        build_reference_column(20e9, lower, upper)


def test_ducting_domain_is_rejected_without_clipping():
    column = build_reference_column(20e9, 0.0, 0.01)
    duct = replace(column, refractive_indices=np.r_[1.01, np.ones(len(column.radii_km) - 1)])
    with pytest.raises(ValueError, match="trapped"):
        duct.at_apparent_elevation(0.0)
