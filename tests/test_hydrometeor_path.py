"""Declared hydrometeor geometry and physical domains, independent of orbit code."""

from dataclasses import FrozenInstanceError
from importlib import import_module
from math import sqrt

import pytest


def path(*, liquid=0.0, rain=25.0, top=5000.0, height=1000.0, elevation=90.0, frequency=28e9):
    api = import_module("openleo.hydrometeor_path")
    station = api.HydrometeorStation(liquid, rain, top, 0.0)
    return api.evaluate_hydrometeor_path(frequency, elevation, height, station)


def test_zenith_rain_layer_is_literal_height_difference_and_db_per_km():
    result = path()
    assert result.rain_path_length_m == 4000.0
    assert result.rain_attenuation_db == result.rain_specific_attenuation_db_per_km * 4.0
    assert result.hydrometeor_attenuation_db == result.rain_attenuation_db
    with pytest.raises(FrozenInstanceError):
        result.rain_path_length_m = 0.0


def test_slant_root_intersects_the_declared_spherical_shell():
    result = path(elevation=30.0)
    # Substitute the path in |r + s*u| = R + rain_top; sin(30 degrees) = 1/2.
    radius = 6_372_000.0
    length = result.rain_path_length_m
    assert sqrt(radius**2 + radius * length + length**2) == pytest.approx(6_376_000.0)
    assert 4000.0 < length < 8000.0


def test_zero_rain_and_zero_height_layer_have_zero_rain_loss():
    assert path(rain=0.0).rain_attenuation_db == 0.0
    layer = path(top=1000.0)
    assert layer.rain_path_length_m == layer.rain_attenuation_db == 0.0
    assert layer.rain_specific_attenuation_db_per_km > 0.0


def test_cloud_loss_is_linear_in_the_declared_above_station_column():
    first = path(liquid=0.5, rain=0.0, elevation=45.0)
    second = path(liquid=1.0, rain=0.0, elevation=45.0)
    assert second.cloud_attenuation_db == 2 * first.cloud_attenuation_db > 0.0
    assert second.hydrometeor_attenuation_db == second.cloud_attenuation_db


def test_path_preserves_official_cloud_and_rain_reference_values():
    assert path(
        frequency=6e9, liquid=0.8235924623564901, elevation=15.0
    ).cloud_attenuation_db == pytest.approx(0.09905224128740467, rel=1e-12)
    assert path(
        frequency=14.25e9, rain=26.48052, elevation=31.076991235657
    ).rain_specific_attenuation_db_per_km == pytest.approx(1.58130839366869, rel=1e-12)


def test_thin_layer_does_not_lose_the_path_to_subtractive_cancellation():
    result = path(top=1000.000001, height=1000.0)
    assert result.rain_path_length_m == pytest.approx(0.000001, rel=1e-7)


@pytest.mark.parametrize("stations", [[], (), (("A", {}),), (("A",),), (("", None),)])
def test_public_config_rejects_mutable_or_malformed_stations(stations):
    api = import_module("openleo.hydrometeor_path")
    with pytest.raises(ValueError):
        api.HydrometeorConfig("declared_uniform_layers", stations)


def test_public_config_rejects_duplicate_names_and_invalid_tilt():
    api = import_module("openleo.hydrometeor_path")
    station = api.HydrometeorStation(0.0, 0.0, 0.0, 180.0)
    with pytest.raises(ValueError):
        api.HydrometeorConfig("declared_uniform_layers", (("A", station), ("A", station)))
    with pytest.raises(ValueError):
        api.HydrometeorStation(0.0, 0.0, 0.0, 181.0)


@pytest.mark.parametrize(
    "arguments",
    [
        (-1, 1000, 30),
        (0, -1, 30),
        (1000, 999, 30),
        (0, 20001, 30),
        (0, 1000, 4),
        (0, 1000, 91),
        (0, 1000, True),
        (10**400, 1000, 30),
    ],
)
def test_geometry_function_rejects_invalid_public_inputs(arguments):
    api = import_module("openleo.hydrometeor_path")
    with pytest.raises(ValueError):
        api.rain_layer_path_length_m(*arguments)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"frequency": 1e9 - 1},
        {"frequency": 200e9 + 1},
        {"elevation": 4.99},
        {"elevation": 90.01},
        {"height": -1},
        {"height": 10001},
        {"top": 999},
        {"top": 20001},
        {"liquid": -1},
        {"rain": -1},
        {"liquid": True},
        {"rain": float("inf")},
        {"liquid": 10**400},
        {"rain": 10**400},
        {"liquid": 1e308, "frequency": 200e9},
        {"rain": 1e308, "frequency": 14e9},
    ],
)
def test_path_rejects_invalid_or_overflowing_values(kwargs):
    with pytest.raises(ValueError):
        path(**kwargs)


@pytest.mark.parametrize("frequency", [1e9, 200e9])
@pytest.mark.parametrize("elevation", [5.0, 90.0])
def test_path_accepts_supported_domain_boundaries(frequency, elevation):
    assert path(frequency=frequency, elevation=elevation).hydrometeor_attenuation_db >= 0.0
