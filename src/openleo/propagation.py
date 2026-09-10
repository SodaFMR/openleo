"""Explicit, immutable configuration for the idealized reference atmosphere."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from openleo.gases import MAX_FREQUENCY_HZ, MIN_FREQUENCY_HZ
from openleo.input import _object, _range, _string
from openleo.reference_atmosphere import PROFILE_SOURCE

PROPAGATION_KEYS = frozenset(("model", "station_heights_amsl_m", "refinement"))
PROPAGATION_LINK_FIELDS = (
    "gaseous_dry_attenuation_db",
    "gaseous_water_attenuation_db",
    "gaseous_attenuation_db",
    "free_space_cn0_db_hz",
    "geometric_delay_s",
    "atmospheric_excess_delay_s",
    "apparent_elevation_deg",
)


@dataclass(frozen=True, slots=True)
class PropagationConfig:
    model: str
    station_heights_amsl_m: tuple[tuple[str, float], ...]
    refinement: int

    def __post_init__(self):
        if self.model != "itu_reference":
            raise ValueError("propagation.model must be itu_reference")
        if (
            isinstance(self.refinement, bool)
            or not isinstance(self.refinement, int)
            or self.refinement not in (1, 2, 4)
        ):
            raise ValueError("propagation.refinement must be an integer: 1, 2 or 4")
        if not isinstance(self.station_heights_amsl_m, tuple) or not all(
            isinstance(entry, tuple) and len(entry) == 2 for entry in self.station_heights_amsl_m
        ):
            raise ValueError(
                "propagation.station_heights_amsl_m must be immutable name-height pairs"
            )
        names = tuple(
            _string(name, "propagation.station_heights_amsl_m station name")
            for name, _ in self.station_heights_amsl_m
        )
        if not names or len(set(names)) != len(names):
            raise ValueError("propagation.station_heights_amsl_m requires unique station names")
        for name, height in self.station_heights_amsl_m:
            _range(height, f"propagation.station_heights_amsl_m.{name}", 0.0, 10_000.0)


def parse_propagation(
    raw: Any,
    station_names: tuple[str, ...],
    frequency_hz: float,
    minimum_elevation_deg: float,
) -> PropagationConfig:
    """Require independent AMSL heights, not an implicit ellipsoid-height conversion."""
    data = _object(raw, PROPAGATION_KEYS, "propagation")
    heights = data["station_heights_amsl_m"]
    if not isinstance(heights, Mapping) or set(heights) != set(station_names):
        raise ValueError("propagation.station_heights_amsl_m must name every station exactly once")
    config = PropagationConfig(
        model=data["model"],
        station_heights_amsl_m=tuple(
            (name, _range(heights[name], f"propagation.station_heights_amsl_m.{name}", 0, 10_000))
            for name in station_names
        ),
        refinement=data["refinement"],
    )
    _range(frequency_hz, "propagation carrier_frequency_hz", MIN_FREQUENCY_HZ, MAX_FREQUENCY_HZ)
    _range(minimum_elevation_deg, "propagation minimum_elevation_deg", 5.0, 90.0)
    return config


def propagation_document(config: PropagationConfig) -> dict[str, Any]:
    return {
        "model": config.model,
        "station_heights_amsl_m": dict(config.station_heights_amsl_m),
        "refinement": config.refinement,
    }


def propagation_metadata(config: PropagationConfig) -> dict[str, Any]:
    """Describe reproducible assumptions without calculating an atmospheric path."""
    return {
        **propagation_document(config),
        "profile": {
            **PROFILE_SOURCE,
            "method": "Annex 1 global reference temperature, total pressure and water vapour; dry pressure = total minus vapour pressure",
        },
        "refractivity": {
            "recommendation": "ITU-R P.453-14",
            "url": "https://www.itu.int/dms_pubrec/itu-r/rec/p/R-REC-P.453-14-201908-I!!PDF-E.pdf",
            "sha256": "fd163ef75cb4fd03848d4fe1fc0a043cb78e0edc164ba29a6bb69209204c17a0",
            "method": "Annex 1 dry and wet radio refractivity; non-dispersive refractive index",
        },
        "ray": {
            "recommendation": "ITU-R P.676-13",
            "url": "https://www.itu.int/dms_pubrec/itu-r/rec/p/R-REC-P.676-13-202208-I!!PDF-E.pdf",
            "sha256": "8c09b2d2c120bdae33f60c2a7abff873374d54075ce0dc71060de8a5507bbe2f",
            "method": "Annex 1 equations (16)-(19), normalized exponential layers; midpoint attenuation and refractivity; split-layer refinement",
            "earth_radius_km": 6371.0,
            "upper_height_km": 100.0,
            "geometry": "Apparent elevation solves the spherical ray endpoint central angle from geometric elevation and range; endpoint above 100 km",
        },
        "height_reference": "Explicit geometric height above mean sea level (AMSL); distinct from station WGS84 ellipsoid height",
        "noise": "Declared system_noise_temperature_k is fixed; atmospheric sky emission is not added",
        "delay": "Geometric range/c plus bent-path and refractive optical-path excess/c; non-dispersive approximation",
        "visibility_and_doppler": "Geometric elevation mask and geometric range-rate Doppler are unchanged; no refractive Doppler or contact-boundary correction",
        "scope": "Idealized global reference atmosphere, not local weather or observational validation; mean-radius sphere approximates WGS84 local geometry",
    }
