"""Reciprocal snapshot routing with WGS84 Earth occlusion.

All links are synthetic, simultaneous, undirected forwarding opportunities.
Only the source and target stations participate, without ground relay edges.
The fixed baseline replaces ground-link rates and retains the same ISL capacity.
Path bottlenecks omit scheduling, contention, queues and transport protocols.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from heapq import heappop, heappush
from itertools import combinations, pairwise
from math import dist, fsum, hypot, inf, isfinite
from typing import Any

from skyfield.api import wgs84

from openleo.input import (
    _finite_number,
    _non_negative_number,
    _object,
    _positive_number,
    _string,
    _utc,
)
from openleo.physics import propagation_delay_s

NETWORK_KEYS = frozenset(
    (
        "source_station",
        "target_station",
        "isl_max_range_m",
        "isl_capacity_bps",
        "fixed_capacity_bps",
    )
)
ROUTE_MODELS = ("minimum_delay", "maximum_rate", "fixed_capacity")
_EQUATORIAL_RADIUS_M = float(wgs84.radius.m)
_POLAR_RADIUS_M = _EQUATORIAL_RADIUS_M * (1.0 - 1.0 / wgs84.inverse_flattening)
_AXES_M = (_EQUATORIAL_RADIUS_M, _EQUATORIAL_RADIUS_M, _POLAR_RADIUS_M)
Graph = Sequence[Sequence[tuple[int, float, float]]]


@dataclass(frozen=True, slots=True)
class NetworkConfig:
    source_station: str
    target_station: str
    isl_max_range_m: float
    isl_capacity_bps: float
    fixed_capacity_bps: float


def parse_network(raw: Any, station_names: Sequence[str]) -> NetworkConfig:
    """Validate the five declared assumptions; zero range disables ISLs."""
    try:
        data = _object(raw, NETWORK_KEYS, "network")
        source = _string(data["source_station"], "network.source_station")
        target = _string(data["target_station"], "network.target_station")
        if len(set(station_names)) != len(station_names):
            raise ValueError("station names must be unique")
        if source not in station_names or target not in station_names:
            raise ValueError("network source_station and target_station must name known stations")
        if source == target:
            raise ValueError("network source_station and target_station must be distinct")
        return NetworkConfig(
            source,
            target,
            _non_negative_number(data["isl_max_range_m"], "network.isl_max_range_m"),
            _positive_number(data["isl_capacity_bps"], "network.isl_capacity_bps"),
            _positive_number(data["fixed_capacity_bps"], "network.fixed_capacity_bps"),
        )
    except (TypeError, OverflowError) as exc:
        raise ValueError(f"invalid network configuration: {exc}") from exc


def _position(raw: Any, path: str) -> tuple[float, float, float]:
    if not isinstance(raw, (list, tuple)) or len(raw) != 3:
        raise ValueError(f"{path} must contain three ECEF coordinates")
    return tuple(_finite_number(value, path) for value in raw)


def _scaled(position: Sequence[float]) -> tuple[float, float, float]:
    return tuple(coordinate / axis for coordinate, axis in zip(position, _AXES_M, strict=True))


def _clear_segment(first: Sequence[float], second: Sequence[float]) -> bool:
    # Scaling the WGS84 axes converts ellipsoid intersection into a unit-sphere
    # closest-point test on the finite segment. Surface tangency is occluded.
    delta = tuple(b - a for a, b in zip(first, second, strict=True))
    length = hypot(*delta)
    if length == 0.0:
        return hypot(*first) > 1.0
    direction = tuple(component / length for component in delta)
    along = min(length, max(0.0, -fsum(a * u for a, u in zip(first, direction, strict=True))))
    return hypot(*(a + along * u for a, u in zip(first, direction, strict=True))) > 1.0


def has_line_of_sight(first_ecef_m: Sequence[float], second_ecef_m: Sequence[float]) -> bool:
    """Whether an ECEF segment avoids the solid WGS84 ellipsoid, including grazing."""
    return _clear_segment(
        _scaled(_position(first_ecef_m, "first_ecef_m")),
        _scaled(_position(second_ecef_m, "second_ecef_m")),
    )


def _endpoints(graph: Graph, source: int, target: int) -> None:
    for node in (source, target):
        if isinstance(node, bool) or not isinstance(node, int) or not 0 <= node < len(graph):
            raise ValueError("route endpoint must be a valid node index")
    if source == target:
        raise ValueError("route endpoints must be distinct")


def shortest_path(
    graph: Graph,
    source: int,
    target: int,
    *,
    minimum_capacity_bps: float = 0.0,
) -> dict[str, Any] | None:
    """Dijkstra over validated (neighbor, nonnegative delay, capacity) adjacency.

    Equal delays prefer fewer hops, then lexicographic node indices. A heap entry
    carries its immutable path, so ties and zero-delay edges cannot form cycles.
    """
    _endpoints(graph, source, target)
    queue = [(0.0, 0, (source,), inf)]
    best = {source: (0.0, 0, (source,))}
    while queue:
        delay, hops, path, bottleneck = heappop(queue)
        node = path[-1]
        if best[node] != (delay, hops, path):
            continue
        if node == target:
            return {
                "nodes": list(path),
                "delay_s": _non_negative_number(delay, "route.delay_s"),
                "bottleneck_bps": bottleneck,
            }
        for neighbor, edge_delay, capacity in graph[node]:
            if capacity <= 0.0 or capacity < minimum_capacity_bps or neighbor in path:
                continue
            candidate = (delay + edge_delay, hops + 1, (*path, neighbor))
            if neighbor not in best or candidate < best[neighbor]:
                best[neighbor] = candidate
                heappush(queue, (*candidate, min(bottleneck, capacity)))
    return None


def widest_path(graph: Graph, source: int, target: int) -> dict[str, Any] | None:
    """Maximize bottleneck, then minimize delay among all equally wide routes."""
    _endpoints(graph, source, target)
    queue = [(-inf, source)]
    best = {source: inf}
    while queue:
        negative_capacity, node = heappop(queue)
        capacity = -negative_capacity
        if capacity < best[node]:
            continue
        if node == target:
            # One widest label per vertex cannot also minimize the final path's
            # delay: a later narrow edge may equalize differently wide prefixes.
            return shortest_path(graph, source, target, minimum_capacity_bps=capacity)
        for neighbor, _, edge_capacity in graph[node]:
            candidate = min(capacity, edge_capacity)
            if candidate > best.get(neighbor, 0.0):
                best[neighbor] = candidate
                heappush(queue, (-candidate, neighbor))
    return None


def _list(raw: Any, path: str, minimum: int, maximum: int) -> list:
    if not isinstance(raw, list) or not minimum <= len(raw) <= maximum:
        raise ValueError(f"{path} must be a list with {minimum}..{maximum} entries")
    return raw


def _positions(satellites: list, sample_count: int) -> tuple:
    positions = tuple(
        tuple(
            _position(xyz, f"satellites[{index}].positions_ecef_m")
            for xyz in _list(
                satellite["positions_ecef_m"],
                f"satellites[{index}].positions_ecef_m",
                sample_count,
                sample_count,
            )
        )
        for index, satellite in enumerate(satellites)
    )
    if any(hypot(*_scaled(xyz)) <= 1.0 for track in positions for xyz in track):
        raise ValueError("satellite positions must be outside the WGS84 ellipsoid")
    return positions


def _ground_edges(raw: Any, satellite_count: int, station_count: int) -> tuple:
    links = _list(raw, "links frame", 0, satellite_count * station_count)
    edges = []
    pairs = set()
    for link in links:
        station, satellite = link["station_index"], link["satellite_index"]
        for value, count, field in (
            (station, station_count, "station_index"),
            (satellite, satellite_count, "satellite_index"),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < count:
                raise ValueError(f"links.{field} must be a valid index")
        pair = (satellite, satellite_count + station)
        if pair in pairs:
            raise ValueError("links frame contains a duplicate satellite-station pair")
        pairs.add(pair)
        delay = _positive_number(link["delay_s"], "links.delay_s")
        rate = _non_negative_number(link["rate_bps"], "links.rate_bps")
        edges.append((*pair, delay, rate))
    return tuple(sorted(edges))


def _adjacency(node_count: int, edges: tuple) -> Graph:
    graph = [[] for _ in range(node_count)]
    for first, second, delay, capacity in edges:
        if capacity > 0.0:
            graph[first].append((second, delay, capacity))
            graph[second].append((first, delay, capacity))
    return tuple(tuple(neighbors) for neighbors in graph)


def _frame(
    positions: tuple,
    ground_edges: tuple,
    settings: NetworkConfig,
    node_count: int,
    source: int,
    target: int,
) -> dict:
    scaled = tuple(_scaled(position) for position in positions)
    isl_edges = []
    # Evaluate every pair in the bounded catalog (at most 128 satellites).
    for first, second in combinations(range(len(positions)), 2):
        range_m = dist(positions[first], positions[second])
        if not isfinite(range_m):
            raise ValueError("satellite separation must be finite")
        if 0.0 < range_m <= settings.isl_max_range_m and _clear_segment(
            scaled[first], scaled[second]
        ):
            isl_edges.append((first, second, propagation_delay_s(range_m)))
    isls = tuple((*edge, settings.isl_capacity_bps) for edge in isl_edges)
    endpoints = tuple(edge for edge in ground_edges if edge[1] in (source, target))
    baseline = tuple((a, b, delay, settings.fixed_capacity_bps) for a, b, delay, _ in endpoints)
    adaptive = _adjacency(node_count, (*isls, *endpoints))
    fixed = _adjacency(node_count, (*isls, *baseline))
    return {
        "isl_edges": [list(edge) for edge in isl_edges],
        "minimum_delay": shortest_path(adaptive, source, target),
        "maximum_rate": widest_path(adaptive, source, target),
        "fixed_capacity": shortest_path(fixed, source, target),
    }


def _summary(frames: list, timestamps: tuple) -> dict:
    intervals = tuple((second - first).total_seconds() for first, second in pairwise(timestamps))
    summary = {}
    for model in ROUTE_MODELS:
        routes = tuple(frame[model] for frame in frames)
        bits = fsum(
            (route["bottleneck_bps"] if route else 0.0) * seconds
            for route, seconds in zip(routes, intervals)
        )
        summary[model] = {
            "connected_sample_fraction": sum(route is not None for route in routes) / len(routes),
            "integrated_bottleneck_bits": _non_negative_number(bits, "integrated_bottleneck_bits"),
            "route_changes": sum(
                first["nodes"] != second["nodes"]
                for first, second in pairwise(routes)
                if first is not None and second is not None
            ),
        }
    return summary


def add_network(document: Mapping[str, Any]) -> dict[str, Any]:
    """Return an enriched document without mutating input; validate consumed fields.

    Integrals left-hold adjacent samples and omit any interval after the final
    sample. Route changes compare consecutive connected samples only; outage and
    recovery affect connected_sample_fraction instead. Neither is availability
    probability or measured throughput.
    """
    try:
        if document["kind"] != "openleo.constellation" or document["schema_version"] != "1":
            raise ValueError("network requires an openleo.constellation schema_version 1 document")
        stations = _list(document["stations"], "stations", 2, 16)
        names = tuple(_string(station["name"], "stations.name") for station in stations)
        settings = parse_network(document["scenario"]["network"], names)
        timestamps = tuple(
            _utc(value, "timestamps_utc")
            for value in _list(
                document["timestamps_utc"],
                "timestamps_utc",
                1,
                1441,
            )
        )
        if any(first >= second for first, second in pairwise(timestamps)):
            raise ValueError("timestamps_utc must be strictly increasing")
        satellites = _list(document["satellites"], "satellites", 1, 128)
        if len(satellites) * len(stations) * len(timestamps) > 500_000:
            raise ValueError("network exceeds 500000 satellite-station-samples")
        positions = _positions(satellites, len(timestamps))
        ground_frames = tuple(
            _ground_edges(raw, len(satellites), len(stations))
            for raw in _list(
                document["links"],
                "links",
                len(timestamps),
                len(timestamps),
            )
        )
        source, target = names.index(settings.source_station), names.index(settings.target_station)
        frames = [
            _frame(
                tuple(track[index] for track in positions),
                edges,
                settings,
                len(satellites) + len(stations),
                len(satellites) + source,
                len(satellites) + target,
            )
            for index, edges in enumerate(ground_frames)
        ]
        return {
            **document,
            "network": {
                "source_station_index": source,
                "target_station_index": target,
                "frames": frames,
                "summary": _summary(frames, timestamps),
            },
        }
    except (KeyError, TypeError, OverflowError) as exc:
        raise ValueError(f"invalid constellation network input: {exc}") from exc
