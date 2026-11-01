"""Bounded, independently validated bridge from a saved run to the multi-hop ns-3 backend.

``prepare_network_replay`` reads one configured route pair (``pair_index``) and routing model of
a ``ReplaySource``. Snapshots are the window start and every route breakpoint strictly inside
the window (the grid and, with events on, the pass and MODCOD switch times of the route
endpoints' links); each snapshot holds the route, the link states and the inter-satellite
geometry of the latest route breakpoint at or before it, which is where the engine evaluated
that route. A change of the route's endpoint satellites starts an acquisition of
``acquisition_delay_s`` that gates both selected ground links, as in v1. Quantization is v1's:
times and delays rounded half up to whole nanoseconds, rates floored.

``run_network_replay`` replays a results directory through a trusted backend and atomically
publishes only independently validated output. The CSV protocol and the C++ adapter are
unchanged from v1.
"""

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

from openleo.replay.network_packet_io import (
    EDGE_FIELDS,
    HOP_FIELDS,
    MAX_HOPS,
    PACKET_FIELDS,
    ROUTE_FIELDS,
    _parse_network_results,
)
from openleo.replay.packet_replay import (
    MAX_RATE_BPS,
    _boundaries,
    _check_output_target,
    _csv_integer,
    _file_sha256,
    _integer,
    _json_text,
    _microseconds,
    _number,
    _read_result,
    _run_backend,
    _utc_text,
    _window,
)
from openleo.replay.source import ROUTE_MODELS, ReplaySource

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
    pair_index: int = 0


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
    _integer(config.pair_index, "pair_index", 0, 1023)
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


class _Edges:
    """Edge states of one route breakpoint, read once per edge."""

    def __init__(self, source, t_s: float, fixed: bool) -> None:
        self.source, self.t_s, self.fixed, self.values = source, t_s, fixed, {}

    def get(self, pair: tuple[int, int]) -> tuple[float, float] | None:
        if pair not in self.values:
            self.values[pair] = self.source.edge_state(*pair, self.t_s, self.fixed)
        return self.values[pair]


def prepare_network_replay(source: ReplaySource, config: NetworkReplayConfig) -> dict:
    """Hold the selected route and its edges from each route breakpoint, with explicit endpoint
    acquisition (module docstring)."""
    start_us, duration_us, acquisition_us, interval = _validate_config(config)
    pairs = source.route_pairs
    if not pairs:
        raise ValueError("these results have no network to replay")
    if config.pair_index >= len(pairs):
        raise ValueError(
            f"pair_index={config.pair_index} must name one of the {len(pairs)} network pairs"
        )
    if config.routing_model not in source.route_models:
        raise ValueError(
            f"routing_model {config.routing_model!r} is not among the results' "
            f"network.route_models {list(source.route_models)}"
        )
    start_s, stop_s = _window(source, start_us, duration_us)
    origin = source.start_utc + timedelta(microseconds=start_us)
    stop = origin + timedelta(microseconds=duration_us)
    duration = duration_us * 1000
    count = source.satellite_count
    source_station, target_station = pairs[config.pair_index]
    source_node, target_node = count + source_station, count + target_station
    fixed = config.routing_model == "fixed_capacity"
    holds = [
        float(t)
        for t in source.breakpoints(
            0.0, stop_s, pair_index=config.pair_index, model=config.routing_model
        )
    ]
    boundaries = _boundaries(holds, start_s, stop_s, duration)
    snapshots, union = [], set()
    endpoint_pair, ready_time = None, None
    for (time_ns, boundary), (next_ns, _) in pairwise(boundaries):
        hold = holds[bisect_right(holds, boundary) - 1]
        raw = source.route(config.pair_index, config.routing_model, hold)
        path = tuple(raw) if raw is not None else ()
        values = _Edges(source, hold, fixed)
        if path:
            if (
                path[0] != source_node
                or path[-1] != target_node
                or len(set(path)) != len(path)
                or any(node >= count for node in path[1:-1])
            ):
                raise ValueError("selected route must be simple and connect only its endpoints")
            route_pairs = tuple(tuple(sorted(pair)) for pair in pairwise(path))
            for pair in route_pairs:
                value = values.get(pair)
                if value is None or value[0] <= 0:
                    raise ValueError("selected route must follow active edges")
            union.update(route_pairs)
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
    snapshots.append((duration, "stop", (), None))
    if len(union) > 1024 or len(snapshots) > 4096 or len(union) * len(snapshots) > 200_000:
        raise ValueError("network replay exceeds edge/frame/edge-state budgets")
    edge_pairs = tuple(sorted(union))
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
            {tuple(sorted((source_node, path[1]))), tuple(sorted((target_node, path[-2])))}
            if path
            else set()
        )
        for edge_id, pair in enumerate(edge_pairs):
            value = values.get(pair) if values is not None else None
            raw_rate, raw_delay = value if value is not None else (0, 0)
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
                "source_node": source_node,
                "target_node": target_node,
                "node_count": count + len(source.station_names),
                "source_station_index": source_station,
                "target_station_index": target_station,
                "pair_index": config.pair_index,
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


V1_LIMITATIONS = (
    "Synthetic reciprocal full-duplex P2P links; not a radio or uplink hardware model.",
    "UDP payload includes the 12-byte measurement header; 30 wire header bytes serialize per hop.",
    "Goodput counts payload delivered inside the half-open window; one-way delays use those deliveries only.",
    "After-window deliveries, end-of-window drops and unresolved packets are separately censored.",
    "Routes and geometric link states are left-held; endpoint acquisition gates both selected ground links.",
    "Hashes and version handshake provide traceability, not authorship attestation.",
)
LIMITATIONS = (
    *V1_LIMITATIONS[:4],
    (
        "Routes, link states and inter-satellite geometry are held from each route breakpoint "
        "(the grid and, with events on, the pass and MODCOD switch times of the endpoint "
        "stations' links) to the next; inter-satellite range and occlusion changes between "
        "breakpoints are not resolved. Endpoint acquisition gates both selected ground links."
    ),
    V1_LIMITATIONS[5],
)


def run_network_replay(results_dir, config, backend, output_dir) -> dict:
    """Replay one route pair of a results directory through a trusted binary and atomically
    publish only independently valid output. Returns the summary."""
    source = ReplaySource.from_results(Path(results_dir))
    prepared = prepare_network_replay(source, config)
    return _execute_network_replay(prepared, source.provenance(), backend, output_dir, LIMITATIONS)


def _execute_network_replay(prepared, source, backend, output_dir, limitations) -> dict:
    """Execute a trusted binary on prepared input and atomically publish only independently
    valid output."""
    target, executable = Path(output_dir), Path(backend)
    config = NetworkReplayConfig(**prepared["metadata"]["configuration"])
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
            "limitations": list(limitations),
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
