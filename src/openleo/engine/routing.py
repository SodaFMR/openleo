"""Snapshot routing on the v1 graph, evaluated at route breakpoints (design spec section 6.5).

The graph rule and the route algorithms are OpenLEO v1.0.0's (``openleo.network``), moved here
unchanged: undirected edges (neighbor, nonnegative delay, capacity), Dijkstra with ties broken
by fewer hops and then lexicographic node indices (``shortest_path``), the widest path with the
minimum delay among equally wide routes (``widest_path``), and Earth occlusion against the WGS84
ellipsoid with surface tangency occluded (``has_line_of_sight``).

The graph at one instant (v1 ``_frame``). Nodes are the satellites 0..N-1, then the stations
N..N+M-1. Every satellite pair at a range r with 0 < r <= ``isl.max_range_m`` and a clear line of
sight is an inter-satellite link of delay r/c and capacity ``isl.capacity_bps``. The ground links
are those of the route pair's two stations only (no other station relays), one per visible
satellite, with the link's delay. ``minimum_delay`` (``shortest_path``) and ``maximum_rate``
(``widest_path``) use the link's rate as capacity, so a visible link without MODCOD lock (rate 0)
is no edge; ``fixed_capacity`` (``shortest_path``) gives every visible ground link of the two
stations ``fixed_capacity_bps``, with or without lock, and keeps the same inter-satellite links.

Breakpoints (``route_breakpoints``) are the grid plus every pass and rate-segment boundary of the
pairs whose station is a route endpoint. ``evaluate_routes`` builds the graph at each breakpoint
from the exact satellite positions there and the link state (``LinkState``) there, runs the
models and holds each result until the next breakpoint; inter-satellite range and occlusion
changes between breakpoints are not resolved (spec section 6.5). The last breakpoint ends the
last interval and is not evaluated, as v1's final sample holds no time.
"""

from __future__ import annotations

import math
from bisect import bisect_right
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from heapq import heappop, heappush
from itertools import pairwise
from math import dist, fsum, hypot, inf
from typing import TYPE_CHECKING, Any

import numpy as np
from skyfield.api import wgs84

from openleo._validation import _finite_number, _non_negative_number, _positive_number
from openleo.engine.integrate import integrate_piecewise, measure
from openleo.engine.statistics import _pair_intervals
from openleo.physics.link import propagation_delay_s

if TYPE_CHECKING:
    from numpy.typing import ArrayLike, NDArray

    from openleo.engine.statistics import PairResult
    from openleo.scenario.model import NetworkConfig

LinkState = Callable[[int, int, float], "tuple[float, float] | None"]
"""(station, satellite, t) -> (rate_bps, delay_s), or None when the pair is not visible at t."""

# The vectorised range and occlusion tests decide only pairs farther than this relative margin
# from a threshold; v1's scalar test decides the rest, so the edge set is v1's exactly.
_PREFILTER_SLACK = 1e-9

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


@dataclass(frozen=True, slots=True)
class RouteInterval:
    """The route of one model for one configured pair on [start_s, end_s) (seconds).

    ``nodes``, ``delay_s`` and ``bottleneck_bps`` are all None while the pair is disconnected.
    """

    pair_index: int
    model: str
    start_s: float
    end_s: float
    nodes: tuple[int, ...] | None
    delay_s: float | None
    bottleneck_bps: float | None


def route_breakpoints(
    grid_s: ArrayLike,
    pairs: Sequence[PairResult],
    station_indices: set[int],
    window_s: float,
) -> NDArray[np.float64]:
    """Sorted unique union of the grid and every pass start/end and rate-segment start/end of
    the pairs whose station is in ``station_indices``, restricted to [0, ``window_s``]."""
    window_s = _positive_number(window_s, "window_s")
    grid = np.asarray(grid_s, dtype=np.float64)
    if grid.ndim != 1 or not np.isfinite(grid).all():
        raise ValueError("grid_s must be a one-dimensional array of finite seconds")
    times = [grid]
    for pair in pairs:
        if pair.station_index not in station_indices:
            continue
        passes, segments = _pair_intervals(pair)
        times.append(
            np.array([t for item in (*passes, *segments) for t in (item.start_s, item.end_s)])
        )
    union = np.unique(np.concatenate(times))
    return union[(union >= 0.0) & (union <= window_s)]


