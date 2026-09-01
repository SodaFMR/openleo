"""ITU-R P.676-13 Annex 1 specific gaseous attenuation.

Equations are adapted from https://github.com/inigodelportillo/ITU-Rpy,
``itur/models/itu676.py`` at commit
f739993c4b6d34076de22249ef53d03fa5a53d73, under the MIT license retained in
``THIRD_PARTY_NOTICES.md``.
"""

from dataclasses import dataclass
from math import exp, isfinite, sqrt

from openleo._p676_coefficients import OXYGEN_LINES, WATER_VAPOUR_LINES


@dataclass(frozen=True, slots=True)
class SpecificGaseousAttenuation:
    dry_air_db_per_km: float
    water_vapour_db_per_km: float
    total_db_per_km: float


def _validate_finite(value: float, name: str) -> None:
    if isinstance(value, bool) or not isfinite(value):
        raise ValueError(f"{name} must be finite and not a bool")


def specific_gaseous_attenuation(
    frequency_hz: float,
    dry_air_pressure_hpa: float,
    temperature_k: float,
    water_vapour_density_g_per_m3: float,
) -> SpecificGaseousAttenuation:
    """Return dry-air, water-vapour, and total specific attenuation in dB/km."""
    for value, name in (
        (frequency_hz, "frequency_hz"),
        (dry_air_pressure_hpa, "dry_air_pressure_hpa"),
        (temperature_k, "temperature_k"),
        (water_vapour_density_g_per_m3, "water_vapour_density_g_per_m3"),
    ):
        _validate_finite(value, name)
    if not 1e9 <= frequency_hz <= 1e12:
        raise ValueError("frequency_hz must be between 1e9 and 1e12 inclusive")
    if dry_air_pressure_hpa <= 0.0:
        raise ValueError("dry_air_pressure_hpa must be positive")
    if temperature_k <= 0.0:
        raise ValueError("temperature_k must be positive")
    if water_vapour_density_g_per_m3 < 0.0:
        raise ValueError("water_vapour_density_g_per_m3 must be non-negative")

    f_ghz = frequency_hz / 1e9
    theta = 300.0 / temperature_k
    water_vapour_pressure_hpa = water_vapour_density_g_per_m3 * temperature_k / 216.7
    total_pressure_hpa = dry_air_pressure_hpa + water_vapour_pressure_hpa

    oxygen_refractivity = 0.0
    for f_oxygen, a1, a2, a3, a4, a5, a6 in OXYGEN_LINES:
        width = (
            a3
            * 1e-4
            * (dry_air_pressure_hpa * theta ** (0.8 - a4) + 1.1 * water_vapour_pressure_hpa * theta)
        )
        width = sqrt(width**2 + 2.25e-6)
        line_mixing = (a5 + a6 * theta) * 1e-4 * total_pressure_hpa * theta**0.8
        line_shape = (
            f_ghz
            / f_oxygen
            * (
                (width - line_mixing * (f_oxygen - f_ghz)) / ((f_oxygen - f_ghz) ** 2 + width**2)
                + (width - line_mixing * (f_oxygen + f_ghz)) / ((f_oxygen + f_ghz) ** 2 + width**2)
            )
        )
        line_strength = a1 * 1e-7 * dry_air_pressure_hpa * theta**3 * exp(a2 * (1.0 - theta))
        oxygen_refractivity += line_strength * line_shape

    continuum_width = 5.6e-4 * total_pressure_hpa * theta**0.8
    dry_continuum = (
        f_ghz
        * dry_air_pressure_hpa
        * theta**2
        * (
            6.14e-5 / (continuum_width * (1.0 + (f_ghz / continuum_width) ** 2))
            + 1.4e-12 * dry_air_pressure_hpa * theta**1.5 / (1.0 + 1.9e-5 * f_ghz**1.5)
        )
    )
    dry_air_db_per_km = 0.1820 * f_ghz * (oxygen_refractivity + dry_continuum)

    water_refractivity = 0.0
    for f_water, b1, b2, b3, b4, b5, b6 in WATER_VAPOUR_LINES:
        width = (
            b3
            * 1e-4
            * (dry_air_pressure_hpa * theta**b4 + b5 * water_vapour_pressure_hpa * theta**b6)
        )
        width = 0.535 * width + sqrt(0.217 * width**2 + 2.1316e-12 * f_water**2 / theta)
        line_shape = (
            f_ghz
            / f_water
            * (
                width / ((f_water - f_ghz) ** 2 + width**2)
                + width / ((f_water + f_ghz) ** 2 + width**2)
            )
        )
        line_strength = b1 * 1e-1 * water_vapour_pressure_hpa * theta**3.5 * exp(b2 * (1.0 - theta))
        water_refractivity += line_strength * line_shape

    water_vapour_db_per_km = 0.1820 * f_ghz * water_refractivity
    total_db_per_km = dry_air_db_per_km + water_vapour_db_per_km
    if not all(
        isfinite(value) and value >= 0.0
        for value in (dry_air_db_per_km, water_vapour_db_per_km, total_db_per_km)
    ):
        raise ValueError("P.676-13 calculation produced invalid attenuation")
    return SpecificGaseousAttenuation(
        dry_air_db_per_km,
        water_vapour_db_per_km,
        total_db_per_km,
    )
