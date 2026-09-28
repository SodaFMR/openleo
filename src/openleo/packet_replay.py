"""Validated bridge from portable OpenLEO experiments to the optional ns-3 replay."""

from __future__ import annotations

import csv
import json
import os
import re
import shutil
import subprocess
import tempfile
from bisect import bisect_right
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from importlib.metadata import version
from io import StringIO
from math import ceil, floor, isfinite
from pathlib import Path

from openleo.input import _utc
from openleo.workbench import (
    ARTIFACT_NAMES,
    MAX_ARTIFACT_BYTES,
    _read,
    _validate_document,
    load_experiment,
)

MAX_RATE_BPS = 1_000_000_000_000
MAX_OFFERED_PACKETS = 200_000
MAX_RESULT_BYTES = 64_000_000
BACKEND_VERSION = "openleo-ns3-replay/1 ns-3.48"
PACKET_FIELDS = (
    "direction",
    "sequence",
    "offered_time_ns",
    "udp_tx_time_ns",
    "phy_tx_time_ns",
    "rx_time_ns",
    "status",
    "payload_bytes",
)
STATUSES = frozenset(
    (
        "received",
        "received_after_window",
        "outage_suppressed",
        "queue_drop",
        "outage_queue_drop",
        "rx_outage_drop",
        "end_of_window_drop",
        "send_error",
        "unresolved",
    )
)


@dataclass(frozen=True, slots=True)
class ReplayConfig:
    station_name: str
    norad_id: int
    start_offset_s: float = 0
    duration_s: float = 60
    offered_load_bps: float = 100_000
    packet_size_bytes: int = 512
    queue_packets: int = 32
    seed: int = 1


def _number(value, name: str, minimum: float, maximum: float | None, *, positive=False) -> float:
    try:
        finite = isfinite(value)
    except (OverflowError, TypeError):
        finite = False
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not finite
        or value < minimum
        or (maximum is not None and value > maximum)
        or (positive and value == 0)
    ):
        qualifier = "> 0" if positive else f">= {minimum}"
        bound = f" and <= {maximum}" if maximum is not None else ""
        raise ValueError(f"{name} must be finite, non-boolean, {qualifier}{bound}")
    return float(value)


