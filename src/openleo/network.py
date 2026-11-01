"""Reciprocal snapshot routing with WGS84 Earth occlusion.

All links are synthetic, simultaneous, undirected forwarding opportunities.
Only the source and target stations participate, without ground relay edges.
The fixed baseline replaces ground-link rates and retains the same ISL capacity.
Path bottlenecks omit scheduling, contention, queues and transport protocols.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from itertools import combinations, pairwise
from math import dist, fsum, hypot, isfinite
from typing import Any

from openleo._validation import (
    _non_negative_number,
    _object,
    _positive_number,
    _string,
    _utc,
)

# The route algorithms moved to openleo.engine.routing; re-exported for the v1 layer and its
# callers until the v1 modules are removed.
from openleo.engine.routing import (  # noqa: F401
    _adjacency,
    _clear_segment,
    _position,
    _scaled,
    has_line_of_sight,
    shortest_path,
    widest_path,
)
from openleo.physics.link import propagation_delay_s

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
        if document["kind"] != "openleo.constellation" or document["schema_version"] not in (
            "1",
            "2",
            "3",
        ):
            raise ValueError(
                "network requires an openleo.constellation schema_version 1, 2 or 3 document"
            )
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
