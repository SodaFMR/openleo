"""ITU-R P.838-3 rain and P.840-9 cloud attenuation kernels.

The P.838 coefficient ``k`` follows the Recommendation's numerical convention
when rain rate is supplied in mm/h; it is not a dimensionless quantity.
"""

from dataclasses import dataclass
from math import cos, exp, isfinite, log10, radians, sin
from numbers import Real
from types import MappingProxyType

RAIN_SOURCE = MappingProxyType(
    {
        "recommendation": "ITU-R P.838-3",
        "url": ("https://www.itu.int/dms_pubrec/itu-r/rec/p/r-rec-p.838-3-200503-i!!pdf-e.pdf"),
        "sha256": "3ab7482993e51fc63c5127a72e9e8930614e73652ac614882760817e7c1469cb",
    }
)
CLOUD_SOURCE = MappingProxyType(
    {
        "recommendation": "ITU-R P.840-9",
        "url": ("https://www.itu.int/dms_pubrec/itu-r/rec/p/R-REC-P.840-9-202308-I!!PDF-E.pdf"),
        "sha256": "cd554242549a4f3c5854133dce6f7f93dd86e04cfa8a20b5c1ff5fe9357f37b6",
    }
)

_K_HORIZONTAL = (
    (
        (-5.33980, -0.10008, 1.13098),
        (-0.35351, 1.26970, 0.45400),
        (-0.23789, 0.86036, 0.15354),
        (-0.94158, 0.64552, 0.16817),
    ),
    -0.18961,
    0.71147,
)
_K_VERTICAL = (
    (
        (-3.80595, 0.56934, 0.81061),
        (-3.44965, -0.22911, 0.51059),
        (-0.39902, 0.73042, 0.11899),
        (0.50167, 1.07319, 0.27195),
    ),
    -0.16398,
    0.63297,
)
_ALPHA_HORIZONTAL = (
    (
        (-0.14318, 1.82442, -0.55187),
        (0.29591, 0.77564, 0.19822),
        (0.32177, 0.63773, 0.13164),
        (-5.37610, -0.96230, 1.47828),
        (16.1721, -3.29980, 3.43990),
    ),
    0.67849,
    -1.95537,
)
_ALPHA_VERTICAL = (
    (
        (-0.07771, 2.33840, -0.76284),
        (0.56727, 0.95545, 0.54039),
        (-0.20238, 1.14520, 0.26809),
        (-48.2991, 0.791669, 0.116226),
        (48.5833, 0.791459, 0.116479),
    ),
    -0.053739,
    0.83433,
)


@dataclass(frozen=True, slots=True)
class RainAttenuation:
    k: float
    alpha: float
    specific_attenuation_db_per_km: float


@dataclass(frozen=True, slots=True)
class CloudAttenuation:
    mass_absorption_db_per_kg_m2: float
    attenuation_db: float


