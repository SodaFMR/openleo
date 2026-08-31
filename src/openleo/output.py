"""Deterministic trace and summary serialization."""

from __future__ import annotations

import csv
import json
from datetime import UTC, datetime
from importlib.metadata import version
from math import isfinite
from pathlib import Path
from typing import Any

from openleo.simulation import SimulationResult, TraceRow

CSV_FIELDS = (
    "timestamp_utc",
    "azimuth_deg",
    "elevation_deg",
    "range_m",
    "range_rate_mps",
    "delay_s",
    "doppler_hz",
    "free_space_path_loss_db",
    "received_carrier_power_dbw",
    "noise_density_dbw_per_hz",
    "carrier_to_noise_density_db_hz",
    "signal_to_noise_ratio_db",
    "shannon_capacity_upper_bound_bps",
)

LIMITATIONS = (
    "free-space-only propagation; no atmospheric gases, rain, cloud, fog, scintillation, or instantaneous weather",
    "geometric elevation only; no atmospheric refraction",
    "synthetic RF parameters; not a real commercial constellation performance claim",
    "Shannon-Hartley capacity is a theoretical upper bound, not throughput or achieved goodput",
    "sample-grid AOS/LOS estimates; true elevation-mask crossings are not interpolated",
    "SGP4 propagation from public mean elements; GP element age is a quality indicator, not a covariance or accuracy guarantee",
    "no calibrated-observation validation; SatNOGS observations are qualitative sanity checks unless a complete calibrated acquisition chain is documented",
)


def write_result(result: SimulationResult, output_dir: str | Path) -> tuple[Path, Path]:
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    csv_path = directory / "trace.csv"
    json_path = directory / "summary.json"

    with csv_path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=CSV_FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(_csv_row(row) for row in result.rows)

    json_path.write_text(
        _json_dumps(_summary(result)) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return csv_path, json_path


def _csv_row(row: TraceRow) -> dict[str, str]:
    return {
        "timestamp_utc": _utc(row.timestamp_utc),
        "azimuth_deg": _float(row.azimuth_deg),
        "elevation_deg": _float(row.elevation_deg),
        "range_m": _float(row.range_m),
        "range_rate_mps": _float(row.range_rate_mps),
        "delay_s": _float(row.propagation_delay_s),
        "doppler_hz": _float(row.doppler_hz),
        "free_space_path_loss_db": _float(row.free_space_path_loss_db),
        "received_carrier_power_dbw": _float(row.received_carrier_power_dbw),
        "noise_density_dbw_per_hz": _float(row.noise_density_dbw_per_hz),
        "carrier_to_noise_density_db_hz": _float(row.carrier_to_noise_density_db_hz),
        "signal_to_noise_ratio_db": _float(row.signal_to_noise_ratio_db),
        "shannon_capacity_upper_bound_bps": _float(row.capacity_upper_bound_bps),
    }


def _summary(result: SimulationResult) -> dict[str, Any]:
    scenario = result.scenario
    summary = result.summary
    return {
        "schema_version": "1",
        "scenario_name": scenario.name,
        "provenance": {
            "scenario": {"sha256": scenario.source_sha256},
            "orbit": {
                "source_url": scenario.orbit.provenance.source_url,
                "retrieved_at_utc": _utc(scenario.orbit.provenance.retrieved_at_utc),
                "terms_url": scenario.orbit.provenance.terms_url,
                "sha256": scenario.orbit.provenance.sha256,
            },
            "time": {
                "leap_second_table_source": summary.leap_second_table_source,
                "leap_second_table_sha256": summary.leap_second_table_sha256,
            },
        },
        "inputs": {
            "ground_station": {
                "name": scenario.ground_station.name,
                "latitude_deg": scenario.ground_station.latitude_deg,
                "longitude_deg": scenario.ground_station.longitude_deg,
                "height_m": scenario.ground_station.height_m,
            },
            "time_window": {
                "start_utc": _utc(scenario.time_window.start_utc),
                "stop_utc": _utc(scenario.time_window.stop_utc),
                "step_s": scenario.time_window.step_s,
                "minimum_elevation_deg": scenario.time_window.minimum_elevation_deg,
            },
            "radio_link": {
                "carrier_frequency_hz": scenario.radio_link.carrier_frequency_hz,
                "channel_bandwidth_hz": scenario.radio_link.channel_bandwidth_hz,
                "eirp_dbw": scenario.radio_link.eirp_dbw,
                "receiver_gain_dbi": scenario.radio_link.receiver_gain_dbi,
                "system_noise_temperature_k": scenario.radio_link.system_noise_temperature_k,
                "miscellaneous_loss_db": scenario.radio_link.miscellaneous_loss_db,
            },
        },
        "versions": {
            "openleo-link": version("openleo-link"),
            "skyfield": version("skyfield"),
            "sgp4": version("sgp4"),
        },
        "warnings": list(summary.warnings),
        "limitations": list(LIMITATIONS),
        "row_count": len(result.rows),
        "sampled_aos_utc": _utc(summary.sampled_aos_utc),
        "sampled_los_utc": _utc(summary.sampled_los_utc),
        "sampling_interval_s": summary.sampling_interval_s,
        "element_epoch_utc": _utc(summary.element_epoch_utc),
        "start_element_age_days": summary.start_element_age_days,
        "stop_element_age_days": summary.stop_element_age_days,
        "maximum_absolute_element_age_days": summary.maximum_absolute_element_age_days,
        "sampled_duration_s": summary.sampled_duration_s,
        "extrema": {
            "maximum_elevation_deg": summary.maximum_elevation_deg,
            "minimum_range_m": summary.minimum_range_m,
            "maximum_capacity_upper_bound_bps": summary.maximum_capacity_upper_bound_bps,
        },
        "integrated_capacity_upper_bound_bits": summary.integrated_capacity_upper_bound_bits,
    }


def _utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _float(value: float) -> str:
    return format(value, ".12g")


def _json_dumps(value: Any) -> str:
    return json.dumps(
        _json_value(value),
        indent=2,
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
    )


def _json_value(value: Any) -> Any:
    if isinstance(value, float):
        if not isfinite(value):
            raise ValueError(f"out of range float values are not JSON compliant: {value!r}")
        return float(format(value, ".12g"))
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value