def link_state_from_pairs(
    pairs: Sequence[PairResult], delay_s: Callable[[int, int, float], float]
) -> LinkState:
    """The ``LinkState`` of the engine's pairs: visible on each pass's [start_s, end_s), at the
    rate of the rate segment holding t (0.0 if none does), with ``delay_s(station, satellite,
    t)``, which is called only at visible times."""
    tracks = {}
    for pair in pairs:
        key = (pair.station_index, pair.satellite_index)
        if key in tracks:
            raise ValueError(f"pair (station {key[0]}, satellite {key[1]}) appears more than once")
        passes, segments = _pair_intervals(pair)
        tracks[key] = (
            [p.start_s for p in passes],
            [p.end_s for p in passes],
            [s.start_s for s in segments],
            [s.end_s for s in segments],
            [s.rate_bps for s in segments],
        )

    def state(station: int, satellite: int, t: float) -> tuple[float, float] | None:
        track = tracks.get((station, satellite))
        if track is None:
            return None
        pass_starts, pass_ends, segment_starts, segment_ends, rates = track
        index = bisect_right(pass_starts, t) - 1
        if index < 0 or t >= pass_ends[index]:
            return None
        index = bisect_right(segment_starts, t) - 1
        rate = rates[index] if index >= 0 and t < segment_ends[index] else 0.0
        return rate, delay_s(station, satellite, t)

    return state


def evaluate_routes(
    network: NetworkConfig,
    station_names: tuple[str, ...],
    satellite_ecef: Callable[[NDArray[np.float64]], NDArray[np.float64]],
    link_state: LinkState,
    breakpoints_s: ArrayLike,
    satellite_count: int,
) -> tuple[RouteInterval, ...]:
    """Every configured pair's route under every configured model, held between breakpoints.

    ``satellite_ecef(times)`` returns ITRS positions (m) of all satellites, shape
    (len(times), satellite_count, 3). Results are ordered by pair, then model (in
    ``network.route_models`` order), then time; neighbouring intervals with identical results
    are merged, so the intervals of one pair and model tile [breakpoints[0], breakpoints[-1]).
    """
    if (
        isinstance(satellite_count, bool)
        or not isinstance(satellite_count, int | np.integer)
        or satellite_count < 1
    ):
        raise ValueError(f"satellite_count={satellite_count!r} must be a positive integer")
    satellite_count = int(satellite_count)
    breakpoints = np.asarray(breakpoints_s, dtype=np.float64)
    if breakpoints.ndim != 1 or breakpoints.size < 2:
        raise ValueError("breakpoints_s must be a one-dimensional array of at least two times")
    if not np.isfinite(breakpoints).all():
        raise ValueError("breakpoints_s must be finite")
    if not (np.diff(breakpoints) > 0.0).all():
        raise ValueError("breakpoints_s must be strictly increasing")
    models = tuple(network.route_models)
    unknown = [model for model in models if model not in _ALGORITHMS]
    if unknown:
        raise ValueError(f"unknown route models {unknown}; expected some of {tuple(_ALGORITHMS)}")
    kinds = {_ALGORITHMS[model][1] for model in models}
    fixed_bps = (
        _positive_number(network.fixed_capacity_bps, "network.fixed_capacity_bps")
        if "fixed" in kinds
        else None
    )
    isl = network.isl
    max_range_m = _non_negative_number(isl.max_range_m, "network.isl.max_range_m")
    isl_bps = _positive_number(isl.capacity_bps, "network.isl.capacity_bps")
    endpoints = []
    for index, pair in enumerate(network.pairs):
        nodes = []
        for name in (pair.source, pair.target):
            if name not in station_names:
                raise ValueError(f"network pair {index} names unknown station {name!r}")
            nodes.append(station_names.index(name))
        if nodes[0] == nodes[1]:
            raise ValueError(f"network pair {index} routes station {pair.source!r} to itself")
        endpoints.append(tuple(nodes))
    stations = sorted({station for pair in endpoints for station in pair})
    node_count = satellite_count + len(station_names)
    evaluated = breakpoints[:-1]
    positions = _satellite_positions(satellite_ecef, evaluated, satellite_count)

    results: dict[tuple[int, str], list] = {
        (index, model): [] for index in range(len(endpoints)) for model in models
    }
    for k, t in enumerate(evaluated.tolist()):
        isls = tuple(
            (first, second, delay, isl_bps)
            for first, second, delay in _isl_edges(positions[k], max_range_m)
        )
        ground = {
            station: _ground_edges(link_state, station, satellite_count, t) for station in stations
        }
        for index, (source, target) in enumerate(endpoints):
            edges = tuple(sorted((*ground[source], *ground[target])))
            source_node, target_node = satellite_count + source, satellite_count + target
            graphs = {
                kind: _adjacency(node_count, (*isls, *ground_links))
                for kind, ground_links in (
                    ("adaptive", edges),
                    ("fixed", tuple((a, b, delay, fixed_bps) for a, b, delay, _ in edges)),
                )
                if kind in kinds
            }
            for model in models:
                algorithm, kind = _ALGORITHMS[model]
                route = algorithm(graphs[kind], source_node, target_node)
                results[(index, model)].append(route)

    intervals: list[RouteInterval] = []
    ends = breakpoints[1:].tolist()
    for (index, model), routes in results.items():
        merged: list[RouteInterval] = []
        for start, end, route in zip(evaluated.tolist(), ends, routes, strict=True):
            values = (
                (tuple(route["nodes"]), route["delay_s"], route["bottleneck_bps"])
                if route is not None
                else (None, None, None)
            )
            last = merged[-1] if merged else None
            if last is not None and (last.nodes, last.delay_s, last.bottleneck_bps) == values:
                merged[-1] = RouteInterval(index, model, last.start_s, end, *values)
            else:
                merged.append(RouteInterval(index, model, start, end, *values))
        intervals += merged
    return tuple(intervals)


