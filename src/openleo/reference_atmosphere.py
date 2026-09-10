"""Idealized global reference atmosphere, not measured or local weather.

Temperature, total pressure, and humidity follow ITU-R P.835-7 Annex 1,
equations (1)-(8). Radio refractivity follows ITU-R P.453-14 Annex 1,
equations (1)-(2):
https://www.itu.int/dms_pubrec/itu-r/rec/p/R-REC-P.453-14-201908-I!!PDF-E.pdf
"""

from dataclasses import dataclass
from math import exp, sqrt
from numbers import Real

PROFILE_SOURCE = {
    "recommendation": "ITU-R P.835-7",
    "url": "https://www.itu.int/dms_pubrec/itu-r/rec/p/R-REC-P.835-7-202408-I!!PDF-E.pdf",
    "sha256": "0c355e9471dd382592b46b35cd16346172700fb2e472b95dea9fd1229b087df3",
}

_GEOPOTENTIAL_RADIUS_KM = 6356.766
# Compare geometric heights so inverse-rounding at H=11 or H=71 does not
# select the wrong side of the published, slightly discontinuous pressure.
_LAYER_TOPS_KM = tuple(
    _GEOPOTENTIAL_RADIUS_KM * height / (_GEOPOTENTIAL_RADIUS_KM - height)
    for height in (11, 20, 32, 47, 51, 71)
)


@dataclass(frozen=True, slots=True)
class AtmosphericState:
    temperature_k: float
    total_pressure_hpa: float
    dry_pressure_hpa: float
    water_vapour_density_g_m3: float
    water_vapour_pressure_hpa: float
    refractive_index: float


def reference_atmosphere(height_km: float) -> AtmosphericState:
    """Return the reference state at geometric height 0..100 km above mean sea level.

    P.835's pressure is total pressure; the returned dry pressure subtracts
    water vapour partial pressure before use in P.453 or P.676 calculations.
    Published rounded layer discontinuities are retained. At the undefined
    H=11 endpoint the upper layer is used; other pressure endpoints use
    the lower layer, as specified by equations (3b)-(3g).
    """
    if isinstance(height_km, bool) or not isinstance(height_km, Real) or not 0 <= height_km <= 100:
        raise ValueError("height_km must be a finite real number between 0 and 100 inclusive")
    height_km = float(height_km)
    temperature, pressure = _temperature_and_pressure(height_km)
    density = max(7.5 * exp(-height_km / 2), 2e-6 * pressure * 216.7 / temperature)
    vapour_pressure = density * temperature / 216.7
    dry_pressure = pressure - vapour_pressure
    refractivity = (
        77.6 * dry_pressure / temperature
        + 72 * vapour_pressure / temperature
        + 3.75e5 * vapour_pressure / temperature**2
    )
    return AtmosphericState(
        temperature_k=temperature,
        total_pressure_hpa=pressure,
        dry_pressure_hpa=dry_pressure,
        water_vapour_density_g_m3=density,
        water_vapour_pressure_hpa=vapour_pressure,
        refractive_index=1 + 1e-6 * refractivity,
    )


def _temperature_and_pressure(height_km: float) -> tuple[float, float]:
    if height_km >= 86:
        temperature = (
            186.8673
            if height_km <= 91
            else 263.1905 - 76.3232 * sqrt(1 - ((height_km - 91) / 19.9429) ** 2)
        )
        pressure = exp(
            95.571899
            - 4.011801 * height_km
            + 6.424731e-2 * height_km**2
            - 4.789660e-4 * height_km**3
            + 1.340543e-6 * height_km**4
        )
        return temperature, pressure

    height = _GEOPOTENTIAL_RADIUS_KM * height_km / (_GEOPOTENTIAL_RADIUS_KM + height_km)
    if height_km < _LAYER_TOPS_KM[0]:
        temperature = 288.15 - 6.5 * height
        return temperature, 1013.25 * (288.15 / temperature) ** (-34.1632 / 6.5)
    if height_km <= _LAYER_TOPS_KM[1]:
        return 216.65, 226.3226 * exp(-34.1632 * (height - 11) / 216.65)
    if height_km <= _LAYER_TOPS_KM[2]:
        temperature = 216.65 + height - 20
        return temperature, 54.74980 * (216.65 / temperature) ** 34.1632
    if height_km <= _LAYER_TOPS_KM[3]:
        temperature = 228.65 + 2.8 * (height - 32)
        return temperature, 8.680422 * (228.65 / temperature) ** (34.1632 / 2.8)
    if height_km <= _LAYER_TOPS_KM[4]:
        return 270.65, 1.109106 * exp(-34.1632 * (height - 47) / 270.65)
    if height_km <= _LAYER_TOPS_KM[5]:
        temperature = 270.65 - 2.8 * (height - 51)
        return temperature, 0.6694167 * (270.65 / temperature) ** (-34.1632 / 2.8)
    temperature = 214.65 - 2 * (height - 71)
    return temperature, 0.03956649 * (214.65 / temperature) ** (-34.1632 / 2)
