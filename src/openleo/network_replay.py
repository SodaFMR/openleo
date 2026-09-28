"""Bounded, independently validated bridge to the optional multi-hop ns-3 backend."""

from __future__ import annotations

import csv
import os
import shutil
import subprocess
import tempfile
from bisect import bisect_right
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import timedelta
from importlib.metadata import version
from io import StringIO
from itertools import pairwise
from math import ceil, floor
from pathlib import Path

from openleo.input import _utc
from openleo.network import ROUTE_MODELS
from openleo.network_packet_io import (
    EDGE_FIELDS,
    HOP_FIELDS,
    MAX_HOPS,
    PACKET_FIELDS,
    ROUTE_FIELDS,
    _parse_network_results,
)
from openleo.packet_replay import (
    MAX_RATE_BPS,
    _check_output_target,
    _csv_integer,
    _file_sha256,
    _integer,
    _json_text,
    _microseconds,
    _number,
    _read_result,
    _run_backend,
    _source_metadata,
    _utc_text,
)
from openleo.workbench import _validate_document, load_experiment

__all__ = [
    "EDGE_FIELDS",
    "HOP_FIELDS",
    "PACKET_FIELDS",
    "ROUTE_FIELDS",
    "NetworkReplayConfig",
    "parse_network_results",
    "prepare_network_replay",
    "run_network_replay",
]

BACKEND_VERSION = "openleo-ns3-network/1 ns-3.48"


@dataclass(frozen=True, slots=True)
class NetworkReplayConfig:
    routing_model: str = "minimum_delay"
    start_offset_s: float = 0
    duration_s: float = 60
    offered_load_bps: float = 100_000
    packet_size_bytes: int = 512
    queue_packets: int = 32
    flows_per_direction: int = 1
    acquisition_delay_s: float = 0
    seed: int = 1


def _validate_config(config):
    if not isinstance(config, NetworkReplayConfig):
        raise ValueError("config must be a NetworkReplayConfig")  # noqa: TRY004
    if config.routing_model not in ROUTE_MODELS:
        raise ValueError("routing_model must name a declared routing model")
    start = _number(config.start_offset_s, "start_offset_s", 0, None)
    duration = _number(config.duration_s, "duration_s", 0, 3600, positive=True)
    acquisition = _number(config.acquisition_delay_s, "acquisition_delay_s", 0, 60)
    offered = _number(config.offered_load_bps, "offered_load_bps", 1, 1_000_000_000)
    _integer(config.packet_size_bytes, "packet_size_bytes", 64, 1400)
    _integer(config.queue_packets, "queue_packets", 1, 10_000)
    _integer(config.flows_per_direction, "flows_per_direction", 1, 4)
    _integer(config.seed, "seed", 1, 2**31 - 1)
    return (
        _microseconds(start, "start_offset_s"),
        _microseconds(duration, "duration_s"),
        _microseconds(acquisition, "acquisition_delay_s"),
        ceil(config.packet_size_bytes * 8_000_000_000 / offered),
    )


