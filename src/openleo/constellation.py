"""Reproducible, bounded multi-satellite geometry and adaptive reference rates."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from importlib.metadata import version
from itertools import pairwise
from math import fsum
from os.path import relpath
from pathlib import Path
from typing import Any

import numpy as np
from skyfield.api import load, wgs84
from skyfield.framelib import itrs

from openleo.adaptation import ETSI_SOURCE_URL, MODCODS, AdaptationConfig, select_modcod
from openleo.catalog import load_catalog
from openleo.input import (
    _finite_number,
    _ground_station,
    _object,
    _orbit,
    _radio_link,
    _read_scenario_bytes,
    _sha256,
    _string,
    _time_window,
)
from openleo.model import GroundStation, OrbitSource, RadioLink, TimeWindow
from openleo.network import NetworkConfig, parse_network
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
from openleo.simulation import _leap_second_table_sha256, _timestamps, _validated_time_grid

CONSTELLATION_KEYS = frozenset(
    ("name", "orbit", "stations", "time_window", "radio_link", "adaptation", "network")
)
ADAPTATION_KEYS = frozenset(
    ("symbol_rate_baud", "rolloff", "implementation_margin_db", "hysteresis_db")
)
MAX_STATIONS = 16
MAX_SAMPLES = 1441
MAX_LINK_SAMPLES = 500_000


@dataclass(frozen=True, slots=True)
class ConstellationScenario:
    name: str
    source_sha256: str
    source_path: Path
    orbit: OrbitSource
    stations: tuple[GroundStation, ...]
    time_window: TimeWindow
    radio_link: RadioLink
    adaptation: AdaptationConfig
    network: NetworkConfig


def load_constellation(path: str | Path) -> ConstellationScenario:
    """Read bounded UTF-8 JSON, fingerprinting its original bytes."""
    scenario_path = Path(path)
    try:
        source = _read_scenario_bytes(scenario_path)
        raw = json.loads(source.decode("utf-8"), object_pairs_hook=_unique_object)
        return parse_constellation(raw, scenario_path, sha256(source).hexdigest())
    except UnicodeDecodeError as exc:
        raise ValueError(f"{scenario_path}: scenario JSON must be UTF-8") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"{scenario_path}: invalid JSON: {exc.msg}") from exc
    except OSError as exc:
        raise ValueError(f"{scenario_path}: could not load scenario: {exc}") from exc


def _unique_object(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("scenario JSON contains duplicate object keys")
    return result


def parse_constellation(raw: Any, path: str | Path, source_sha256: str) -> ConstellationScenario:
    """Validate parsed JSON; resolve the archived catalog relative to ``path``.

    A live server must pin the orbit object to the initial configuration before
    calling this parser, because this local-file API also supports CLI input.
    """
    scenario_path = Path(path).resolve()
    try:
        data = _object(raw, CONSTELLATION_KEYS, "scenario")
        stations = _stations(data["stations"])
        radio = _radio_link(data["radio_link"])
        adaptation = AdaptationConfig(**_object(data["adaptation"], ADAPTATION_KEYS, "adaptation"))
        if radio.channel_bandwidth_hz < adaptation.symbol_rate_baud * (1 + adaptation.rolloff):
            raise ValueError(
                "radio_link.channel_bandwidth_hz must be >= "
                "adaptation.symbol_rate_baud * (1 + adaptation.rolloff)"
            )
        scenario = ConstellationScenario(
            name=_string(data["name"], "name"),
            source_sha256=_sha256(source_sha256, "source_sha256"),
            source_path=scenario_path,
            orbit=_orbit(data["orbit"], scenario_path),
            stations=stations,
            time_window=_time_window(data["time_window"]),
            radio_link=radio,
            adaptation=adaptation,
            network=parse_network(data["network"], tuple(station.name for station in stations)),
        )
        _bounded_grid(scenario)
        return scenario
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{scenario_path}: invalid constellation scenario: {exc}") from exc


def _stations(raw: Any) -> tuple[GroundStation, ...]:
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_STATIONS:
        raise ValueError(f"stations must be a list of 1 to {MAX_STATIONS} ground stations")
    stations = tuple(_ground_station(station) for station in raw)
    if len({station.name for station in stations}) != len(stations):
        raise ValueError("station names must be unique")
    return stations


def _bounded_grid(scenario: ConstellationScenario) -> tuple[timedelta, int]:
    step, count = _validated_time_grid(scenario)
    if count > MAX_SAMPLES:
        raise ValueError(f"time window produces {count} inclusive samples; limit is {MAX_SAMPLES}")
    return step, count


def _utc_text(timestamp: datetime) -> str:
    if not isinstance(timestamp, datetime) or timestamp.utcoffset() != timedelta(0):
        raise ValueError("timestamps must be timezone-aware UTC")
    return timestamp.astimezone(UTC).isoformat().replace("+00:00", "Z")


def scenario_document(scenario: ConstellationScenario) -> dict[str, Any]:
    """Return an independent input document, with a relative catalog path."""
    provenance = scenario.orbit.provenance
    return {
        "name": scenario.name,
        "orbit": {
            "path": Path(relpath(scenario.orbit.path, scenario.source_path.parent)).as_posix(),
            "source_url": provenance.source_url,
            "retrieved_at_utc": _utc_text(provenance.retrieved_at_utc),
            "terms_url": provenance.terms_url,
            "sha256": provenance.sha256,
        },
        "stations": [asdict(station) for station in scenario.stations],
        "time_window": {
            **asdict(scenario.time_window),
            "start_utc": _utc_text(scenario.time_window.start_utc),
            "stop_utc": _utc_text(scenario.time_window.stop_utc),
        },
        "radio_link": asdict(scenario.radio_link),
        "adaptation": asdict(scenario.adaptation),
        "network": asdict(scenario.network),
    }


def simulate_constellation(scenario: ConstellationScenario) -> dict[str, Any]:
    """Produce a new JSON-compatible experiment; never change input objects."""
    scenario = parse_constellation(
        scenario_document(scenario), scenario.source_path, scenario.source_sha256
    )
    step, count = _bounded_grid(scenario)
    timescale = load.timescale(builtin=True)
    catalog = load_catalog(scenario.orbit, timescale)
    samples = len(catalog) * len(scenario.stations) * count
    if samples > MAX_LINK_SAMPLES:
        raise ValueError(f"{samples} satellite-station-samples exceed limit {MAX_LINK_SAMPLES}")
    timestamps = tuple(_timestamps(scenario, step, count))
    times = timescale.from_datetimes(timestamps)
    states = tuple(_propagate(orbit, times, timestamps) for orbit in catalog)
    sites = tuple(
        wgs84.latlon(site.latitude_deg, site.longitude_deg, elevation_m=site.height_m)
        for site in scenario.stations
    )
    satellites = [
        _satellite_document(orbit, state, timestamps) for orbit, state in zip(catalog, states)
    ]
    links = _links(scenario, timestamps, times, states, sites)
    return {
        "schema_version": "1",
        "kind": "openleo.constellation",
        "scenario": scenario_document(scenario),
        "provenance": _provenance(scenario),
        "models": _models(timescale),
        "warnings": [
            f"NORAD {satellite['norad_id']}: maximum requested time is more than 14 days "
            f"from the element epoch ({satellite['maximum_absolute_element_age_days']:.6g} days)"
            for satellite in satellites
            if satellite["maximum_absolute_element_age_days"] > 14.0
        ],
        "limitations": _limitations(),
        "timestamps_utc": [_utc_text(timestamp) for timestamp in timestamps],
        "satellites": satellites,
        "stations": [
            {**asdict(station), "position_ecef_m": site.itrs_xyz.m.tolist()}
            for station, site in zip(scenario.stations, sites, strict=True)
        ],
        "links": links,
        "statistics": _statistics(scenario, timestamps, links),
    }


def _propagate(orbit, times, timestamps):
    state = orbit.satellite.at(times)
    for timestamp, error in zip(timestamps, state.message, strict=True):
        if error:
            raise ValueError(
                f"SGP4 error for NORAD {orbit.satellite.model.satnum} at {_utc_text(timestamp)}: {error}"
            )
    if not np.isfinite(state.position.km).all() or not np.isfinite(state.velocity.km_per_s).all():
        raise ValueError(
            f"SGP4 produced a non-finite state for NORAD {orbit.satellite.model.satnum}"
        )
    return state


def _satellite_document(orbit, state, timestamps):
    return {
        "name": orbit.satellite.name,
        "norad_id": int(orbit.satellite.model.satnum),
        "epoch_utc": _utc_text(orbit.epoch_utc),
        "maximum_absolute_element_age_days": max(
            abs((timestamp - orbit.epoch_utc).total_seconds()) / 86_400.0
            for timestamp in (timestamps[0], timestamps[-1])
        ),
        "positions_ecef_m": state.frame_xyz(itrs).m.T.tolist(),
    }


def _links(scenario, timestamps, times, states, sites):
    frames = [[] for _ in timestamps]
    for station_index, site in enumerate(sites):
        site_state = site.at(times)
        for satellite_index, state in enumerate(states):
            elevation, azimuth, distance, range_rate = _topocentric(
                state, site_state, site, scenario.stations[station_index].name
            )
            visible = elevation.degrees >= scenario.time_window.minimum_elevation_deg
            contacts = _remaining_contacts(timestamps, visible)
            previous = None
            for index, is_visible in enumerate(visible):
                if not is_visible:
                    previous = None
                    continue
                radio = _radio_metrics(scenario.radio_link, float(distance.m[index]))
                esn0_db = signal_to_noise_ratio_db(
                    radio["cn0_db_hz"], scenario.adaptation.symbol_rate_baud
                )
                previous = select_modcod(esn0_db, scenario.adaptation, previous)
                required_db = (previous or MODCODS[0]).required_esn0_db
                range_rate_mps = float(range_rate.m_per_s[index])
                remaining_s, truncated = contacts[index]
                frames[index].append(
                    {
                        "station_index": station_index,
                        "satellite_index": satellite_index,
                        "elevation_deg": float(elevation.degrees[index]),
                        "azimuth_deg": float(azimuth.degrees[index]),
                        "range_m": float(distance.m[index]),
                        "range_rate_mps": range_rate_mps,
                        "doppler_hz": doppler_shift_hz(
                            scenario.radio_link.carrier_frequency_hz, range_rate_mps
                        ),
                        **radio,
                        "esn0_db": esn0_db,
                        "modcod": previous.name if previous else None,
                        "rate_bps": scenario.adaptation.symbol_rate_baud
                        * previous.efficiency_bits_per_symbol
                        if previous
                        else 0.0,
                        "margin_db": esn0_db
                        - scenario.adaptation.implementation_margin_db
                        - required_db,
                        "remaining_contact_s": remaining_s,
                        "contact_truncated": truncated,
                    }
                )
    return frames


def _topocentric(state, site_state, site, station_name):
    # Skyfield also calculates unused angular derivatives; singular derivatives
    # need not invalidate finite elevation, azimuth, range and radial velocity.
    with np.errstate(over="ignore", invalid="ignore", divide="ignore"):
        elevation, azimuth, distance, _, _, range_rate = (
            state - site_state
        ).frame_latlon_and_rates(site)
    values = (elevation.degrees, azimuth.degrees, distance.m, range_rate.m_per_s)
    if not all(np.isfinite(value).all() for value in values):
        raise ValueError(f"non-finite topocentric geometry for station {station_name}")
    return elevation, azimuth, distance, range_rate


def _remaining_contacts(timestamps, visible):
    boundary = len(timestamps) - 1
    truncated = True
    reverse = []
    for index in range(len(timestamps) - 1, -1, -1):
        if not visible[index]:
            boundary, truncated = index, False
        reverse.append(((timestamps[boundary] - timestamps[index]).total_seconds(), truncated))
    return tuple(reversed(reverse))


def _radio_metrics(radio: RadioLink, range_m: float) -> dict[str, float]:
    path_loss_db = free_space_path_loss_db(range_m, radio.carrier_frequency_hz)
    carrier_power_dbw = received_carrier_power_dbw(
        radio.eirp_dbw, radio.receiver_gain_dbi, path_loss_db, radio.miscellaneous_loss_db
    )
    cn0_db_hz = carrier_to_noise_density_db_hz(
        carrier_power_dbw, noise_density_dbw_per_hz(radio.system_noise_temperature_k)
    )
    snr_db = signal_to_noise_ratio_db(cn0_db_hz, radio.channel_bandwidth_hz)
    return {
        "delay_s": propagation_delay_s(range_m),
        "cn0_db_hz": cn0_db_hz,
        "snr_db": snr_db,
        "shannon_upper_bound_bps": _finite_number(
            shannon_capacity_upper_bound_bps(snr_db, radio.channel_bandwidth_hz),
            "shannon_upper_bound_bps",
        ),
    }


def _statistics(scenario, timestamps, frames):
    intervals = tuple((end - start).total_seconds() for start, end in pairwise(timestamps))
    records = []
    for station_index, station in enumerate(scenario.stations):
        visible = tuple(
            tuple(link for link in frame if link["station_index"] == station_index)
            for frame in frames
        )
        best = tuple(
            max(
                (link for link in links if link["rate_bps"] > 0),
                key=lambda link: (link["rate_bps"], -link["satellite_index"]),
                default=None,
            )
            for links in visible
        )
        records.append(
            {
                "name": station.name,
                "station_index": station_index,
                "visible_sample_fraction": sum(bool(links) for links in visible) / len(frames),
                "usable_sample_fraction": sum(link is not None for link in best) / len(frames),
                "best_link_integrated_bits": _finite_number(
                    fsum(
                        (link["rate_bps"] if link else 0.0) * interval
                        for link, interval in zip(best, intervals)
                    ),
                    "best_link_integrated_bits",
                ),
                "fixed_baseline_integrated_bits": _finite_number(
                    fsum(
                        scenario.network.fixed_capacity_bps * interval
                        for links, interval in zip(visible, intervals)
                        if links
                    ),
                    "fixed_baseline_integrated_bits",
                ),
                "handover_count": sum(
                    bool(first and second and first["satellite_index"] != second["satellite_index"])
                    for first, second in pairwise(best)
                ),
            }
        )
    return records


def _provenance(scenario):
    provenance = scenario.orbit.provenance
    return {
        "scenario_sha256": scenario.source_sha256,
        "orbit": {
            "source_url": provenance.source_url,
            "retrieved_at_utc": _utc_text(provenance.retrieved_at_utc),
            "terms_url": provenance.terms_url,
            "sha256": provenance.sha256,
        },
        "software": {name: version(name) for name in ("openleo-link", "skyfield", "sgp4", "numpy")},
    }


def _models(timescale):
    return {
        "coordinates": {
            "frame": "ITRS",
            "units": "metres",
            "ellipsoid": "WGS84",
            "equatorial_radius_m": float(wgs84.radius.m),
            "inverse_flattening": float(wgs84.inverse_flattening),
            "orbit_propagator": "SGP4 with WGS72 gravity, TEME GP elements transformed by Skyfield",
            "earth_orientation": "Skyfield builtin time data; polar motion is not supplied",
        },
        "time": {
            "leap_second_table_source": "skyfield-builtin",
            "leap_second_table_sha256": _leap_second_table_sha256(timescale),
            "sampling": "Inclusive UTC datetime grid; final sample is the exact stop, even with a shorter last interval",
            "integration": "Left-hold rates over adjacent sample intervals; UTC datetime differences (POSIX convention)",
        },
        "radio_link": {
            "channel": "Free-space AWGN; one declared reciprocal radio link budget for every satellite and station",
            "doppler": "First order: -carrier_frequency_hz * range_rate_mps / c; receding is negative",
            "delay": "One-way instantaneous geometric range / c",
            "capacity": "Shannon AWGN upper bound, not traffic throughput",
            "constants": "c = 299792458 m/s; k = 1.380649e-23 J/K (exact SI)",
        },
        "adaptation": {
            "reference": "ETSI EN 302 307-1 V1.4.1 (2014-11), Table 13",
            "source_url": ETSI_SOURCE_URL,
            "assumptions": "DVB-S2 normal 64800-bit frames without pilots, ideal AWGN reference requirements; scenario implementation margin added",
            "modcod_table": [asdict(modcod) for modcod in MODCODS],
            "esn0": "C/N0 - 10*log10(symbol_rate_baud)",
            "rate": "symbol_rate_baud * spectral efficiency; no MAC, network or application overhead deduction",
            "occupied_bandwidth": "symbol_rate_baud * (1 + rolloff) <= channel_bandwidth_hz",
            "hysteresis": "Upgrades of an existing lock only; immediate nominal-threshold downgrade; no-contact resets lock",
            "margin_db": "Es/N0 minus implementation margin and selected nominal requirement; lowest requirement when unlocked",
        },
        "contacts": {
            "visibility": "Geometric elevation >= scenario mask; independent of RF lock",
            "remaining_contact_s": "Time to first sampled below-mask instant, or stop when truncated; no boundary interpolation",
            "contact_truncated": "True when this sampled contact remains above mask at the window stop",
        },
        "statistics": {
            "fractions": "Counts of qualifying samples divided by all inclusive samples; not service probabilities",
            "best_link": "One greatest positive-rate ground link per station; equal rates prefer lowest NORAD ID",
            "handover_count": "Best-link satellite changes between adjacent usable samples; outages reset selection",
            "fixed_baseline": "Same geometric visibility with declared fixed_capacity_bps per ground link, even when adaptive RF lock fails",
        },
        "network": {
            "links": "Simultaneous reciprocal snapshots; range-limited ISLs with WGS84 Earth occlusion",
            "relay": "Only source and target ground stations participate; intermediate stations do not relay",
            "baseline": "Only ground-link rates become fixed_capacity_bps; ISLs retain isl_capacity_bps",
        },
    }


def _limitations():
    return [
        "Archived GP elements are propagated predictions, not measured satellite positions; age is reported without an uncertainty distribution.",
        "Station coordinates and every RF/ISL parameter are declared experiment assumptions, not surveyed infrastructure or real constellation specifications.",
        "DVB-S2 reference rates are synthetic link adaptation, not Iridium waveform or receiver measurements.",
        "No atmosphere, rain, scintillation, terrain, antenna patterns, interference, RF acquisition delay or tracking dynamics are modeled.",
        "Contact boundaries and route choices are sampled; finer events between samples can be missed.",
        "UTC datetime integration uses the POSIX convention; inserted leap seconds are not explicit grid samples or additional integration seconds.",
        "Snapshot path bottlenecks omit link scheduling, contention, queues, packet loss, TCP and protocol overhead; they are not measured throughput.",
    ]
