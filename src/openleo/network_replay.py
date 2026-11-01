"""v1 import path of the multi-hop replay, which moved to ``openleo.replay.network_replay``.

The v1 experiment and benchmark modules still replay v1 workbench bundles: this module keeps
v1's ``NetworkReplayConfig`` (without ``pair_index``), its document-based
``prepare_network_replay`` and bundle-based ``run_network_replay``, and re-exports the protocol,
the parser and the shared execution from the new home, until the v1 modules are removed.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import asdict, dataclass
from datetime import timedelta
from itertools import pairwise
from math import ceil, floor
from pathlib import Path

from openleo._validation import _utc
from openleo.network import ROUTE_MODELS
from openleo.packet_replay import _source_metadata
from openleo.replay.network_packet_io import (
    EDGE_FIELDS,
    HOP_FIELDS,
    MAX_HOPS,
    PACKET_FIELDS,
    ROUTE_FIELDS,
)
from openleo.replay.network_replay import (
    V1_LIMITATIONS,
    _execute_network_replay,
    _offers,
    parse_network_results,
)
from openleo.replay.packet_replay import (
    MAX_RATE_BPS,
    _integer,
    _microseconds,
    _number,
    _utc_text,
)
from openleo.workbench import _validate_document, load_experiment

__all__ = [
    "EDGE_FIELDS",
    "HOP_FIELDS",
    "PACKET_FIELDS",
    "ROUTE_FIELDS",
    "NetworkReplayConfig",
    "_offers",
    "_validate_config",
    "parse_network_results",
    "prepare_network_replay",
    "run_network_replay",
]


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


def run_network_replay(bundle_dir, config, backend, output_dir) -> dict:
    """Execute a trusted binary on a v1 bundle and atomically publish only valid output."""
    bundle = Path(bundle_dir)
    document = load_experiment(bundle)
    prepared = prepare_network_replay(document, config)
    source = _source_metadata(bundle, document)
    return _execute_network_replay(prepared, source, backend, output_dir, V1_LIMITATIONS)
