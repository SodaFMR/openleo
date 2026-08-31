"""End-to-end pass simulation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha256
from itertools import pairwise
from math import isclose

from skyfield.api import load, wgs84

from openleo.input import MAX_TIME_GRID_SAMPLES
from openleo.model import RadioLink, Scenario
from openleo.orbit import load_orbit
from openleo.physics import (
    carrier_to_noise_density_db_hz,
    doppler_shift_hz,
    free_space_path_loss_db,
    noise_density_dbw_per_hz,
    propagation_delay_s,
    received_carrier_power_dbw,
    shannon_capacity_upper_bound_bps,
    signal_to_noise_ratio_db,
)


@dataclass(frozen=True, slots=True)
class TraceRow:
    timestamp_utc: datetime
    azimuth_deg: float
    elevation_deg: float
    range_m: float
    range_rate_mps: float
    propagation_delay_s: float
    doppler_hz: float
    free_space_path_loss_db: float
    received_carrier_power_dbw: float
    noise_density_dbw_per_hz: float
    carrier_to_noise_density_db_hz: float
    signal_to_noise_ratio_db: float
    capacity_upper_bound_bps: float


@dataclass(frozen=True, slots=True)
class SimulationSummary:
    sampled_aos_utc: datetime
    sampled_los_utc: datetime
    sampling_interval_s: float
    element_epoch_utc: datetime
    start_element_age_days: float
    stop_element_age_days: float
    maximum_absolute_element_age_days: float
    leap_second_table_source: str
    leap_second_table_sha256: str
    sampled_duration_s: float
    maximum_elevation_deg: float
    minimum_range_m: float
    maximum_capacity_upper_bound_bps: float
    integrated_capacity_upper_bound_bits: float
    warnings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SimulationResult:
    rows: tuple[TraceRow, ...]
    summary: SimulationSummary
    scenario: Scenario


def simulate_scenario(scenario: Scenario) -> SimulationResult:
    step, sample_count = _validated_time_grid(scenario)
    timescale = load.timescale(builtin=True)
    orbit = load_orbit(scenario.orbit, timescale)
    station = wgs84.latlon(
        scenario.ground_station.latitude_deg,
        scenario.ground_station.longitude_deg,
        elevation_m=scenario.ground_station.height_m,
    )

    sampled_rows = tuple(
        _row_at(scenario.radio_link, orbit.satellite, station, timescale, timestamp)
        for timestamp in _timestamps(scenario, step, sample_count)
    )
    visible = tuple(
        row.elevation_deg >= scenario.time_window.minimum_elevation_deg for row in sampled_rows
    )

    if visible[0]:
        raise ValueError("time window starts during a visible pass; start earlier")
    if visible[-1]:
        raise ValueError("time window ends during a visible pass; stop later")

    segments = _visible_segments(sampled_rows, visible)
    if not segments:
        raise ValueError("no visible pass in time window")
    if len(segments) != 1:
        raise ValueError("expected exactly one visible pass in time window")

    rows = segments[0]
    capacities = tuple(row.capacity_upper_bound_bps for row in rows)
    warnings = ()
    start_element_age_days = (
        scenario.time_window.start_utc - orbit.epoch_utc
    ).total_seconds() / 86_400.0
    stop_element_age_days = (
        scenario.time_window.stop_utc - orbit.epoch_utc
    ).total_seconds() / 86_400.0
    maximum_absolute_element_age_days = max(abs(start_element_age_days), abs(stop_element_age_days))
    if maximum_absolute_element_age_days > 14.0:
        warnings = ("maximum requested time is more than 14 days from the element epoch",)

    return SimulationResult(
        rows=rows,
        summary=SimulationSummary(
            sampled_aos_utc=rows[0].timestamp_utc,
            sampled_los_utc=rows[-1].timestamp_utc,
            sampling_interval_s=scenario.time_window.step_s,
            element_epoch_utc=orbit.epoch_utc,
            start_element_age_days=start_element_age_days,
            stop_element_age_days=stop_element_age_days,
            maximum_absolute_element_age_days=maximum_absolute_element_age_days,
            leap_second_table_source="skyfield-builtin",
            leap_second_table_sha256=_leap_second_table_sha256(timescale),
            sampled_duration_s=(rows[-1].timestamp_utc - rows[0].timestamp_utc).total_seconds(),
            maximum_elevation_deg=max(row.elevation_deg for row in rows),
            minimum_range_m=min(row.range_m for row in rows),
            maximum_capacity_upper_bound_bps=max(capacities),
            integrated_capacity_upper_bound_bits=_trapezoid_capacity_bits(rows),
            warnings=warnings,
        ),
        scenario=scenario,
    )


def _validated_time_grid(scenario: Scenario) -> tuple[timedelta, int]:
    window = scenario.time_window
    if (
        window.start_utc.tzinfo is None
        or window.stop_utc.tzinfo is None
        or window.start_utc.utcoffset() != timedelta(0)
        or window.stop_utc.utcoffset() != timedelta(0)
    ):
        raise ValueError("time window timestamps must be timezone-aware UTC")
    if window.start_utc >= window.stop_utc:
        raise ValueError("time_window.stop_utc must be after time_window.start_utc")
    duration_s = (window.stop_utc - window.start_utc).total_seconds()
    if window.step_s > duration_s:
        raise ValueError(
            f"time_window.step_s={window.step_s!r} must be no larger than "
            f"the window duration {duration_s}"
        )
    step = timedelta(seconds=window.step_s)
    represented_step_s = step.total_seconds()
    if represented_step_s <= 0.0 or not isclose(
        represented_step_s, window.step_s, rel_tol=0.0, abs_tol=1e-12
    ):
        raise ValueError(
            f"time_window.step_s={window.step_s!r} must be positive and exactly "
            "representable at microsecond resolution"
        )
    quotient, remainder = divmod(window.stop_utc - window.start_utc, step)
    sample_count = quotient + 1 + bool(remainder)
    if sample_count > MAX_TIME_GRID_SAMPLES:
        raise ValueError(
            f"time_window.step_s={window.step_s!r} produces {sample_count} inclusive "
            f"samples; limit is {MAX_TIME_GRID_SAMPLES}; increase time_window.step_s or "
            "shorten the window"
        )
    return step, sample_count


def _timestamps(scenario: Scenario, step: timedelta, sample_count: int):
    for index in range(sample_count - 1):
        yield scenario.time_window.start_utc + index * step
    yield scenario.time_window.stop_utc


def _leap_second_table_sha256(timescale) -> str:
    payload = {
        "leap_dates": [float(value) for value in timescale.leap_dates],
        "leap_offsets": [float(value) for value in timescale.leap_offsets],
    }
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return sha256(serialized.encode("utf-8")).hexdigest()


def _row_at(link: RadioLink, satellite, station, timescale, timestamp) -> TraceRow:
    elevation, azimuth, distance, _, _, range_rate = (
        (satellite - station).at(timescale.from_datetime(timestamp)).frame_latlon_and_rates(station)
    )
    range_m = distance.m
    range_rate_mps = range_rate.m_per_s
    path_loss_db = free_space_path_loss_db(range_m, link.carrier_frequency_hz)
    carrier_power_dbw = received_carrier_power_dbw(
        link.eirp_dbw,
        link.receiver_gain_dbi,
        path_loss_db,
        link.miscellaneous_loss_db,
    )
    noise_dbw_per_hz = noise_density_dbw_per_hz(link.system_noise_temperature_k)
    cn0_db_hz = carrier_to_noise_density_db_hz(carrier_power_dbw, noise_dbw_per_hz)
    snr_db = signal_to_noise_ratio_db(cn0_db_hz, link.channel_bandwidth_hz)

    return TraceRow(
        timestamp_utc=timestamp,
        azimuth_deg=azimuth.degrees,
        elevation_deg=elevation.degrees,
        range_m=range_m,
        range_rate_mps=range_rate_mps,
        propagation_delay_s=propagation_delay_s(range_m),
        doppler_hz=doppler_shift_hz(link.carrier_frequency_hz, range_rate_mps),
        free_space_path_loss_db=path_loss_db,
        received_carrier_power_dbw=carrier_power_dbw,
        noise_density_dbw_per_hz=noise_dbw_per_hz,
        carrier_to_noise_density_db_hz=cn0_db_hz,
        signal_to_noise_ratio_db=snr_db,
        capacity_upper_bound_bps=shannon_capacity_upper_bound_bps(
            snr_db, link.channel_bandwidth_hz
        ),
    )


def _visible_segments(rows: tuple[TraceRow, ...], visible: tuple[bool, ...]):
    segments = []
    current = []
    for row, is_visible in zip(rows, visible, strict=True):
        if is_visible:
            current.append(row)
        elif current:
            segments.append(tuple(current))
            current = []
    if current:
        segments.append(tuple(current))
    return tuple(segments)


def _trapezoid_capacity_bits(rows: tuple[TraceRow, ...]) -> float:
    return sum(
        (
            (previous.capacity_upper_bound_bps + current.capacity_upper_bound_bps)
            * 0.5
            * (current.timestamp_utc - previous.timestamp_utc).total_seconds()
        )
        for previous, current in pairwise(rows)
    )
