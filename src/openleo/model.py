"""Immutable OpenLEO scenario models."""

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Provenance:
    source_url: str
    retrieved_at_utc: datetime
    terms_url: str
    sha256: str


@dataclass(frozen=True, slots=True)
class OrbitSource:
    path: Path
    provenance: Provenance


@dataclass(frozen=True, slots=True)
class GroundStation:
    name: str
    latitude_deg: float
    longitude_deg: float
    height_m: float


@dataclass(frozen=True, slots=True)
class TimeWindow:
    start_utc: datetime
    stop_utc: datetime
    step_s: float
    minimum_elevation_deg: float


@dataclass(frozen=True, slots=True)
class RadioLink:
    carrier_frequency_hz: float
    channel_bandwidth_hz: float
    eirp_dbw: float
    receiver_gain_dbi: float
    system_noise_temperature_k: float
    miscellaneous_loss_db: float


@dataclass(frozen=True, slots=True)
class Scenario:
    name: str
    source_sha256: str
    orbit: OrbitSource
    ground_station: GroundStation
    time_window: TimeWindow
    radio_link: RadioLink
