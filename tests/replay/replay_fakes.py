"""A hand-built replay source: link, route and ISL states declared per frame and left-held.

It answers the ``ReplaySource`` queries the bridges use, so the bridge tests can declare exact
rates, delays and routes (v1's hand-edited documents) without running the engine.
"""

from __future__ import annotations

from bisect import bisect_right
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime

import numpy as np

MODELS = ("minimum_delay", "maximum_rate", "fixed_capacity")


@dataclass
class FrameSource:
    offsets: list[float]
    links: list[dict[tuple[int, int], tuple[float, float]]]
    routes: list[dict[str, tuple[int, ...] | None]] = field(default_factory=list)
    isl: list[dict[tuple[int, int], float]] = field(default_factory=list)
    station_names: tuple[str, ...] = ("Cartagena", "Neighbor")
    norad_ids: tuple[int, ...] = (25544,)
    start_utc: datetime = datetime(2026, 8, 30, 6, 17, tzinfo=UTC)
    route_pairs: tuple[tuple[int, int], ...] = ((0, 1),)
    route_models: tuple[str, ...] = MODELS
    fixed_capacity_bps: float = 1_000_000.0
    isl_capacity_bps: float = 10_000_000.0

    @property
    def window_s(self) -> float:
        return float(self.offsets[-1])

    @property
    def satellite_count(self) -> int:
        return len(self.norad_ids)

    def _frame(self, t: float) -> int:
        return bisect_right(self.offsets, t) - 1

    def link_state(self, station_index, satellite_index, t_s):
        return self.links[self._frame(t_s)].get((station_index, satellite_index))

    def route(self, pair_index, model, t_s):
        return self.routes[self._frame(t_s)].get(model)

    def edge_state(self, a, b, t_s, fixed):
        count = self.satellite_count
        frame = self._frame(t_s)
        if b < count:
            delay = self.isl[frame].get((a, b)) if self.isl else None
            return None if delay is None else (self.isl_capacity_bps, delay)
        state = self.link_state(b - count, a, t_s)
        if state is None:
            return None
        return (self.fixed_capacity_bps if fixed else state[0], state[1])

    def breakpoints(self, start_s, stop_s, **selection):
        times = np.array(self.offsets, dtype=np.float64)
        return times[(times >= start_s) & (times <= stop_s)]

    def copy(self) -> FrameSource:
        return deepcopy(self)


def packet_source() -> FrameSource:
    """v1's single-link fixture: a fractional rate, an outage frame and a terminal frame."""
    return FrameSource(
        offsets=[0.0, 10.0, 20.0],
        links=[
            {(0, 0): (10.75, 0.0010000004), (1, 0): (0.0, 0.003)},
            {(1, 0): (0.0, 0.003)},
            {(0, 0): (0.0, 0.002), (1, 0): (0.0, 0.003)},
        ],
        routes=[{}, {}, {}],
    )


def network_source() -> FrameSource:
    """v1's two-hop fixture: both stations see the one satellite, routed 1 -> 0 -> 2."""
    link = (1_000_000.75, 0.0020000004)
    return FrameSource(
        offsets=[0.0, 10.0, 20.0],
        links=[{(0, 0): link, (1, 0): link} for _ in range(3)],
        routes=[dict.fromkeys(MODELS, (1, 0, 2)) for _ in range(3)],
        isl=[{} for _ in range(3)],
    )


def multihop_source(second_path) -> FrameSource:
    """Four satellites with ISLs; the route is 4-0-1-3-5, then ``second_path``."""
    link = (1_000_000.75, 0.0020000004)
    paths = ((4, 0, 1, 3, 5), tuple(second_path), tuple(second_path))
    edges = dict.fromkeys(((0, 1), (0, 2), (1, 2), (1, 3), (2, 3)), 0.001)
    return FrameSource(
        offsets=[0.0, 10.0, 20.0],
        links=[{(s, n): link for s in (0, 1) for n in range(4)} for _ in range(3)],
        routes=[dict.fromkeys(MODELS, path) for path in paths],
        isl=[dict(edges) for _ in range(3)],
        norad_ids=(25544, 25545, 25546, 25547),
    )
