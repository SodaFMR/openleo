"""Fixed declared clouds and uniform spherical rain layers; no weather inference."""

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from math import radians, sin, sqrt
from typing import Any

from openleo.hydrometeors import cloud_slant_attenuation, rain_specific_attenuation
from openleo.input import _non_negative_number, _object, _range, _string

EARTH_RADIUS_M = 6_371_000.0
STATION_KEYS = frozenset(
    (
        "liquid_water_kg_m2",
        "rain_rate_mm_h",
        "rain_top_height_amsl_m",
        "polarization_tilt_deg",
    )
)
HYDROMETEOR_LINK_FIELDS = (
    "cloud_attenuation_db",
    "rain_specific_attenuation_db_per_km",
    "rain_path_length_m",
    "rain_attenuation_db",
    "hydrometeor_attenuation_db",
    "total_atmospheric_attenuation_db",
)


@dataclass(frozen=True, slots=True)
class HydrometeorStation:
    liquid_water_kg_m2: float
    rain_rate_mm_h: float
    rain_top_height_amsl_m: float
    polarization_tilt_deg: float

    def __post_init__(self):
        try:
            for field in ("liquid_water_kg_m2", "rain_rate_mm_h"):
                _non_negative_number(getattr(self, field), f"hydrometeors.{field}")
            _range(self.rain_top_height_amsl_m, "hydrometeors.rain_top_height_amsl_m", 0, 20_000)
            _range(self.polarization_tilt_deg, "hydrometeors.polarization_tilt_deg", 0, 180)
        except OverflowError as exc:
            raise ValueError("hydrometeor values must be finite") from exc


@dataclass(frozen=True, slots=True)
class HydrometeorConfig:
    model: str
    stations: tuple[tuple[str, HydrometeorStation], ...]

    def __post_init__(self):
        if self.model != "declared_uniform_layers":
            raise ValueError("hydrometeors.model must be declared_uniform_layers")
        if (
            not isinstance(self.stations, tuple)
            or not self.stations
            or not all(
                isinstance(entry, tuple)
                and len(entry) == 2
                and isinstance(entry[1], HydrometeorStation)
                for entry in self.stations
            )
        ):
            raise ValueError("hydrometeors.stations must be immutable name-state pairs")
        names = tuple(_string(name, "hydrometeors station name") for name, _ in self.stations)
        if len(set(names)) != len(names):
            raise ValueError("hydrometeors.stations requires unique station names")


@dataclass(frozen=True, slots=True)
class HydrometeorPathResult:
    cloud_attenuation_db: float
    rain_specific_attenuation_db_per_km: float
    rain_path_length_m: float
    rain_attenuation_db: float
    hydrometeor_attenuation_db: float


def parse_hydrometeors(raw: Any, station_names: tuple[str, ...]) -> HydrometeorConfig:
    data = _object(raw, frozenset(("model", "stations")), "propagation.hydrometeors")
    stations = data["stations"]
    if not isinstance(stations, Mapping) or set(stations) != set(station_names):
        raise ValueError("hydrometeors.stations must name every station exactly once")
    return HydrometeorConfig(
        data["model"],
        tuple(
            (
                name,
                HydrometeorStation(**_object(stations[name], STATION_KEYS, f"hydrometeors.{name}")),
            )
            for name in station_names
        ),
    )


def hydrometeor_document(config: HydrometeorConfig) -> dict[str, Any]:
    return {
        "model": config.model,
        "stations": {name: asdict(state) for name, state in config.stations},
    }


def rain_layer_path_length_m(
    station_height_amsl_m: float,
    rain_top_height_amsl_m: float,
    geometric_elevation_deg: float,
) -> float:
    """Stable positive shell-intersection root for an outward ground ray."""
    try:
        _range(station_height_amsl_m, "hydrometeors station_height_amsl_m", 0, 10_000)
        _range(
            rain_top_height_amsl_m,
            "hydrometeors rain_top_height_amsl_m",
            station_height_amsl_m,
            20_000,
        )
        _range(geometric_elevation_deg, "hydrometeors geometric_elevation_deg", 5, 90)
    except OverflowError as exc:
        raise ValueError("hydrometeor geometry inputs must be finite") from exc
    radius = EARTH_RADIUS_M + station_height_amsl_m
    thickness = rain_top_height_amsl_m - station_height_amsl_m
    along = radius * sin(radians(geometric_elevation_deg))
    difference = thickness * (2 * radius + thickness)
    return difference / (sqrt(along * along + difference) + along)


def evaluate_hydrometeor_path(
    frequency_hz: float,
    geometric_elevation_deg: float,
    station_height_amsl_m: float,
    station: HydrometeorStation,
) -> HydrometeorPathResult:
    """P.840-9 cloud loss and P.838-3 rain dB/km integrated to the declared top.

    The positive root of s² + 2*r*sin(elevation)*s = top_radius²-r²
    is rationalized to preserve precision for thin layers, including zenith.
    """
    try:
        _range(frequency_hz, "hydrometeors frequency_hz", 1e9, 200e9)
    except OverflowError as exc:
        raise ValueError("hydrometeor path inputs must be finite") from exc
    if not isinstance(station, HydrometeorStation):
        raise ValueError("hydrometeor station must be a HydrometeorStation")  # noqa: TRY004
    length_m = rain_layer_path_length_m(
        station_height_amsl_m, station.rain_top_height_amsl_m, geometric_elevation_deg
    )
    cloud = cloud_slant_attenuation(
        frequency_hz, station.liquid_water_kg_m2, geometric_elevation_deg
    )
    rain = rain_specific_attenuation(
        frequency_hz, station.rain_rate_mm_h, geometric_elevation_deg, station.polarization_tilt_deg
    )
    rain_db = _non_negative_number(
        rain.specific_attenuation_db_per_km * (length_m / 1000), "rain_attenuation_db"
    )
    total_db = _non_negative_number(cloud.attenuation_db + rain_db, "hydrometeor_attenuation_db")
    return HydrometeorPathResult(
        cloud.attenuation_db, rain.specific_attenuation_db_per_km, length_m, rain_db, total_db
    )
