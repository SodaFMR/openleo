"""v1 import path of the single-link replay, which moved to ``openleo.replay.packet_replay``.

The v1 experiment and benchmark modules still replay v1 workbench bundles: this module keeps
v1's document-based ``prepare_packet_replay`` and bundle-based ``run_packet_replay`` and
re-exports everything else from the new home, until the v1 modules are removed.
"""

from __future__ import annotations

import json
from bisect import bisect_right
from dataclasses import asdict
from datetime import timedelta
from hashlib import sha256
from math import floor
from pathlib import Path

from openleo._validation import _utc
from openleo.replay.packet_replay import (
    BACKEND_VERSION,
    MAX_OFFERED_PACKETS,
    MAX_RATE_BPS,
    MAX_RESULT_BYTES,
    PACKET_FIELDS,
    STATUSES,
    V1_LIMITATIONS,
    ReplayConfig,
    _check_output_target,
    _csv_integer,
    _execute_packet_replay,
    _file_sha256,
    _integer,
    _json_text,
    _microseconds,
    _nearest_serialization_ns,
    _number,
    _overlaps,
    _read_result,
    _run_backend,
    _trace_csv,
    _trace_state,
    _utc_text,
    _validate_config,
    _validate_prepared,
    parse_packet_results,
)
from openleo.workbench import (
    ARTIFACT_NAMES,
    MAX_ARTIFACT_BYTES,
    _read,
    _validate_document,
    load_experiment,
)

__all__ = [
    "BACKEND_VERSION",
    "MAX_OFFERED_PACKETS",
    "MAX_RATE_BPS",
    "MAX_RESULT_BYTES",
    "PACKET_FIELDS",
    "STATUSES",
    "ReplayConfig",
    "_check_output_target",
    "_csv_integer",
    "_file_sha256",
    "_integer",
    "_json_text",
    "_microseconds",
    "_nearest_serialization_ns",
    "_number",
    "_overlaps",
    "_read_result",
    "_run_backend",
    "_source_metadata",
    "_trace_csv",
    "_trace_state",
    "_utc_text",
    "_validate_config",
    "_validate_prepared",
    "parse_packet_results",
    "prepare_packet_replay",
    "run_packet_replay",
]


def _state(document: dict, frame_index: int, station_index: int, satellite_index: int):
    return next(
        (
            link
            for link in document["links"][frame_index]
            if link["station_index"] == station_index and link["satellite_index"] == satellite_index
        ),
        None,
    )


