"""Snapshot routing on the v1 graph, evaluated at route breakpoints (design spec section 6.5).

The graph rule and the route algorithms are OpenLEO v1.0.0's (``openleo.network``), moved here
unchanged: undirected edges (neighbor, nonnegative delay, capacity), Dijkstra with ties broken
by fewer hops and then lexicographic node indices (``shortest_path``), the widest path with the
minimum delay among equally wide routes (``widest_path``), and Earth occlusion against the WGS84
ellipsoid with surface tangency occluded (``has_line_of_sight``).
"""

from __future__ import annotations

from collections.abc import Sequence
from heapq import heappop, heappush
from math import fsum, hypot, inf
from typing import Any

from skyfield.api import wgs84

from openleo._validation import _finite_number, _non_negative_number

_EQUATORIAL_RADIUS_M = float(wgs84.radius.m)
_POLAR_RADIUS_M = _EQUATORIAL_RADIUS_M * (1.0 - 1.0 / wgs84.inverse_flattening)
_AXES_M = (_EQUATORIAL_RADIUS_M, _EQUATORIAL_RADIUS_M, _POLAR_RADIUS_M)
Graph = Sequence[Sequence[tuple[int, float, float]]]


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


def _adjacency(node_count: int, edges: tuple) -> Graph:
    graph = [[] for _ in range(node_count)]
    for first, second, delay, capacity in edges:
        if capacity > 0.0:
            graph[first].append((second, delay, capacity))
            graph[second].append((first, delay, capacity))
    return tuple(tuple(neighbors) for neighbors in graph)