def _finite(value: float, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{name} must be finite and not a bool")  # noqa: TRY004
    try:
        finite = isfinite(value)
    except OverflowError:
        finite = False
    if not finite:
        raise ValueError(f"{name} must be finite and not a bool")


def _p838_fit(log_frequency_ghz: float, coefficients, logarithmic: bool) -> float:
    terms, slope, intercept = coefficients
    value = (
        sum(
            amplitude * exp(-(((log_frequency_ghz - centre) / width) ** 2))
            for amplitude, centre, width in terms
        )
        + slope * log_frequency_ghz
        + intercept
    )
    return 10.0**value if logarithmic else value


def rain_specific_attenuation(
    frequency_hz: float,
    rain_rate_mm_h: float,
    elevation_deg: float,
    polarization_tilt_deg: float,
) -> RainAttenuation:
    """Return P.838-3 coefficients and rain specific attenuation in dB/km."""
    for value, name in (
        (frequency_hz, "frequency_hz"),
        (rain_rate_mm_h, "rain_rate_mm_h"),
        (elevation_deg, "elevation_deg"),
        (polarization_tilt_deg, "polarization_tilt_deg"),
    ):
        _finite(value, name)
    if not 1e9 <= frequency_hz <= 1e12:
        raise ValueError("frequency_hz must be between 1e9 and 1e12 inclusive")
    if rain_rate_mm_h < 0.0:
        raise ValueError("rain_rate_mm_h must be non-negative")
    if not 0.0 <= elevation_deg <= 90.0:
        raise ValueError("elevation_deg must be between 0 and 90 inclusive")
    if not 0.0 <= polarization_tilt_deg <= 180.0:
        raise ValueError("polarization_tilt_deg must be between 0 and 180 inclusive")

    log_frequency_ghz = log10(frequency_hz / 1e9)
    k_h = _p838_fit(log_frequency_ghz, _K_HORIZONTAL, True)
    k_v = _p838_fit(log_frequency_ghz, _K_VERTICAL, True)
    alpha_h = _p838_fit(log_frequency_ghz, _ALPHA_HORIZONTAL, False)
    alpha_v = _p838_fit(log_frequency_ghz, _ALPHA_VERTICAL, False)
    geometry = cos(radians(elevation_deg)) ** 2 * cos(radians(2.0 * polarization_tilt_deg))
    k = (k_h + k_v + (k_h - k_v) * geometry) / 2.0
    alpha = (k_h * alpha_h + k_v * alpha_v + (k_h * alpha_h - k_v * alpha_v) * geometry) / (2.0 * k)
    try:
        specific_attenuation = 0.0 if rain_rate_mm_h == 0.0 else k * rain_rate_mm_h**alpha
    except OverflowError as exc:
        raise ValueError("P.838-3 calculation produced invalid attenuation") from exc
    if not all(isfinite(value) and value >= 0.0 for value in (k, alpha, specific_attenuation)):
        raise ValueError("P.838-3 calculation produced invalid attenuation")
    return RainAttenuation(k, alpha, specific_attenuation)


def cloud_slant_attenuation(
    frequency_hz: float,
    liquid_water_kg_m2: float,
    elevation_deg: float,
) -> CloudAttenuation:
    """Return P.840-9 liquid mass absorption and instantaneous slant loss."""
    for value, name in (
        (frequency_hz, "frequency_hz"),
        (liquid_water_kg_m2, "liquid_water_kg_m2"),
        (elevation_deg, "elevation_deg"),
    ):
        _finite(value, name)
    if not 1e9 <= frequency_hz <= 200e9:
        raise ValueError("frequency_hz must be between 1e9 and 200e9 inclusive")
    if liquid_water_kg_m2 < 0.0:
        raise ValueError("liquid_water_kg_m2 must be non-negative")
    if not 5.0 <= elevation_deg <= 90.0:
        raise ValueError("elevation_deg must be between 5 and 90 inclusive")

    frequency_ghz = frequency_hz / 1e9
    temperature_k = 273.75
    theta = 300.0 / temperature_k - 1.0
    epsilon_0 = 77.66 + 103.3 * theta
    epsilon_1 = 0.0671 * epsilon_0
    epsilon_2 = 3.52
    principal_frequency = 20.20 - 146.0 * theta + 316.0 * theta**2
    secondary_frequency = 39.8 * principal_frequency
    epsilon_double_prime = frequency_ghz * (epsilon_0 - epsilon_1) / (
        principal_frequency * (1.0 + (frequency_ghz / principal_frequency) ** 2)
    ) + frequency_ghz * (epsilon_1 - epsilon_2) / (
        secondary_frequency * (1.0 + (frequency_ghz / secondary_frequency) ** 2)
    )
    epsilon_prime = (
        (epsilon_0 - epsilon_1) / (1.0 + (frequency_ghz / principal_frequency) ** 2)
        + (epsilon_1 - epsilon_2) / (1.0 + (frequency_ghz / secondary_frequency) ** 2)
        + epsilon_2
    )
    eta = (2.0 + epsilon_prime) / epsilon_double_prime
    k_l = 0.819 * frequency_ghz / (epsilon_double_prime * (1.0 + eta**2))
    correction = (
        0.1522 * exp(-((frequency_ghz + 23.9589) ** 2) / 3299.1)
        + 11.51 * exp(-((frequency_ghz - 219.2096) ** 2) / 2_759_500.0)
        - 10.4912
    )
    mass_absorption = k_l * correction
    attenuation = mass_absorption * liquid_water_kg_m2 / sin(radians(elevation_deg))
    if not all(isfinite(value) and value >= 0.0 for value in (mass_absorption, attenuation)):
        raise ValueError("P.840-9 calculation produced invalid attenuation")
    return CloudAttenuation(mass_absorption, attenuation)