def _integer(value, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer in {minimum}..{maximum}")
    return value


def _microseconds(value: float, name: str) -> int:
    try:
        microseconds = round(value * 1_000_000)
    except OverflowError as exc:
        raise ValueError(f"{name} exceeds the supported machine range") from exc
    if value != microseconds / 1_000_000:
        raise ValueError(f"{name} must be representable as a whole number of microseconds")
    return microseconds


def _validate_config(config: ReplayConfig) -> tuple[int, int, int]:
    if not isinstance(config, ReplayConfig):
        raise ValueError("config must be a ReplayConfig")  # noqa: TRY004
    if not isinstance(config.station_name, str) or not config.station_name:
        raise ValueError("station_name must be a non-empty string")
    _integer(config.norad_id, "norad_id", 1, 2**53 - 1)
    start = _number(config.start_offset_s, "start_offset_s", 0, None)
    duration = _number(config.duration_s, "duration_s", 0, 3600, positive=True)
    offered = _number(config.offered_load_bps, "offered_load_bps", 1, 1_000_000_000)
    _integer(config.packet_size_bytes, "packet_size_bytes", 64, 1400)
    _integer(config.queue_packets, "queue_packets", 1, 10_000)
    _integer(config.seed, "seed", 1, 2**31 - 1)
    start_us = _microseconds(start, "start_offset_s")
    duration_us = _microseconds(duration, "duration_s")
    interval_ns = ceil(config.packet_size_bytes * 8_000_000_000 / offered)
    if interval_ns < 1:
        raise ValueError("offered_load_bps produces an interval below 1 ns")
    return start_us, duration_us, interval_ns


def _utc_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


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


def _read_result(path: Path) -> str:
    try:
        with path.open("rb") as file:
            raw = file.read(MAX_RESULT_BYTES + 1)
    except OSError as exc:
        raise ValueError(f"could not read packet results {path}") from exc
    if len(raw) > MAX_RESULT_BYTES:
        raise ValueError("packet results exceed 64000000 bytes")
    try:
        return raw.decode("ascii")
    except UnicodeDecodeError as exc:
        raise ValueError("packet results must be ASCII CSV") from exc


def _csv_integer(value: str, field: str, maximum: int, *, optional=False):
    if optional and value == "":
        return None
    if not re.fullmatch(r"[0-9]+", value):
        raise ValueError(f"packet result {field} must be a non-negative integer")
    result = int(value)
    if result > maximum:
        raise ValueError(f"packet result {field} exceeds its supported bound")
    return result


def _trace_state(rows: list[dict], time_ns: int) -> dict:
    return rows[bisect_right(rows, time_ns, key=lambda row: row["time_ns"]) - 1]


def _overlaps(start: int, stop: int, intervals: list[tuple[int, int]]) -> bool:
    return any(
        start < interval_stop and interval_start < stop
        for interval_start, interval_stop in intervals
    )


def _nearest_serialization_ns(wire_bits: int, rate_bps: int) -> int:
    whole, remainder = divmod(wire_bits * 1_000_000_000, rate_bps)
    return whole + (2 * remainder >= rate_bps)


def _validate_prepared(prepared: dict):
    try:
        metadata = prepared["metadata"]
        config = metadata["configuration"]
        traffic = metadata["traffic"]
        duration_ns = metadata["window"]["duration_ns"]
        interval_ns = traffic["interval_ns"]
        count = traffic["offered_packets_per_direction"]
        payload = config["packet_size_bytes"]
        rows = prepared["trace_rows"]
    except (KeyError, TypeError) as exc:
        raise ValueError(f"invalid prepared packet replay: {exc}") from exc
    for value, name in ((duration_ns, "duration_ns"), (interval_ns, "interval_ns")):
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"invalid prepared packet replay {name}")
    _integer(payload, "packet_size_bytes", 64, 1400)
    expected_count = (duration_ns + interval_ns - 1) // interval_ns
    if (
        isinstance(count, bool)
        or not isinstance(count, int)
        or count != expected_count
        or count * 2 > MAX_OFFERED_PACKETS
        or traffic.get("total_offered_packets") != count * 2
    ):
        raise ValueError("invalid prepared packet replay traffic accounting")
    if not isinstance(rows, list) or not 2 <= len(rows) <= 4096:
        raise ValueError("invalid prepared packet replay trace row count")
    previous = -1
    for row in rows:
        if not isinstance(row, dict) or set(row) != {
            "time_ns",
            "rate_bps",
            "delay_ns",
            "available",
        }:
            raise ValueError("invalid prepared packet replay trace row")
        time_ns = row["time_ns"]
        rate = row["rate_bps"]
        delay = row["delay_ns"]
        available = row["available"]
        if (
            any(
                isinstance(value, bool) or not isinstance(value, int)
                for value in (time_ns, rate, delay)
            )
            or time_ns <= previous
            or rate < 0
            or rate > MAX_RATE_BPS
            or delay < 0
            or delay > 1_000_000_000
            or isinstance(available, bool)
            or available not in (0, 1)
            or (available == 0 and rate != 0)
            or (available == 1 and rate < 1)
            or (rate and (payload + 30) * 8_000_000_000 < rate)
        ):
            raise ValueError("invalid prepared packet replay trace row values")
        previous = time_ns
    if rows[0]["time_ns"] != 0 or rows[-1]["time_ns"] != duration_ns:
        raise ValueError("prepared packet replay trace must span the full window")
    return rows, duration_ns, interval_ns, count, payload