def prepare_packet_replay(document: dict, config: ReplayConfig) -> dict:
    """Validate and clip one explicitly selected ground link into integer ns-3 input."""
    _validate_document(document)
    start_us, duration_us, interval_ns = _validate_config(config)
    timestamps = tuple(_utc(value, "timestamps_utc") for value in document["timestamps_utc"])
    span = timestamps[-1] - timestamps[0]
    span_us = (span.days * 86_400 + span.seconds) * 1_000_000 + span.microseconds
    if start_us > span_us or duration_us > span_us - start_us:
        raise ValueError("requested replay window must lie fully inside the experiment window")
    origin = timestamps[0] + timedelta(microseconds=start_us)
    stop = origin + timedelta(microseconds=duration_us)
    try:
        station_index = next(
            index
            for index, station in enumerate(document["stations"])
            if station["name"] == config.station_name
        )
    except StopIteration as exc:
        raise ValueError(f"station {config.station_name!r} is not in the experiment") from exc
    try:
        satellite_index = next(
            index
            for index, satellite in enumerate(document["satellites"])
            if satellite["norad_id"] == config.norad_id
        )
    except StopIteration as exc:
        raise ValueError(f"NORAD {config.norad_id} is not in the experiment") from exc

    boundaries = (origin,) + tuple(value for value in timestamps if origin < value < stop) + (stop,)
    rows = []
    max_rate_error = 0.0
    max_delay_error = 0.0
    wire_bits = (config.packet_size_bytes + 30) * 8
    for boundary in boundaries:
        frame_index = bisect_right(timestamps, boundary) - 1
        link = _state(document, frame_index, station_index, satellite_index)
        if link is None:
            rate, delay, available = 0, 0, 0
        else:
            source_rate = float(link["rate_bps"])
            source_delay_ns = float(link["delay_s"]) * 1_000_000_000
            if 0 < source_rate < 1 or source_rate > MAX_RATE_BPS:
                raise ValueError("selected link rate_bps must be zero or in 1..1000000000000")
            if source_delay_ns < 0 or source_delay_ns > 1_000_000_000:
                raise ValueError("selected link delay_s must be in 0..1")
            rate = floor(source_rate)
            delay = floor(source_delay_ns + 0.5)
            available = int(rate > 0)
            max_rate_error = max(max_rate_error, source_rate - rate)
            max_delay_error = max(max_delay_error, abs(source_delay_ns - delay))
            if rate and wire_bits * 1_000_000_000 < rate:
                raise ValueError("selected link serialization time is below 1 ns")
        elapsed = boundary - origin
        time_ns = (
            elapsed.days * 86_400 + elapsed.seconds
        ) * 1_000_000_000 + elapsed.microseconds * 1000
        rows.append(
            {"time_ns": time_ns, "rate_bps": rate, "delay_ns": delay, "available": available}
        )
    if len(rows) > 4096:
        raise ValueError("packet replay trace exceeds 4096 rows")

    duration_ns = duration_us * 1000
    offered_count = (duration_ns + interval_ns - 1) // interval_ns
    if offered_count * 2 > MAX_OFFERED_PACKETS:
        raise ValueError("packet replay exceeds the 200000 packet limit")
    return {
        "trace_rows": rows,
        "metadata": {
            "configuration": asdict(config),
            "selection": {
                "station_index": station_index,
                "station_name": config.station_name,
                "satellite_index": satellite_index,
                "norad_id": config.norad_id,
            },
            "window": {
                "start_utc": _utc_text(origin),
                "stop_utc": _utc_text(stop),
                "start_offset_s": start_us / 1_000_000,
                "duration_s": duration_us / 1_000_000,
                "duration_ns": duration_ns,
            },
            "quantization": {
                "max_rate_floor_error_bps": max_rate_error,
                "max_delay_round_error_ns": max_delay_error,
            },
            "traffic": {
                "interval_ns": interval_ns,
                "effective_offered_load_bps": config.packet_size_bytes
                * 8_000_000_000
                / interval_ns,
                "offered_packets_per_direction": offered_count,
                "total_offered_packets": offered_count * 2,
            },
        },
    }


def _source_metadata(bundle: Path, document: dict) -> dict:
    try:
        manifest_raw = _read(bundle / "manifest.json", 1_000_000)
        manifest = json.loads(manifest_raw)
        raw_files = {}
        for name in ARTIFACT_NAMES:
            raw_files[name] = _read(bundle / name, MAX_ARTIFACT_BYTES)
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError) as exc:
        raise ValueError("could not fingerprint the validated source bundle") from exc
    checksums = {name: sha256(raw).hexdigest() for name, raw in raw_files.items()}
    if not isinstance(manifest, dict) or manifest.get("files") != checksums:
        raise ValueError("source bundle changed while it was being prepared")
    try:
        exact_document = json.loads(raw_files["experiment.json"])
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("source experiment changed while it was being prepared") from exc
    if exact_document != document:
        raise ValueError("source experiment changed while it was being prepared")
    provenance = document["provenance"]
    return {
        "experiment": {
            "sha256": checksums["experiment.json"],
            "schema_version": document["schema_version"],
        },
        "scenario": {
            "sha256": provenance["scenario_sha256"],
            "hash_encoding": provenance.get("scenario_hash_encoding", "unspecified"),
        },
        "orbit_catalog": dict(provenance["orbit"]),
        "bundle": {
            "manifest_sha256": sha256(manifest_raw).hexdigest(),
            "files_sha256": checksums,
        },
    }


def run_packet_replay(
    bundle_dir: str | Path,
    config: ReplayConfig,
    backend: str | Path,
    output_dir: str | Path,
) -> dict:
    """Run the explicit external backend on a v1 bundle and atomically publish evidence."""
    bundle = Path(bundle_dir)
    document = load_experiment(bundle)
    prepared = prepare_packet_replay(document, config)
    source = _source_metadata(bundle, document)
    return _execute_packet_replay(prepared, source, backend, output_dir, V1_LIMITATIONS)