def _offers(duration, interval, flows):
    return tuple(
        (
            flow * interval // flows,
            max(0, (duration - flow * interval // flows + interval - 1) // interval),
        )
        for flow in range(flows)
    )


def _edge_values(document, frame, satellite_count, fixed):
    settings = document["scenario"]["network"]
    ground = {
        tuple(sorted((link["satellite_index"], satellite_count + link["station_index"]))): (
            settings["fixed_capacity_bps"] if fixed else link["rate_bps"],
            link["delay_s"],
        )
        for link in document["links"][frame]
    }
    return {
        **ground,
        **{
            (a, b): (settings["isl_capacity_bps"], delay)
            for a, b, delay in document["network"]["frames"][frame]["isl_edges"]
        },
    }


def prepare_network_replay(document: dict, config: NetworkReplayConfig) -> dict:
    """Left-hold selected routes and geometry, with explicit endpoint acquisition."""
    _validate_document(document)
    start_us, duration_us, acquisition_us, interval = _validate_config(config)
    timestamps = tuple(_utc(value, "timestamps_utc") for value in document["timestamps_utc"])
    span = timestamps[-1] - timestamps[0]
    span_us = (span.days * 86_400 + span.seconds) * 1_000_000 + span.microseconds
    if start_us > span_us or duration_us > span_us - start_us:
        raise ValueError("requested replay window must lie fully inside the experiment window")
    origin = timestamps[0] + timedelta(microseconds=start_us)
    stop = origin + timedelta(microseconds=duration_us)
    count = len(document["satellites"])
    network = document["network"]
    source, target = (
        count + network[field] for field in ("source_station_index", "target_station_index")
    )
    fixed = config.routing_model == "fixed_capacity"
    boundaries = (origin,) + tuple(t for t in timestamps if origin < t < stop) + (stop,)
    snapshots, union = [], set()
    endpoint_pair, ready_time = None, None
    for boundary, next_boundary in pairwise(boundaries):
        time_ns = round((boundary - origin).total_seconds() * 1_000_000) * 1000
        next_ns = round((next_boundary - origin).total_seconds() * 1_000_000) * 1000
        frame = bisect_right(timestamps, boundary) - 1
        raw = network["frames"][frame][config.routing_model]
        path = tuple(raw["nodes"]) if raw is not None else ()
        values = _edge_values(document, frame, count, fixed)
        if path:
            if (
                path[0] != source
                or path[-1] != target
                or len(set(path)) != len(path)
                or any(node >= count for node in path[1:-1])
            ):
                raise ValueError("selected route must be simple and connect only its endpoints")
            pairs = tuple(tuple(sorted(pair)) for pair in pairwise(path))
            if any(pair not in values or values[pair][0] <= 0 for pair in pairs):
                raise ValueError("selected route must follow active edges")
            union.update(pairs)
            current_pair = (path[1], path[-2])
            if current_pair != endpoint_pair:
                ready_time = time_ns + acquisition_us * 1000
            endpoint_pair = current_pair
            acquiring = time_ns < ready_time
            snapshots.append((time_ns, "acquiring" if acquiring else "ready", path, values))
            if acquiring and ready_time < next_ns:
                snapshots.append((ready_time, "ready", path, values))
        else:
            endpoint_pair, ready_time = None, None
            snapshots.append((time_ns, "disconnected", (), values))
    snapshots.append((duration_us * 1000, "stop", (), {}))
    if len(union) > 1024 or len(snapshots) > 4096 or len(union) * len(snapshots) > 200_000:
        raise ValueError("network replay exceeds edge/frame/edge-state budgets")
    pairs = tuple(sorted(union))
    edges, routes = [], []
    rate_error, delay_error = 0.0, 0.0
    for time_ns, state, path, values in snapshots:
        routes.append(
            {
                "time_ns": time_ns,
                "state": state,
                "path": ";".join(map(str, path)) if state == "ready" else "",
            }
        )
        selected_ground = (
            {tuple(sorted((source, path[1]))), tuple(sorted((target, path[-2])))} if path else set()
        )
        for edge_id, pair in enumerate(pairs):
            raw_rate, raw_delay = values.get(pair, (0, 0))
            _number(raw_rate, "edge rate_bps", 0, MAX_RATE_BPS)
            _number(raw_delay, "edge delay_s", 0, 1)
            if 0 < raw_rate < 1:
                raise ValueError("edge rate_bps must be zero or at least 1")
            rate, delay = floor(raw_rate), floor(raw_delay * 1e9 + 0.5)
            rate_error = max(rate_error, raw_rate - rate)
            delay_error = max(delay_error, abs(raw_delay * 1e9 - delay))
            available = int(rate > 0 and state != "stop")
            if pair[1] >= count:
                available = int(available and state == "ready" and pair in selected_ground)
            edges.append(
                dict(
                    zip(
                        EDGE_FIELDS,
                        (time_ns, edge_id, *pair, rate if available else 0, delay, available),
                        strict=True,
                    )
                )
            )
    duration = duration_us * 1000
    offers = _offers(duration, interval, config.flows_per_direction)
    total = sum(n for _, n in offers) * 2
    max_hops = max((len(path) - 1 for _, _, path, _ in snapshots), default=0)
    if total > 200_000 or total * max(max_hops, 1) > MAX_HOPS:
        raise ValueError("network replay exceeds offered packet/hop budgets")
    return {
        "edges": edges,
        "routes": routes,
        "metadata": {
            "configuration": asdict(config),
            "selection": {
                "source_node": source,
                "target_node": target,
                "node_count": count + len(document["stations"]),
                "source_station_index": network["source_station_index"],
                "target_station_index": network["target_station_index"],
            },
            "window": {
                "start_utc": _utc_text(origin),
                "stop_utc": _utc_text(stop),
                "start_offset_s": start_us / 1e6,
                "duration_s": duration_us / 1e6,
                "duration_ns": duration,
            },
            "quantization": {
                "max_rate_floor_error_bps": rate_error,
                "max_delay_round_error_ns": delay_error,
                "interval_rounding": "ceil to whole nanoseconds",
            },
            "traffic": {
                "interval_ns": interval,
                "flows_per_direction": config.flows_per_direction,
                "effective_offered_load_bps_per_flow": config.packet_size_bytes * 8e9 / interval,
                "offered_packets_per_flow": [n for _, n in offers],
                "offered_packets_per_direction": total // 2,
                "total_offered_packets": total,
            },
        },
    }


def _validate_prepared(prepared):
    try:
        metadata = prepared["metadata"]
        config = NetworkReplayConfig(**metadata["configuration"])
        _, duration_us, _, interval = _validate_config(config)
        duration = duration_us * 1000
        if metadata["window"]["duration_ns"] != duration:
            raise ValueError("prepared window does not match configuration")
        offers = _offers(duration, interval, config.flows_per_direction)
        traffic = metadata["traffic"]
        for field, maximum in (
            ("interval_ns", 2**63 - 1),
            ("total_offered_packets", 200_000),
            ("offered_packets_per_direction", 100_000),
            ("flows_per_direction", 4),
        ):
            _integer(traffic[field], field, 0, maximum)
        counts = traffic["offered_packets_per_flow"]
        if not isinstance(counts, list) or len(counts) != config.flows_per_direction:
            raise ValueError("invalid prepared per-flow offer counts")
        for count in counts:
            _integer(count, "offered_packets_per_flow", 0, 100_000)
        total = sum(n for _, n in offers) * 2
        if (
            traffic["interval_ns"] != interval
            or traffic["total_offered_packets"] != total
            or traffic["offered_packets_per_flow"] != [n for _, n in offers]
            or traffic["offered_packets_per_direction"] != total // 2
            or traffic["flows_per_direction"] != config.flows_per_direction
            or total > 200_000
        ):
            raise ValueError("invalid prepared traffic accounting")
        selection = metadata["selection"]
        nodes = _integer(selection["node_count"], "node_count", 3, 144)
        source = _integer(selection["source_node"], "source_node", 0, nodes - 1)
        target = _integer(selection["target_node"], "target_node", 0, nodes - 1)
        if source == target:
            raise ValueError("prepared endpoints must differ")
        satellite_count = source - _integer(
            selection["source_station_index"], "source station", 0, 15
        )
        if not 1 <= satellite_count <= 128 or target != satellite_count + _integer(
            selection["target_station_index"], "target station", 0, 15
        ):
            raise ValueError("prepared ground station indices must match node IDs")
        routes, edges = prepared["routes"], prepared["edges"]
        if not isinstance(routes, list) or not 2 <= len(routes) <= 4096:
            raise ValueError("invalid prepared route count")
        previous = -1
        for index, row in enumerate(routes):
            if set(row) != set(ROUTE_FIELDS):
                raise ValueError("invalid prepared route fields")
            _integer(row["time_ns"], "route time_ns", 0, duration)
            if row["time_ns"] <= previous:
                raise ValueError("prepared route times must increase")
            previous = row["time_ns"]
            if row["state"] not in ("ready", "disconnected", "acquiring", "stop"):
                raise ValueError("invalid prepared route state")
            if (row["state"] == "stop") != (index == len(routes) - 1):
                raise ValueError("prepared stop must be terminal")
            if not isinstance(row["path"], str):
                raise ValueError("invalid prepared route path")  # noqa: TRY004
            if row["state"] != "ready" and row["path"]:
                raise ValueError("inactive prepared route must have empty path")
        if routes[0]["time_ns"] != 0 or routes[-1]["time_ns"] != duration:
            raise ValueError("prepared routes must span the measurement window")
        if not isinstance(edges, list) or len(edges) > 200_000:
            raise ValueError("invalid prepared edge count")
        by_edge, pairs = defaultdict(list), {}
        previous_key = (-1, -1)
        for row in edges:
            if set(row) != set(EDGE_FIELDS):
                raise ValueError("invalid prepared edge fields")
            for field, maximum in (
                ("time_ns", duration),
                ("edge_id", 1023),
                ("node_a", nodes - 1),
                ("node_b", nodes - 1),
                ("rate_bps", MAX_RATE_BPS),
                ("delay_ns", 1_000_000_000),
                ("available", 1),
            ):
                _integer(row[field], field, 0, maximum)
            key = (row["time_ns"], row["edge_id"])
            pair = (row["node_a"], row["node_b"])
            if pair[1] >= satellite_count and (
                pair[0] >= satellite_count or pair[1] not in (source, target)
            ):
                raise ValueError(
                    "prepared ground edges may connect only satellites to its endpoints"
                )
            if (
                key <= previous_key
                or pair[0] >= pair[1]
                or bool(row["rate_bps"]) != bool(row["available"])
            ):
                raise ValueError("invalid prepared edge ordering/availability")
            if pairs.get(row["edge_id"], pair) != pair:
                raise ValueError("prepared edge endpoints must be stable")
            pairs[row["edge_id"]] = pair
            by_edge[row["edge_id"]].append(row)
            previous_key = key
        if sorted(pairs) != list(range(len(pairs))) or len(set(pairs.values())) != len(pairs):
            raise ValueError("prepared edge IDs and pairs must be unique and contiguous")
        times = [row["time_ns"] for row in routes]
        if any([row["time_ns"] for row in trace] != times for trace in by_edge.values()):
            raise ValueError("each prepared edge must appear at every route time")
        lookup = {pair: edge_id for edge_id, pair in pairs.items()}
        max_hops = 1
        for index, route in enumerate(routes):
            if route["state"] == "stop" and any(
                trace[index]["available"] for trace in by_edge.values()
            ):
                raise ValueError("prepared stop cannot have active edges")
            active_ground = {
                pair
                for edge_id, pair in pairs.items()
                if pair[1] >= satellite_count and by_edge[edge_id][index]["available"]
            }
            if route["state"] != "ready" and active_ground:
                raise ValueError("inactive prepared route cannot have active ground edges")
            if route["state"] == "ready":
                path = tuple(
                    _csv_integer(token, "path node", nodes - 1)
                    for token in route["path"].split(";")
                )
                if (
                    len(path) < 3
                    or path[0] != source
                    or path[-1] != target
                    or len(set(path)) != len(path)
                ):
                    raise ValueError("prepared ready route must be simple with correct endpoints")
                if any(node >= satellite_count for node in path[1:-1]):
                    raise ValueError("prepared route cannot use intermediate ground nodes")
                selected_ground = {
                    tuple(sorted((source, path[1]))),
                    tuple(sorted((target, path[-2]))),
                }
                if active_ground != selected_ground:
                    raise ValueError(
                        "prepared route requires exactly its two selected ground edges"
                    )
                max_hops = max(max_hops, len(path) - 1)
                for pair in pairwise(path):
                    edge_id = lookup.get(tuple(sorted(pair)))
                    if edge_id is None or not by_edge[edge_id][index]["available"]:
                        raise ValueError("prepared ready route must use active edges")
        if total * max_hops > MAX_HOPS:
            raise ValueError("prepared replay exceeds packet/hop budget")
        return config, duration, interval, offers, routes, dict(by_edge), source, target
    except (KeyError, TypeError, OverflowError) as exc:
        raise ValueError(f"invalid prepared network replay: {exc}") from exc


def parse_network_results(directory: str | Path, prepared: dict) -> dict:
    """Revalidate native observations and derive metrics without trusting summaries."""
    return _parse_network_results(directory, _validate_prepared(prepared))


def _csv_text(rows, fields):
    output = StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue()


def run_network_replay(bundle_dir, config, backend, output_dir) -> dict:
    """Execute a trusted binary and atomically publish only independently valid output."""
    bundle, target, executable = Path(bundle_dir), Path(output_dir), Path(backend)
    document = load_experiment(bundle)
    prepared = prepare_network_replay(document, config)
    source = _source_metadata(bundle, document)
    _check_output_target(target)
    binary_hash = _file_sha256(executable)
    try:
        handshake = subprocess.run(
            [str(executable.resolve()), "--PrintVersion"],
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError(f"network backend version handshake failed: {exc}") from exc
    if (
        handshake.returncode
        or handshake.stderr
        or handshake.stdout != (BACKEND_VERSION + "\n").encode("ascii")
    ):
        raise ValueError(
            f"network backend version handshake must yield exactly {BACKEND_VERSION!r}"
        )
    temporary = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=f".{target.name}.", dir=target.parent))
        for name, fields in (("edges", EDGE_FIELDS), ("routes", ROUTE_FIELDS)):
            (temporary / f"{name}.csv").write_text(
                _csv_text(prepared[name], fields), encoding="ascii", newline=""
            )
        metadata = prepared["metadata"]
        selection, traffic = metadata["selection"], metadata["traffic"]
        command = [
            str(executable.resolve()),
            f"--edges={(temporary / 'edges.csv').resolve()}",
            f"--routes={(temporary / 'routes.csv').resolve()}",
            f"--output={temporary.resolve()}",
            f"--source={selection['source_node']}",
            f"--target={selection['target_node']}",
            f"--nodeCount={selection['node_count']}",
            f"--intervalNs={traffic['interval_ns']}",
            f"--packetSize={config.packet_size_bytes}",
            f"--queuePackets={config.queue_packets}",
            f"--flows={config.flows_per_direction}",
            f"--seed={config.seed}",
        ]
        _run_backend(command)
        if _file_sha256(executable) != binary_hash:
            raise ValueError("network backend executable changed during execution")
        for name, fields in (("edges", EDGE_FIELDS), ("routes", ROUTE_FIELDS)):
            if _read_result(temporary / f"{name}.csv") != _csv_text(prepared[name], fields):
                raise ValueError("network backend changed its prepared input traces")
        results = parse_network_results(temporary, prepared)
        summary = {
            "schema_version": "1",
            "kind": "openleo.network-replay",
            "software": {"openleo-link": version("openleo-link")},
            "source": source,
            "backend": {
                "executable": executable.name,
                "binary_sha256": binary_hash,
                "protocol_version": "openleo-ns3-network/1",
                "ns3_version": "ns-3.48",
            },
            **metadata,
            **results,
            "limitations": [
                "Synthetic reciprocal full-duplex P2P links; not a radio or uplink hardware model.",
                "UDP payload includes the 12-byte measurement header; 30 wire header bytes serialize per hop.",
                "Goodput counts payload delivered inside the half-open window; one-way delays use those deliveries only.",
                "After-window deliveries, end-of-window drops and unresolved packets are separately censored.",
                "Routes and geometric link states are left-held; endpoint acquisition gates both selected ground links.",
                "Hashes and version handshake provide traceability, not authorship attestation.",
            ],
        }
        (temporary / "network-summary.json").write_text(_json_text(summary), encoding="utf-8")
        names = ("edges.csv", "routes.csv", "packets.csv", "hops.csv", "network-summary.json")
        unexpected = {path.name for path in temporary.iterdir()} - set(names)
        if unexpected:
            raise ValueError("network backend produced unexpected artifacts")
        manifest = {
            "schema_version": "1",
            "kind": "openleo.network-replay.bundle",
            "files": {name: _file_sha256(temporary / name) for name in names},
        }
        (temporary / "manifest.json").write_text(_json_text(manifest), encoding="utf-8")
        _check_output_target(target)
        if target.exists():
            target.rmdir()
        os.replace(temporary, target)
        return summary
    except OSError as exc:
        raise ValueError(f"could not produce network replay output: {exc}") from exc
    finally:
        if temporary is not None and temporary.exists():
            shutil.rmtree(temporary, ignore_errors=True)