def parse_packet_results(path: str | Path, prepared: dict) -> tuple[list[dict], list[dict]]:
    """Parse and independently account for one bounded backend packet CSV."""
    trace, duration_ns, interval_ns, count, payload = _validate_prepared(prepared)
    reader = csv.reader(StringIO(_read_result(Path(path)), newline=""))
    try:
        header = next(reader)
    except StopIteration as exc:
        raise ValueError("packet result CSV is empty") from exc
    if header != list(PACKET_FIELDS):
        raise ValueError("packet result CSV header does not match the replay protocol")
    wire_bits = (payload + 30) * 8
    positive_rates = [row["rate_bps"] for row in trace[:-1] if row["rate_bps"]]
    maximum_time = duration_ns + 1_000_000_000 + max(row["delay_ns"] for row in trace)
    if positive_rates:
        minimum_rate = min(positive_rates)
        maximum_time += (wire_bits * 1_000_000_000 + minimum_rate - 1) // minimum_rate
    outage_intervals = [
        (row["time_ns"], trace[index + 1]["time_ns"])
        for index, row in enumerate(trace[:-1])
        if not row["available"]
    ]
    records = []
    for index, raw in enumerate(reader):
        if index >= count * 2:
            raise ValueError(f"packet result CSV must contain exactly {count * 2} records")
        if len(raw) != len(PACKET_FIELDS):
            raise ValueError("packet result CSV record has the wrong number of fields")
        record = {
            "direction": _csv_integer(raw[0], "direction", 1),
            "sequence": _csv_integer(raw[1], "sequence", count - 1),
            "offered_time_ns": _csv_integer(raw[2], "offered_time_ns", duration_ns - 1),
            "udp_tx_time_ns": _csv_integer(raw[3], "udp_tx_time_ns", maximum_time, optional=True),
            "phy_tx_time_ns": _csv_integer(raw[4], "phy_tx_time_ns", maximum_time, optional=True),
            "rx_time_ns": _csv_integer(raw[5], "rx_time_ns", maximum_time, optional=True),
            "status": raw[6],
            "payload_bytes": _csv_integer(raw[7], "payload_bytes", 1400),
        }
        if record["status"] not in STATUSES:
            raise ValueError(f"unknown packet result status {record['status']!r}")
        expected = (index // 2 * interval_ns, index % 2, index // 2)
        actual = (record["offered_time_ns"], record["direction"], record["sequence"])
        if actual != expected:
            raise ValueError("packet results must be complete, unique, and ordered by offer time")
        records.append(record)
    if len(records) != count * 2:
        raise ValueError(f"packet result CSV must contain exactly {count * 2} records")

    full_times = frozenset(("received", "received_after_window", "rx_outage_drop"))
    for record in records:
        status = record["status"]
        udp = record["udp_tx_time_ns"]
        phy = record["phy_tx_time_ns"]
        rx = record["rx_time_ns"]
        if record["payload_bytes"] != payload:
            raise ValueError("packet result payload_bytes does not match the configuration")
        if status == "outage_suppressed":
            if any(value is not None for value in (udp, phy, rx)):
                raise ValueError("outage_suppressed packet cannot have transmission times")
        elif udp is None:
            raise ValueError(f"{status} packet requires udp_tx_time_ns")
        if status in full_times and (phy is None or rx is None):
            raise ValueError(f"{status} packet requires PHY and receive times")
        if (
            status not in full_times
            and status != "unresolved"
            and (phy is not None or rx is not None)
        ):
            raise ValueError(f"{status} packet cannot have PHY or receive times")
        if status == "unresolved" and rx is not None:
            raise ValueError("unresolved packet cannot have rx_time_ns")
        if status == "outage_queue_drop" and not any(
            udp < outage_start < duration_ns for outage_start, _ in outage_intervals
        ):
            raise ValueError(
                "outage_queue_drop packet requires an outage onset after UDP admission"
            )
        times = [record["offered_time_ns"], udp, phy, rx]
        present = [value for value in times if value is not None]
        if present != sorted(present):
            raise ValueError("packet result times must be causal")
        if udp is not None and udp >= duration_ns:
            raise ValueError("UDP transmission cannot start at or after the window end")
        if phy is not None and phy >= duration_ns:
            raise ValueError("PHY transmission cannot start at or after the window end")

        offered_state = _trace_state(trace, record["offered_time_ns"])
        if (status == "outage_suppressed") != (not offered_state["available"]):
            raise ValueError("packet source status does not match the prepared trace")
        overlaps_outage = False
        if phy is not None:
            phy_state = _trace_state(trace, phy)
            if not phy_state["available"]:
                raise ValueError("packet PHY transmission starts during an outage")
            expected_serialization = _nearest_serialization_ns(wire_bits, phy_state["rate_bps"])
            serialization_ns = expected_serialization
            if rx is not None:
                serialization_ns = rx - phy - phy_state["delay_ns"]
                if serialization_ns < 1 or abs(serialization_ns - expected_serialization) > 1:
                    raise ValueError(
                        "packet receive time is inconsistent with prepared rate and delay"
                    )
            overlaps_outage = _overlaps(phy, phy + serialization_ns, outage_intervals)
            if rx is not None:
                overlaps_outage |= _overlaps(rx - serialization_ns, rx, outage_intervals)
        if status == "rx_outage_drop" and not overlaps_outage:
            raise ValueError("rx_outage_drop packet does not overlap a prepared outage")
        if status in ("received", "received_after_window") and overlaps_outage:
            raise ValueError("successfully received packet overlaps a prepared outage")
        if status == "received" and rx >= duration_ns:
            raise ValueError("received packet completed after the measurement window")
        if status == "received_after_window" and rx < duration_ns:
            raise ValueError("received_after_window packet completed inside the measurement window")

    summaries = []
    for direction in (0, 1):
        selected = [record for record in records if record["direction"] == direction]
        received = [
            record
            for record in selected
            if record["status"] in ("received", "received_after_window")
        ]
        within = [record for record in selected if record["status"] == "received"]
        delays = [(record["rx_time_ns"] - record["udp_tx_time_ns"]) / 1e9 for record in within]
        summaries.append(
            {
                "direction": direction,
                "offered_packets": len(selected),
                "admitted_packets": sum(
                    record["status"] not in ("outage_suppressed", "send_error")
                    for record in selected
                ),
                "received_packets": len(received),
                "received_in_window_packets": len(within),
                "received_after_window_packets": sum(
                    record["status"] == "received_after_window" for record in selected
                ),
                "outage_suppressed_packets": sum(
                    record["status"] == "outage_suppressed" for record in selected
                ),
                "queue_drop_packets": sum(record["status"] == "queue_drop" for record in selected),
                "outage_queue_drop_packets": sum(
                    record["status"] == "outage_queue_drop" for record in selected
                ),
                "rx_outage_drop_packets": sum(
                    record["status"] == "rx_outage_drop" for record in selected
                ),
                "end_of_window_drop_packets": sum(
                    record["status"] == "end_of_window_drop" for record in selected
                ),
                "send_error_packets": sum(record["status"] == "send_error" for record in selected),
                "unresolved_packets": sum(record["status"] == "unresolved" for record in selected),
                "goodput_bps": len(within) * payload * 8_000_000_000 / duration_ns,
                "mean_delay_s": sum(delays) / len(delays) if delays else None,
                "max_delay_s": max(delays) if delays else None,
            }
        )
    return records, summaries


def _json_text(value) -> str:
    return (
        json.dumps(
            value, allow_nan=False, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        + "\n"
    )


def _trace_csv(rows: list[dict]) -> str:
    file = StringIO(newline="")
    writer = csv.DictWriter(
        file,
        fieldnames=("time_ns", "rate_bps", "delay_ns", "available"),
        lineterminator="\n",
    )
    writer.writeheader()
    writer.writerows(rows)
    return file.getvalue()


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


def _file_sha256(path: Path) -> str:
    digest = sha256()
    try:
        with path.open("rb") as file:
            for block in iter(lambda: file.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise ValueError(f"backend executable {path} could not be read") from exc
    return digest.hexdigest()


def _check_output_target(path: Path) -> None:
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        raise ValueError("output path must be a directory, not a file or symlink")
    if path.exists():
        try:
            nonempty = next(path.iterdir(), None) is not None
        except OSError as exc:
            raise ValueError("could not inspect output directory") from exc
        if nonempty:
            raise ValueError("refusing to overwrite a non-empty output directory")


def _run_backend(command: list[str]) -> None:
    try:
        result = subprocess.run(command, capture_output=True, timeout=120, check=False)
    except subprocess.TimeoutExpired as exc:
        raise ValueError("packet replay backend timed out after 120 seconds") from exc
    except OSError as exc:
        raise ValueError(f"could not execute packet replay backend: {exc}") from exc
    if result.returncode:
        detail = result.stderr.decode("utf-8", "replace")[-2000:].strip()
        suffix = f": {detail}" if detail else ""
        raise ValueError(f"packet replay backend exited with status {result.returncode}{suffix}")


def run_packet_replay(
    bundle_dir: str | Path,
    config: ReplayConfig,
    backend: str | Path,
    output_dir: str | Path,
) -> dict:
    """Run the explicit external backend and atomically publish validated evidence."""
    bundle = Path(bundle_dir)
    document = load_experiment(bundle)
    prepared = prepare_packet_replay(document, config)
    source = _source_metadata(bundle, document)
    target = Path(output_dir)
    _check_output_target(target)
    executable = Path(backend)
    binary_sha256 = _file_sha256(executable)
    try:
        handshake = subprocess.run(
            [str(executable.resolve()), "--PrintVersion"],
            capture_output=True,
            timeout=10,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError(
            "packet replay backend version handshake timed out after 10 seconds"
        ) from exc
    except OSError as exc:
        raise ValueError(
            f"could not execute packet replay backend version handshake: {exc}"
        ) from exc
    if (
        handshake.returncode != 0
        or handshake.stdout != (BACKEND_VERSION + "\n").encode("ascii")
        or handshake.stderr
    ):
        raise ValueError(
            f"packet replay backend version handshake must yield exactly {BACKEND_VERSION!r}"
        )

    parent = target.parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=f".{target.name}.", dir=parent))
    except OSError as exc:
        raise ValueError("could not create temporary packet replay output") from exc
    try:
        trace_path = temporary / "link-trace.csv"
        packet_path = temporary / "packets.csv"
        trace_path.write_text(_trace_csv(prepared["trace_rows"]), encoding="ascii", newline="")
        traffic = prepared["metadata"]["traffic"]
        configuration = prepared["metadata"]["configuration"]
        command = [
            str(executable.resolve()),
            f"--trace={trace_path.resolve()}",
            f"--output={packet_path.resolve()}",
            f"--intervalNs={traffic['interval_ns']}",
            f"--packetSize={configuration['packet_size_bytes']}",
            f"--queuePackets={configuration['queue_packets']}",
            f"--seed={configuration['seed']}",
        ]
        _run_backend(command)
        if _file_sha256(executable) != binary_sha256:
            raise ValueError("packet replay backend executable changed during execution")
        _, directions = parse_packet_results(packet_path, prepared)
        summary = {
            "schema_version": "1",
            "kind": "openleo.packet-replay",
            "software": {"openleo-link": version("openleo-link")},
            "configuration": configuration,
            "source": source,
            "backend": {
                "executable": executable.name,
                "binary_sha256": binary_sha256,
                "protocol_version": "openleo-ns3-replay/1",
                "ns3_version": "ns-3.48",
            },
            "window": prepared["metadata"]["window"],
            "quantization": prepared["metadata"]["quantization"],
            "traffic": traffic,
            "directions": directions,
            "limitations": [
                (
                    "The two directions are independent full-duplex virtual links with identical "
                    "rates and delays, not a real uplink hardware model."
                ),
                (
                    "The 12-byte sequence/timestamp measurement header is included in UDP "
                    "payload bytes; PPP (2), IPv4 (20), and UDP (8) bytes serialize in addition."
                ),
                (
                    "Packets received after the measurement window are reported but excluded "
                    "from goodput and latency metrics."
                ),
                (
                    "Backend version and binary/input hashes support traceability, not "
                    "authorship attestation."
                ),
            ],
        }
        summary_path = temporary / "packet-summary.json"
        summary_path.write_text(_json_text(summary), encoding="utf-8", newline="\n")
        fixed = ("link-trace.csv", "packets.csv", "packet-summary.json")
        manifest = {
            "schema_version": "1",
            "kind": "openleo.packet-replay.bundle",
            "files": {name: sha256((temporary / name).read_bytes()).hexdigest() for name in fixed},
        }
        (temporary / "manifest.json").write_text(
            _json_text(manifest), encoding="utf-8", newline="\n"
        )
        _check_output_target(target)
        if target.exists():
            target.rmdir()
        os.replace(temporary, target)
        return summary
    except OSError as exc:
        raise ValueError(f"could not produce packet replay output: {exc}") from exc
    finally:
        if temporary.exists():
            shutil.rmtree(temporary, ignore_errors=True)