def route_metrics(
    intervals: Sequence[RouteInterval], window_s: float
) -> dict[tuple[int, str], dict[str, float | int]]:
    """Route metrics per (pair_index, model), in order of first appearance (spec section 6.5).

    - ``connected_fraction``: the measure of the connected intervals over ``window_s``;
    - ``integrated_bottleneck_bits``: the bottleneck integrated exactly over them;
    - ``route_changes``: neighbouring connected intervals (one ending where the next starts)
      with different node sequences; an outage separates two routes without a change (v1's
      rule on consecutive samples);
    - ``mean_delay_connected_s``: the time-weighted mean delay over connected time, NaN when
      never connected.
    """
    window_s = _positive_number(window_s, "window_s")
    groups: dict[tuple[int, str], list[RouteInterval]] = {}
    for interval in intervals:
        groups.setdefault((interval.pair_index, interval.model), []).append(interval)
    metrics = {}
    for key, group in groups.items():
        group.sort(key=lambda i: (i.start_s, i.end_s))
        for interval in group:
            start = _finite_number(interval.start_s, "route interval start_s")
            end = _finite_number(interval.end_s, "route interval end_s")
            if not 0.0 <= start <= end <= window_s:
                raise ValueError(
                    f"route interval [{start!r}, {end!r}) of pair {key[0]} ({key[1]}) is not "
                    f"inside the window [0, {window_s!r}]"
                )
            if interval.nodes is not None:
                _non_negative_number(interval.delay_s, "route interval delay_s")
                _non_negative_number(interval.bottleneck_bps, "route interval bottleneck_bps")
        for left, right in pairwise(group):
            if right.start_s < left.end_s:
                raise ValueError(
                    f"route intervals of pair {key[0]} ({key[1]}) overlap: "
                    f"[{left.start_s!r}, {left.end_s!r}) and [{right.start_s!r}, {right.end_s!r})"
                )
        connected = [i for i in group if i.nodes is not None]
        starts = [i.start_s for i in connected]
        ends = [i.end_s for i in connected]
        connected_s = measure(zip(starts, ends, strict=True))
        metrics[key] = {
            "connected_fraction": connected_s / window_s,
            "integrated_bottleneck_bits": integrate_piecewise(
                starts, ends, [i.bottleneck_bps for i in connected]
            ),
            "route_changes": sum(
                1
                for left, right in pairwise(group)
                if left.nodes is not None
                and right.nodes is not None
                and left.end_s == right.start_s
                and left.nodes != right.nodes
            ),
            "mean_delay_connected_s": integrate_piecewise(
                starts, ends, [i.delay_s for i in connected]
            )
            / connected_s
            if connected_s > 0.0
            else math.nan,
        }
    return metrics


