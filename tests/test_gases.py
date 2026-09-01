from math import isclose

import pytest

from openleo.gases import specific_gaseous_attenuation


@pytest.mark.parametrize(
    ("frequency_hz", "expected_dry", "expected_wet", "expected_total"),
    [
        (12e9, 0.00869826406877357, 0.00953538822024593, 0.0182336522890195),
        (20e9, 0.0118835504778076, 0.0970473048151117, 0.108930855292919),
        (60e9, 14.6234747964861, 0.154841840636247, 14.7783166371223),
        (90e9, 0.0388697110724235, 0.341973394422181, 0.380843105494605),
        (130e9, 0.0415090835995228, 0.751844703646129, 0.793353787245652),
    ],
)
def test_specific_gaseous_attenuation_matches_official_p676_13_cases(
    frequency_hz: float,
    expected_dry: float,
    expected_wet: float,
    expected_total: float,
) -> None:
    attenuation = specific_gaseous_attenuation(frequency_hz, 1013.25, 288.15, 7.5)

    assert isclose(
        attenuation.dry_air_db_per_km,
        expected_dry,
        rel_tol=1e-12,
        abs_tol=1e-13,
    )
    assert isclose(
        attenuation.water_vapour_db_per_km,
        expected_wet,
        rel_tol=1e-12,
        abs_tol=1e-13,
    )
    assert isclose(
        attenuation.total_db_per_km,
        expected_total,
        rel_tol=1e-12,
        abs_tol=1e-13,
    )
    assert (
        attenuation.total_db_per_km
        == attenuation.dry_air_db_per_km + attenuation.water_vapour_db_per_km
    )


@pytest.mark.parametrize("frequency_hz", [1e9, 1e12])
def test_specific_gaseous_attenuation_accepts_frequency_boundaries(frequency_hz: float) -> None:
    attenuation = specific_gaseous_attenuation(frequency_hz, 1013.25, 288.15, 7.5)

    assert attenuation.dry_air_db_per_km >= 0.0
    assert attenuation.water_vapour_db_per_km >= 0.0
    assert (
        attenuation.total_db_per_km
        == attenuation.dry_air_db_per_km + attenuation.water_vapour_db_per_km
    )


def test_specific_gaseous_attenuation_accepts_dry_air() -> None:
    attenuation = specific_gaseous_attenuation(12e9, 1013.25, 288.15, 0.0)

    assert attenuation.water_vapour_db_per_km == 0.0
    assert attenuation.total_db_per_km == attenuation.dry_air_db_per_km


@pytest.mark.parametrize(
    ("args", "field"),
    [
        ((1e9 - 1.0, 1013.25, 288.15, 7.5), "frequency_hz"),
        ((1e12 + 1.0, 1013.25, 288.15, 7.5), "frequency_hz"),
        ((float("nan"), 1013.25, 288.15, 7.5), "frequency_hz"),
        ((float("inf"), 1013.25, 288.15, 7.5), "frequency_hz"),
        ((12e9, 0.0, 288.15, 7.5), "dry_air_pressure_hpa"),
        ((12e9, -1.0, 288.15, 7.5), "dry_air_pressure_hpa"),
        ((12e9, float("nan"), 288.15, 7.5), "dry_air_pressure_hpa"),
        ((12e9, 1013.25, 0.0, 7.5), "temperature_k"),
        ((12e9, 1013.25, -1.0, 7.5), "temperature_k"),
        ((12e9, 1013.25, float("inf"), 7.5), "temperature_k"),
        ((12e9, 1013.25, 288.15, -1.0), "water_vapour_density_g_per_m3"),
        ((12e9, 1013.25, 288.15, float("nan")), "water_vapour_density_g_per_m3"),
        ((True, 1013.25, 288.15, 7.5), "frequency_hz"),
        ((12e9, True, 288.15, 7.5), "dry_air_pressure_hpa"),
        ((12e9, 1013.25, True, 7.5), "temperature_k"),
        ((12e9, 1013.25, 288.15, False), "water_vapour_density_g_per_m3"),
    ],
)
def test_specific_gaseous_attenuation_rejects_invalid_domains(
    args: tuple[float, float, float, float],
    field: str,
) -> None:
    with pytest.raises(ValueError, match=field):
        specific_gaseous_attenuation(*args)