# v1 _frame: the algorithm of each model and its graph, "adaptive" (ground capacities are the
# link rates) or "fixed" (every visible ground link at fixed_capacity_bps).
_ALGORITHMS = {
    "minimum_delay": (shortest_path, "adaptive"),
    "maximum_rate": (widest_path, "adaptive"),
    "fixed_capacity": (shortest_path, "fixed"),
}


def _satellite_positions(satellite_ecef, times, satellite_count):
    positions = np.asarray(satellite_ecef(times), dtype=np.float64)
    if positions.shape != (times.size, satellite_count, 3):
        raise ValueError(
            f"satellite_ecef returned shape {positions.shape}; expected "
            f"{(times.size, satellite_count, 3)}"
        )
    if not np.isfinite(positions).all():
        raise ValueError("satellite positions must be finite")
    if (np.linalg.norm(positions / np.array(_AXES_M), axis=-1) <= 1.0).any():
        raise ValueError("satellite positions must be outside the WGS84 ellipsoid")
    return positions


def _isl_edges(positions: NDArray[np.float64], max_range_m: float) -> list:
    """v1's inter-satellite links (first, second, delay) at one instant, in v1's pair order.

    Every range is v1's ``math.dist`` compared with v1's ``0 < range <= max_range_m``, and the
    delay is v1's ``propagation_delay_s(range)``. Vectorised tests only skip work: pairs out of
    range by more than ``_PREFILTER_SLACK`` (relative) are dropped, and v1's scalar occlusion test
    runs only where the vectorised closest approach to the unit sphere (in WGS84-scaled
    coordinates) is within ``_PREFILTER_SLACK`` of 1. The vectorised values differ from the
    scalar ones by a few ulps, far inside the margin, so the edges are v1's.
    """
    count = positions.shape[0]
    if max_range_m == 0.0 or count < 2:
        return []
    first, second = np.triu_indices(count, 1)  # itertools.combinations order
    delta = positions[second] - positions[first]
    near = np.sqrt(np.einsum("ij,ij->i", delta, delta)) <= max_range_m * (1.0 + _PREFILTER_SLACK)
    first, second = first[near], second[near]
    scaled = positions / np.array(_AXES_M)
    a = scaled[first]
    d = scaled[second] - a
    length = np.sqrt(np.einsum("ij,ij->i", d, d))
    with np.errstate(invalid="ignore", divide="ignore"):
        u = d / length[:, None]
        along = np.minimum(length, np.maximum(0.0, -np.einsum("ij,ij->i", a, u)))
        closest = np.linalg.norm(a + along[:, None] * u, axis=1)
    # NaN (coincident satellites) compares False both ways and goes to the scalar test.
    clear = closest > 1.0 + _PREFILTER_SLACK
    undecided = ~clear & ~(closest <= 1.0 - _PREFILTER_SLACK)
    rows = positions.tolist()
    scaled_rows = scaled.tolist()
    edges = []
    for i, j, is_clear, check in zip(
        first.tolist(), second.tolist(), clear.tolist(), undecided.tolist(), strict=True
    ):
        if not (is_clear or check):
            continue
        range_m = dist(rows[i], rows[j])
        if 0.0 < range_m <= max_range_m and (
            is_clear or _clear_segment(scaled_rows[i], scaled_rows[j])
        ):
            edges.append((i, j, propagation_delay_s(range_m)))
    return edges


def _ground_edges(link_state: LinkState, station: int, satellite_count: int, t: float) -> list:
    """(satellite, station node, delay, rate) of every satellite the station sees at t."""
    edges = []
    for satellite in range(satellite_count):
        state = link_state(station, satellite, t)
        if state is None:
            continue
        rate, delay = state
        edges.append(
            (
                satellite,
                satellite_count + station,
                _positive_number(delay, "link state delay_s"),
                _non_negative_number(rate, "link state rate_bps"),
            )
        )
    return edges
