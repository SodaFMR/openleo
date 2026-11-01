"""One run: a scenario in, ``Results`` out (design spec sections 6 and 7).

``run`` wires the engine together and never mutates its inputs:

1. ``catalog``: the grid and the catalogs. Before anything is propagated it checks the step
   against the event bound (``step_s <= P_min / 8`` with events on, P_min the shortest orbital
   period in the catalogs, spec section 6.1) and the memory estimate against the budget.
2. ``geometry``: satellite positions and elevations at the grid times.
3. ``events``: passes and MODCOD segments of every pair. With events on, AOS, LOS and
   culminations are root-found (``find_passes``) and the MODCOD state machine runs in continuous
   time (``acm_segments``), both with ``time.events.tolerance_s``. With events off, visibility
   and MODCOD selection are evaluated at the grid samples (``passes_on_grid``,
   ``acm_on_grid``): v1's semantics.
4. ``links``: the link budget at every visible grid sample (the ``samples`` table) and the
   Shannon upper bound integrated over every pass.
5. ``routes``: snapshot routes on the route breakpoints, if a network is configured.
6. ``convergence``: the grid-convergence gate (``openleo.engine.convergence``).
7. ``tables``: the results tables and ``results.json`` metadata.

The phase names are passed to ``progress`` as each phase starts.

Shannon bound. With events on it is integrated over each pass with adaptive 5-point
Gauss-Legendre (``integrate_smooth``) on the pass's interior breakpoints (grid times,
culmination, rate-segment ends), every piece first split to at most ``SHANNON_PIECE_S`` seconds
so a sharp change cannot be accepted on a coarse piece. The integrand is evaluated only at
Gauss-Legendre nodes, which are interior, and never past ``end_s - tolerance_s`` of a pass with a
root-found LOS: that LOS lies within the tolerance on the invisible side, where the link budget
is undefined. As ``acm_segments`` holds the MODCOD state, the bound at ``end_s - tolerance_s`` is
held over that last sliver. With events off it is left-held from each visible sample (v1).

Integrand. The quadrature nodes and the MODCOD root-finding (``acm_segments``) read the link
evaluator's ``tabulated_shannon_bps`` and ``tabulated_available_db``: the v1 link budget with
the gaseous loss from each station's verified gas table (within 1e-10 dB of the ray trace,
``openleo.physics.atmospheric_path.GasTable``), recorded in ``models`` as
``shannon_integrand_atmosphere``. The ``samples`` table, minimum ranges and route delays use the
exact ``evaluate``.

Grid samples. The ``samples`` table holds every (station, satellite, grid time) at or above the
mask. Its MODCOD and rate are those of the rate segment holding the time; at the end of a pass
that is visible at the window stop (no segment holds that instant) they are the state in effect
there: that of the last decision effective before the pass end. As in ``acm_segments``, a
decision effective at or after the pass end is dropped (it records a ``modcod_decision`` event
but no ``modcod_effective`` one), so a lock decided exactly at the window stop is not reported
at that final sample, where v1 reported it (the shipped examples have no such decision).
``remaining_contact_s`` is the pass
end minus the time and ``contact_truncated`` whether the pass is cut by the window stop.

Minimum range. With events on, the smallest range over each pass is found from the range at its
ends, grid times and culmination, and at every root of the range rate between them
(``solve_brackets`` to ``tolerance_s``). With events off it is the smallest sampled range.

Routes. Delays are evaluated once per endpoint pair, in one batch over the route breakpoints that
fall in its passes; a breakpoint within ``tolerance_s`` before a root-found LOS reads the delay at
``end_s - tolerance_s`` (as above). With events off, a breakpoint between grid samples where the
satellite is below the mask (a held pass tail) reads it at the grid sample before it. Satellite positions at the breakpoints are computed in chunks
of at most ``ROUTE_CHUNK_BYTES`` and the routes merged across chunks.

Workers. ``workers > 1`` runs the per-satellite phases in a ``ProcessPoolExecutor`` over
contiguous satellite chunks; each worker rebuilds the catalogs, grid, geometry and evaluator from
the scenario (the catalogs are checked against their SHA-256 again). Results are merged in
satellite order, and every number is computed by the same code from the same inputs, so they are
byte-identical to ``workers=1``.
"""

from __future__ import annotations

import json
import math
import tempfile
from bisect import bisect_left, bisect_right
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass, replace
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, Any, Self

import numpy as np

from openleo.engine import _assembly as assembly
from openleo.engine.acm import acm_on_grid, acm_segments
from openleo.engine.catalogs import MAX_SATELLITES, _rows, load_catalogs, minimum_period_s
from openleo.engine.convergence import compare_metrics, convergence_table, gate
from openleo.engine.evaluator import LinkEvaluator
from openleo.engine.events import find_passes, passes_on_grid, solve_brackets
from openleo.engine.geometry import Geometry
from openleo.engine.integrate import integrate_piecewise, integrate_smooth, measure
from openleo.engine.routing import (
    evaluate_routes,
    link_state_from_pairs,
    route_breakpoints,
    route_metrics,
)
from openleo.engine.statistics import PairResult, pair_metrics, station_metrics
from openleo.engine.timegrid import build_grid, builtin_timescale, refine
from openleo.physics.adaptation import MODCODS
from openleo.results import Results, write_results
from openleo.scenario.explain import limitations
from openleo.scenario.parse import scenario_document

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from numpy.typing import NDArray

    from openleo.engine.acm import ModcodEvent, RateSegment
    from openleo.engine.events import Pass
    from openleo.engine.routing import RouteInterval
    from openleo.engine.timegrid import TimeGrid
    from openleo.scenario.model import CatalogSource, Scenario

PHASES = ("catalog", "geometry", "events", "links", "routes", "convergence", "tables")
DEFAULT_MEMORY_BUDGET_BYTES = 4 * 2**30
# Largest Shannon quadrature piece before adaptive bisection (Task 10 review ruling).
SHANNON_PIECE_S = 10.0
# Satellite positions held at once while routing: one chunk of route breakpoints.
ROUTE_CHUNK_BYTES = 32 * 2**20
# Temporaries of the inter-satellite link test per satellite pair at one breakpoint
# (``routing._isl_edges``: the separation vector, its length, the scaled direction and the
# closest-approach terms, eight float64 values).
ISL_PAIR_BYTES = 8 * 8
# Pair metrics that are extremes, not integrals: reported, not gated.
UNGATED_PAIR_METRICS = frozenset(("max_elevation_deg", "min_range_m"))


def estimate_memory_bytes(
    satellites: int, stations: int, samples: int, *, network: bool = False
) -> int:
    """Upper estimate of the run's large arrays, in bytes (spec section 6.8).

    The base is satellites x samples x 6 float64 (ECEF position and velocity) plus stations x
    satellites x samples x 4 float64 (topocentric elevation, azimuth, range and range rate).
    With a network, routing adds one chunk of satellite positions at route breakpoints,
    c x satellites x 3 float64 with c = max(1, ROUTE_CHUNK_BYTES // (24 x satellites)), and the
    inter-satellite link temporaries of one breakpoint, satellites x (satellites - 1) / 2 x
    ISL_PAIR_BYTES.
    """
    for name, value in (("satellites", satellites), ("stations", stations), ("samples", samples)):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{name}={value!r} must be a non-negative integer")
    total = satellites * samples * 6 * 8 + stations * satellites * samples * 4 * 8
    if network and satellites:
        chunk = max(1, ROUTE_CHUNK_BYTES // (24 * satellites))
        total += chunk * satellites * 3 * 8 + satellites * (satellites - 1) // 2 * ISL_PAIR_BYTES
    return total


# --- per-satellite work ------------------------------------------------------------------------


class _Context:
    """Everything the per-satellite phases read; built once per process."""

    def __init__(self, scenario: Scenario, orbits=None, grid: TimeGrid | None = None) -> None:
        self.scenario = scenario
        self.timescale = builtin_timescale()
        self.grid = build_grid(scenario.time) if grid is None else grid
        self.orbits = (
            load_catalogs(scenario.space_segment, self.timescale) if orbits is None else orbits
        )
        stations = scenario.ground_segment.stations
        self.geometry = Geometry(self.orbits, stations, self.timescale, self.grid.start_utc)
        self.evaluator = LinkEvaluator(scenario, self.geometry)
        default = scenario.ground_segment.default_minimum_elevation_deg
        self.masks = tuple(
            default if s.minimum_elevation_deg is None else s.minimum_elevation_deg
            for s in stations
        )
        self.station_count = len(stations)
        self.events_on = scenario.time.events.enabled
        self.tolerance = scenario.time.events.tolerance_s
        convergence = scenario.time.convergence
        self.refined = (
            refine(self.grid, convergence.refinement_factor) if convergence.mode != "off" else None
        )
        self.adaptation = scenario.adaptation
        self.modcods = {modcod.name: modcod for modcod in MODCODS}

    def label(self, station_index: int, satellite_index: int) -> str:
        name = self.scenario.ground_segment.stations[station_index].name
        norad = self.orbits[satellite_index].satellite.model.satnum
        return f"station {name!r}, NORAD {norad}"


@dataclass(frozen=True, slots=True)
class _PairEvents:
    passes: tuple[Pass, ...]
    segments: tuple[RateSegment, ...]
    modcod_events: tuple[ModcodEvent, ...]
    # Events off: the link arrays at the visible grid samples (they gave the MODCOD inputs).
    link: dict[str, NDArray[np.float64]] | None


@dataclass(frozen=True, slots=True)
class _PairLinks:
    samples: dict[str, Any]
    pass_shannon_bits: tuple[float, ...]
    max_elevation_deg: float
    min_range_m: float


def _geometry_stage(ctx: _Context, satellite: int):
    offsets = ctx.grid.offsets_s
    positions = (
        ctx.geometry.satellite_ecef(satellite, offsets)[0]
        if ctx.scenario.outputs.satellite_positions == "grid"
        else None
    )
    elevations = np.array(
        [ctx.geometry.elevation_deg(i, satellite, offsets) for i in range(ctx.station_count)]
    ).reshape(ctx.station_count, offsets.size)
    return positions, elevations


def _events_stage(ctx: _Context, satellite: int, elevations) -> list[_PairEvents]:
    offsets = ctx.grid.offsets_s
    return [
        _pair_events(ctx, i, satellite, elevations[i], offsets) for i in range(ctx.station_count)
    ]


def _pair_events(ctx: _Context, station: int, satellite: int, elevation, offsets) -> _PairEvents:
    mask = ctx.masks[station]
    try:
        if ctx.events_on:
            passes = find_passes(
                lambda t: ctx.geometry.elevation_deg(station, satellite, t),
                offsets,
                mask,
                ctx.tolerance,
            )
            breakpoints = np.union1d(offsets, [p.culmination_s for p in passes])
            segments, events = acm_segments(
                lambda t: ctx.evaluator.tabulated_available_db(station, satellite, t),
                passes,
                breakpoints,
                ctx.adaptation,
                ctx.tolerance,
            )
            return _PairEvents(passes, segments, events, None)
        visible = np.flatnonzero(elevation >= mask)
        passes = passes_on_grid(elevation, offsets, mask)
        available = np.full(offsets.size, np.nan)
        link = None
        if visible.size:
            link = ctx.evaluator.evaluate(station, satellite, offsets[visible])
            available[visible] = link["esn0_db"] - ctx.adaptation.implementation_margin_db
        segments, events = acm_on_grid(available, offsets, passes, ctx.adaptation)
        return _PairEvents(passes, segments, events, link)
    except ValueError as exc:
        raise ValueError(f"{ctx.label(station, satellite)}: {exc}") from exc


def _links_stage(ctx: _Context, satellite: int, elevations, pairs) -> list[_PairLinks]:
    return [
        _pair_links(ctx, i, satellite, elevations[i], pairs[i]) for i in range(ctx.station_count)
    ]


def _pair_links(ctx: _Context, station: int, satellite: int, elevation, pair: _PairEvents):
    offsets = ctx.grid.offsets_s
    visible = np.flatnonzero(elevation >= ctx.masks[station])
    link = pair.link
    if ctx.events_on and visible.size:
        link = ctx.evaluator.evaluate(station, satellite, offsets[visible])
    if link is None:
        link = {name: np.empty(0) for name in ctx.evaluator.columns}
    samples = _sample_columns(ctx, station, satellite, visible, link, pair)
    try:
        if ctx.events_on:
            shannon = _shannon_on_events(ctx, station, satellite, pair, offsets)
            minimum_range = _min_range(ctx, station, satellite, pair.passes, offsets)
        else:
            shannon = _shannon_on_grid(pair.passes, offsets, visible, link)
            minimum_range = float(np.min(link["range_m"])) if visible.size else math.nan
    except ValueError as exc:
        raise ValueError(f"{ctx.label(station, satellite)}: {exc}") from exc
    highest = max((p.max_elevation_deg for p in pair.passes), default=math.nan)
    return _PairLinks(samples, shannon, highest, minimum_range)


def _refined_stage(ctx: _Context, satellite: int, pairs) -> list:
    """Convergence: per pair, the refined-grid Shannon of each pass (events on) or the whole
    pair recomputed on the refined grid (events off)."""
    offsets = ctx.refined.offsets_s
    result = []
    for station in range(ctx.station_count):
        if ctx.events_on:
            try:
                result.append(_shannon_on_events(ctx, station, satellite, pairs[station], offsets))
            except ValueError as exc:
                raise ValueError(f"{ctx.label(station, satellite)}: {exc}") from exc
            continue
        elevation = ctx.geometry.elevation_deg(station, satellite, offsets)
        events = _pair_events(ctx, station, satellite, elevation, offsets)
        visible = np.flatnonzero(elevation >= ctx.masks[station])
        link = events.link or {"shannon_upper_bound_bps": np.empty(0)}
        shannon = _shannon_on_grid(events.passes, offsets, visible, link)
        result.append(
            PairResult(
                station,
                satellite,
                events.passes,
                events.segments,
                events.modcod_events,
                math.fsum(shannon),
                math.nan,
                math.nan,
            )
        )
    return result


_STAGES = {
    "geometry": _geometry_stage,
    "events": _events_stage,
    "links": _links_stage,
    "refined": _refined_stage,
}
_WORKER: _Context | None = None
_WORKER_ERROR: str | None = None


def _init_worker(scenario: Scenario) -> None:
    """Build the worker's context. A failure is kept and raised by every chunk as ValueError:
    an exception escaping an initializer only breaks the pool (``BrokenProcessPool``) and
    loses its cause."""
    global _WORKER, _WORKER_ERROR
    try:
        _WORKER, _WORKER_ERROR = _Context(scenario), None
    except Exception as exc:  # noqa: BLE001 - reported by _run_chunk with its type
        _WORKER, _WORKER_ERROR = None, f"{type(exc).__name__}: {exc}"


def _run_chunk(stage: str, items: list[tuple]) -> list:
    if _WORKER is None:
        raise ValueError(f"worker process initialisation failed: {_WORKER_ERROR}")
    return [_STAGES[stage](_WORKER, *item) for item in items]


class _Executor:
    """Runs a per-satellite stage in this process or over satellite chunks in a process pool."""

    def __init__(self, context: _Context, workers: int) -> None:
        self.context = context
        self.workers = min(workers, len(context.orbits))
        self.pool = None

    def __enter__(self) -> Self:
        if self.workers > 1:
            self.pool = ProcessPoolExecutor(
                max_workers=self.workers,
                initializer=_init_worker,
                initargs=(self.context.scenario,),
            )
        return self

    def __exit__(self, *exc_info) -> None:
        if self.pool is not None:
            self.pool.shutdown(cancel_futures=True)

    def map(self, stage: str, items: list[tuple]) -> list:
        if self.pool is None:
            return [_STAGES[stage](self.context, *item) for item in items]
        size = max(1, math.ceil(len(items) / (4 * self.workers)))
        chunks = [items[k : k + size] for k in range(0, len(items), size)]
        merged: list = []
        try:
            for part in self.pool.map(_run_chunk, [stage] * len(chunks), chunks):
                merged.extend(part)
        except BrokenProcessPool as exc:
            raise ValueError(
                f"a worker process died during the {stage} phase ({exc}); it may have run out "
                "of memory: retry with fewer workers"
            ) from exc
        return merged


# --- link quantities ---------------------------------------------------------------------------


def _subdivide(breakpoints: NDArray[np.float64], piece_s: float) -> NDArray[np.float64]:
    """The breakpoints with every interval split into equal pieces of at most ``piece_s``."""
    lengths = np.diff(breakpoints)
    counts = np.maximum(1, np.ceil(lengths / piece_s)).astype(np.int64)
    starts = np.repeat(breakpoints[:-1], counts)
    widths = np.repeat(lengths / counts, counts)
    step = np.arange(counts.sum()) - np.repeat(np.cumsum(counts) - counts, counts)
    return np.append(starts + widths * step, breakpoints[-1])


def _evaluable_end(p: Pass, tolerance: float) -> float:
    """The last time of a pass at which the link is evaluated (module docstring)."""
    return p.end_s if p.truncated_end else max(p.start_s, p.end_s - tolerance)


def _shannon_on_events(
    ctx: _Context, station: int, satellite: int, pair: _PairEvents, offsets
) -> tuple[float, ...]:
    if not pair.passes:
        return ()
    candidates = np.unique(
        np.concatenate(
            [
                offsets,
                [p.culmination_s for p in pair.passes],
                [t for s in pair.segments for t in (s.start_s, s.end_s)],
            ]
        )
    )

    def shannon(t):
        return ctx.evaluator.tabulated_shannon_bps(station, satellite, t)

    bits = []
    for p in pair.passes:
        if p.end_s <= p.start_s:
            bits.append(0.0)
            continue
        last = _evaluable_end(p, ctx.tolerance)
        total = 0.0
        if last > p.start_s:
            inside = candidates[(candidates > p.start_s) & (candidates < last)]
            pieces = _subdivide(np.concatenate([[p.start_s], inside, [last]]), SHANNON_PIECE_S)
            total = integrate_smooth(shannon, pieces)
        if last < p.end_s:
            held = float(shannon(np.array([last]))[0]) * (p.end_s - last)
            total = math.fsum([total, held])
        bits.append(total)
    return tuple(bits)


def _shannon_on_grid(passes, offsets, visible, link) -> tuple[float, ...]:
    values = np.full(offsets.size, np.nan)
    values[visible] = link["shannon_upper_bound_bps"]
    bits = []
    for p in passes:
        members = np.flatnonzero((offsets >= p.start_s) & (offsets < p.end_s))
        bits.append(integrate_piecewise(offsets[members], offsets[members + 1], values[members]))
    return tuple(bits)


def _min_range(ctx: _Context, station: int, satellite: int, passes, offsets) -> float:
    if not passes:
        return math.nan
    times, owner = [], []
    for index, p in enumerate(passes):
        inside = offsets[(offsets > p.start_s) & (offsets < p.end_s)]
        pass_times = np.unique(np.concatenate([[p.start_s, p.culmination_s, p.end_s], inside]))
        times.append(pass_times)
        owner.append(np.full(pass_times.size, index))
    times, owner = np.concatenate(times), np.concatenate(owner)
    geometry = ctx.geometry.topocentric(station, satellite, times)
    ranges, rates = geometry.range_m, geometry.range_rate_mps
    closest = float(np.min(ranges))
    turning = np.flatnonzero((owner[:-1] == owner[1:]) & (rates[:-1] < 0.0) & (rates[1:] > 0.0))
    if turning.size:
        roots = solve_brackets(
            lambda t: ctx.geometry.topocentric(station, satellite, t).range_rate_mps,
            times[turning],
            times[turning + 1],
            ctx.tolerance,
            f_lo=rates[turning],
            f_hi=rates[turning + 1],
        )
        closest = min(
            closest, float(np.min(ctx.geometry.topocentric(station, satellite, roots).range_m))
        )
    return closest


def _sample_columns(ctx: _Context, station: int, satellite: int, visible, link, pair):
    offsets = ctx.grid.offsets_s
    times = offsets[visible]
    count = visible.size
    starts = [p.start_s for p in pair.passes]
    segment_starts = [s.start_s for s in pair.segments]
    margin_db = ctx.adaptation.implementation_margin_db
    esn0 = link["esn0_db"]
    modcods, rates, margins = [], np.empty(count), np.empty(count)
    remaining, truncated = np.empty(count), np.zeros(count, dtype=bool)
    for j, t in enumerate(times.tolist()):
        index = bisect_right(starts, t) - 1
        p = pair.passes[index] if index >= 0 else None
        if p is None or t > p.end_s:  # pragma: no cover - visible samples lie in passes
            raise ValueError(f"{ctx.label(station, satellite)}: visible sample {t!r} s in no pass")
        if t < p.end_s:
            k = bisect_right(segment_starts, t) - 1
            segment = pair.segments[k] if k >= 0 and t < pair.segments[k].end_s else None
            name = segment.modcod if segment is not None else None
            rate = segment.rate_bps if segment is not None else 0.0
        else:
            name, rate = _state_at_pass_end(ctx, p, pair.modcod_events, esn0[j] - margin_db)
        required = (ctx.modcods[name] if name else MODCODS[0]).required_esn0_db
        modcods.append(name or "")
        rates[j] = rate
        margins[j] = esn0[j] - margin_db - required
        remaining[j] = p.end_s - t
        truncated[j] = p.truncated_end
    return {
        "station_index": np.full(count, station, dtype=np.int64),
        "satellite_index": np.full(count, satellite, dtype=np.int64),
        "time_index": visible.astype(np.int64),
        "t_offset_s": times,
        **{name: link[name] for name in ctx.evaluator.columns},
        "modcod": modcods,
        "rate_bps": rates,
        "margin_db": margins,
        "remaining_contact_s": remaining,
        "contact_truncated": truncated,
    }


def _state_at_pass_end(ctx: _Context, p: Pass, events, available_db: float):
    """The MODCOD in effect at a pass's end instant, which no rate segment holds."""
    delay = ctx.adaptation.feedback_delay_s
    state = None
    for event in events:
        if p.start_s <= event.decision_s <= p.end_s and event.decision_s + delay < p.end_s:
            state = event.to_modcod
    if state is None:
        return None, 0.0
    modcod = ctx.modcods[state]
    if available_db < modcod.required_esn0_db:  # the mode in effect fails: frames are lost
        return None, 0.0
    return state, ctx.adaptation.symbol_rate_baud * modcod.efficiency_bits_per_symbol


# --- routes ------------------------------------------------------------------------------------


def _route_intervals(ctx: _Context, pairs: dict, offsets) -> tuple[RouteInterval, ...]:
    network = ctx.scenario.network
    names = tuple(s.name for s in ctx.scenario.ground_segment.stations)
    satellites = len(ctx.orbits)
    endpoints = sorted({names.index(n) for p in network.pairs for n in (p.source, p.target)})
    endpoint_pairs = [pairs[(i, s)] for i in endpoints for s in range(satellites)]
    breakpoints = route_breakpoints(offsets, endpoint_pairs, set(endpoints), float(offsets[-1]))
    evaluated = breakpoints[:-1]

    delays: dict[tuple[int, int], dict[float, float]] = {}
    for pair in endpoint_pairs:
        times, queries = [], []
        for p in pair.passes:
            inside = evaluated[(evaluated >= p.start_s) & (evaluated < p.end_s)]
            times.append(inside)
            queries.append(
                np.minimum(inside, _evaluable_end(p, ctx.tolerance))
                if ctx.events_on
                else _held_queries(ctx, pair.station_index, pair.satellite_index, inside, offsets)
            )
        table: dict[float, float] = {}
        if times and sum(t.size for t in times):
            times, queries = np.concatenate(times), np.concatenate(queries)
            unique, inverse = np.unique(queries, return_inverse=True)
            values = ctx.evaluator.evaluate(pair.station_index, pair.satellite_index, unique)
            table = dict(zip(times.tolist(), values["delay_s"][inverse].tolist(), strict=True))
        delays[(pair.station_index, pair.satellite_index)] = table

    def delay_s(station: int, satellite: int, t: float) -> float:
        return delays[(station, satellite)][t]

    def satellite_ecef(times):
        return np.stack(
            [ctx.geometry.satellite_ecef(s, times)[0] for s in range(satellites)], axis=1
        )

    state = link_state_from_pairs(endpoint_pairs, delay_s)
    chunk = max(1, ROUTE_CHUNK_BYTES // (24 * satellites))
    groups: dict[tuple[int, str], list[RouteInterval]] = {}
    for first in range(0, breakpoints.size - 1, chunk):
        part = breakpoints[first : first + chunk + 1]
        for interval in evaluate_routes(network, names, satellite_ecef, state, part, satellites):
            group = groups.setdefault((interval.pair_index, interval.model), [])
            last = group[-1] if group else None
            if last is not None and last.end_s == interval.start_s and _same_route(last, interval):
                group[-1] = replace(last, end_s=interval.end_s)
            else:
                group.append(interval)
    order = {model: index for index, model in enumerate(network.route_models)}
    return tuple(
        interval
        for key in sorted(groups, key=lambda key: (key[0], order[key[1]]))
        for interval in groups[key]
    )


def _held_queries(ctx: _Context, station: int, satellite: int, times, offsets):
    """Events off: where to read the delay at each route breakpoint inside a pass.

    Visibility is held from grid sample to grid sample (v1), so a breakpoint between samples
    (a rate-segment end at a sample plus the feedback delay) can lie in a pass tail where the
    satellite is already below the mask and the link budget is undefined. The delay is read at
    the breakpoint itself where the satellite is at or above the mask, and at the grid sample at
    or before it otherwise (``openleo.replay.source`` applies the same rule).
    """
    sample = offsets[np.searchsorted(offsets, times, side="right") - 1]
    off_grid = np.flatnonzero(times != sample)
    if off_grid.size == 0:
        return times
    elevation = ctx.geometry.elevation_deg(station, satellite, times[off_grid])
    below = off_grid[~(elevation >= ctx.masks[station])]
    queries = times.copy()
    queries[below] = sample[below]
    return queries


def _same_route(a: RouteInterval, b: RouteInterval) -> bool:
    return (a.nodes, a.delay_s, a.bottleneck_bps) == (b.nodes, b.delay_s, b.bottleneck_bps)


# --- metrics -----------------------------------------------------------------------------------


def _metrics(ctx: _Context, pairs: dict, intervals, window_s: float):
    """Pair, station and route metrics of one grid's results."""
    network = ctx.scenario.network
    fixed = network.fixed_capacity_bps if network is not None else None
    satellites = len(ctx.orbits)
    pair_values = {key: pair_metrics(pair) for key, pair in sorted(pairs.items())}
    station_values = [
        station_metrics(i, [pairs[(i, s)] for s in range(satellites)], window_s, fixed)
        for i in range(ctx.station_count)
    ]
    route_values = route_metrics(intervals, window_s) if network is not None else {}
    return pair_values, station_values, route_values


def _gated(ctx: _Context, pair_values, station_values, route_values) -> dict[str, float]:
    names = [s.name for s in ctx.scenario.ground_segment.stations]
    metrics: dict[str, float] = {}
    for (station, satellite), values in pair_values.items():
        norad = ctx.orbits[satellite].satellite.model.satnum
        for key, value in values.items():
            if key not in UNGATED_PAIR_METRICS:
                metrics[f"pair/{names[station]}/{norad}/{key}"] = float(value)
    for station, values in enumerate(station_values):
        for key, value in values.items():
            metrics[f"station/{names[station]}/{key}"] = float(value)
    for (pair_index, model), values in route_values.items():
        pair = ctx.scenario.network.pairs[pair_index]
        for key, value in values.items():
            metrics[f"route/{pair.source}->{pair.target}/{model}/{key}"] = float(value)
    return metrics


# --- run -----------------------------------------------------------------------------------------


def check_step(scenario: Scenario, orbits) -> None:
    """Refuse a step above P_min/8 with events on (spec 6.1); the CLI validate check uses it."""
    period_s = minimum_period_s(orbits)
    limit_s = period_s / 8.0
    step_s = scenario.time.step_s
    if step_s > limit_s:
        raise ValueError(
            f"/time/step_s={step_s!r} exceeds the event-detection limit P_min/8 = {limit_s!r} s, "
            f"where P_min = {period_s!r} s is the shortest orbital period in the catalogs: a "
            "pass could hide between two samples. Use a step of at most that limit, or set "
            "/time/events/enabled to false for v1's sampled semantics"
        )


def _record_counts(catalogs: Sequence[CatalogSource]) -> list[int]:
    """Records in each catalog file, before ``select_norad_ids`` (provenance)."""
    counts = []
    for index, catalog in enumerate(catalogs):
        raw = _verified_bytes(catalog, f"/space_segment/catalogs/{index}")
        counts.append(len(_rows(raw.decode("utf-8"), MAX_SATELLITES)))
    return counts


def _verified_bytes(catalog: CatalogSource, pointer: str) -> bytes:
    try:
        raw = catalog.path.read_bytes()
    except OSError as exc:
        raise ValueError(f"{pointer}: could not read {catalog.path}: {exc}") from exc
    actual = sha256(raw).hexdigest()
    if actual != catalog.sha256:
        raise ValueError(
            f"{pointer}: {catalog.path}: SHA-256 mismatch: expected {catalog.sha256}, got {actual}"
        )
    return raw


def _positive_int(value, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name}={value!r} must be a positive integer")
    return value


def run(
    scenario: Scenario,
    *,
    workers: int = 1,
    memory_budget_bytes: int = DEFAULT_MEMORY_BUDGET_BYTES,
    progress: Callable[[str], None] | None = None,
) -> Results:
    """Simulate a scenario and return its results (module docstring). Errors are ValueError."""
    workers = _positive_int(workers, "workers")
    memory_budget_bytes = _positive_int(memory_budget_bytes, "memory_budget_bytes")
    report = progress if progress is not None else (lambda phase: None)

    report("catalog")
    timescale = builtin_timescale()
    grid = build_grid(scenario.time)
    orbits = load_catalogs(scenario.space_segment, timescale)
    record_counts = _record_counts(scenario.space_segment.catalogs)
    if scenario.time.events.enabled:
        check_step(scenario, orbits)
    stations = scenario.ground_segment.stations
    estimate = estimate_memory_bytes(
        len(orbits), len(stations), grid.offsets_s.size, network=scenario.network is not None
    )
    if estimate > memory_budget_bytes:
        raise ValueError(
            f"memory estimate of {estimate} bytes for {len(orbits)} satellites, {len(stations)} "
            f"stations and {grid.offsets_s.size} samples exceeds the budget of "
            f"{memory_budget_bytes} bytes; shorten the window, coarsen /time/step_s, select "
            "fewer satellites or raise the budget"
        )
    ctx = _Context(scenario, orbits, grid)
    satellites = range(len(orbits))
    offsets = grid.offsets_s
    window_s = float(offsets[-1])

    with _Executor(ctx, workers) as executor:
        report("geometry")
        geometry = executor.map("geometry", [(s,) for s in satellites])
        report("events")
        events = executor.map("events", [(s, geometry[s][1]) for s in satellites])
        report("links")
        links = executor.map("links", [(s, geometry[s][1], events[s]) for s in satellites])
        pairs = {
            (i, s): PairResult(
                i,
                s,
                events[s][i].passes,
                events[s][i].segments,
                events[s][i].modcod_events,
                math.fsum(links[s][i].pass_shannon_bits),
                links[s][i].max_elevation_deg,
                links[s][i].min_range_m,
            )
            for s in satellites
            for i in range(ctx.station_count)
        }
        report("routes")
        intervals = _route_intervals(ctx, pairs, offsets) if scenario.network is not None else ()
        report("convergence")
        settings = scenario.time.convergence
        primary = _metrics(ctx, pairs, intervals, window_s)
        rows = ()
        if settings.mode != "off":
            refined_parts = executor.map("refined", [(s, events[s]) for s in satellites])
            if ctx.events_on:
                refined_pairs = {
                    (i, s): replace(pair, shannon_bits=math.fsum(refined_parts[s][i]))
                    for (i, s), pair in pairs.items()
                }
            else:
                refined_pairs = {
                    (i, s): refined_parts[s][i]
                    for s in satellites
                    for i in range(ctx.station_count)
                }
            refined_intervals = (
                _route_intervals(ctx, refined_pairs, ctx.refined.offsets_s)
                if scenario.network is not None
                else ()
            )
            refined = _metrics(ctx, refined_pairs, refined_intervals, window_s)
            rows = compare_metrics(_gated(ctx, *primary), _gated(ctx, *refined), settings)
        convergence_block, convergence_warnings = gate(rows, settings, scenario.time.step_s)

    report("tables")
    tables = _tables(ctx, geometry, events, links, primary, intervals, rows)
    counts = {kind: 0 for kind in assembly.EVENT_TYPES}
    for kind in tables["events"]["type"]:
        counts[kind] += 1
    metadata = {
        "kind": "openleo.results",
        "schema_version": "2",
        "software": assembly.software(),
        "scenario": assembly.scenario_block(scenario),
        "provenance": assembly.provenance(scenario, orbits, record_counts, grid, timescale),
        "models": assembly.models(scenario, timescale, ctx.events_on, ctx.evaluator.gas_tables),
        "events": {
            "enabled": ctx.events_on,
            "integration": "event_segments" if ctx.events_on else "grid_left_hold",
            "tolerance_s": ctx.tolerance if ctx.events_on else None,
            "counts": counts,
        },
        "convergence": convergence_block,
        "warnings": assembly.warnings(scenario, orbits, grid) + convergence_warnings,
        "limitations": limitations(scenario),
        "summary": _summary(ctx, primary, window_s),
    }
    return Results(assembly.json_safe(metadata), tables)


def _summary(ctx: _Context, primary, window_s: float) -> dict[str, Any]:
    pair_values, station_values, route_values = primary
    names = [s.name for s in ctx.scenario.ground_segment.stations]
    network = ctx.scenario.network
    return {
        "satellite_count": len(ctx.orbits),
        "station_count": ctx.station_count,
        "grid_sample_count": int(ctx.grid.offsets_s.size),
        "step_s": ctx.scenario.time.step_s,
        "window_s": window_s,
        "pass_count": sum(int(v["pass_count"]) for v in pair_values.values()),
        "visible_pair_duration_s": math.fsum(v["visible_duration_s"] for v in pair_values.values()),
        "integrated_rate_bits": math.fsum(v["integrated_rate_bits"] for v in pair_values.values()),
        "integrated_shannon_upper_bound_bits": math.fsum(
            v["integrated_shannon_upper_bound_bits"] for v in pair_values.values()
        ),
        "stations": {names[i]: values for i, values in enumerate(station_values)},
        "routes": {
            f"{network.pairs[index].source}->{network.pairs[index].target}/{model}": values
            for (index, model), values in route_values.items()
        },
    }


def _tables(ctx: _Context, geometry, events, links, primary, intervals, rows) -> dict:
    scenario = ctx.scenario
    satellites = range(len(ctx.orbits))
    stations = range(ctx.station_count)
    pair_values, station_values, route_values = primary
    tables = {
        "satellites": assembly.satellites_table(ctx.orbits, ctx.grid),
        "stations": assembly.stations_table(scenario, ctx.masks, ctx.geometry.station_ecef_m),
    }
    if scenario.outputs.satellite_positions == "grid":
        tables["positions"] = assembly.positions_table([g[0] for g in geometry], ctx.grid)
    tables["samples"] = assembly.samples_table(
        ctx.evaluator.columns, [links[s][i].samples for i in stations for s in satellites]
    )
    event_rows, pass_rows, segment_rows = [], [], []
    for i in stations:
        for s in satellites:
            pair, pair_links = events[s][i], links[s][i]
            event_rows += _event_rows(ctx, i, s, pair)
            pass_rows += _pass_rows(i, s, pair, pair_links)
            segment_rows += [
                {
                    "station_index": i,
                    "satellite_index": s,
                    "start_offset_s": seg.start_s,
                    "end_offset_s": seg.end_s,
                    "modcod": seg.modcod or "",
                    "rate_bps": seg.rate_bps,
                }
                for seg in pair.segments
            ]
    tables["events"] = assembly.events_table(event_rows)
    tables["passes"] = assembly.passes_table(pass_rows)
    keys = list(next(iter(pair_values.values())))
    tables["pair_metrics"] = assembly.pair_metrics_table(
        [
            {
                "station_index": i,
                "satellite_index": s,
                "norad_id": ctx.orbits[s].satellite.model.satnum,
                **values,
            }
            for (i, s), values in pair_values.items()
        ],
        keys,
    )
    names = [st.name for st in scenario.ground_segment.stations]
    tables["station_metrics"] = assembly.station_metrics_table(
        [{"station_index": i, "name": names[i], **v} for i, v in enumerate(station_values)],
        list(station_values[0]),
    )
    tables["rate_segments"] = assembly.rate_segments_table(segment_rows)
    if scenario.network is not None:
        tables["routes"] = assembly.routes_table(intervals, scenario)
        tables["route_metrics"] = assembly.route_metrics_table(route_values, scenario)
    tables["convergence"] = convergence_table(rows)
    return tables


# Same-instant events in causal order: AOS, the MODCOD decision and its effect, culmination, LOS.
_EVENT_ORDER = {kind: rank for rank, kind in enumerate(assembly.EVENT_TYPES)}


def _event_rows(ctx: _Context, station: int, satellite: int, pair: _PairEvents) -> list[tuple]:
    window_s = float(ctx.grid.offsets_s[-1])
    delay = ctx.adaptation.feedback_delay_s
    nan = math.nan
    rows = []
    for p in pair.passes:
        if not p.truncated_start:
            rows.append((station, satellite, p.start_s, "aos", "", "", nan, nan))
        edge = (p.truncated_start and p.culmination_s == 0.0) or (
            p.truncated_end and p.culmination_s == window_s
        )
        if not edge:
            rows.append(
                (
                    station,
                    satellite,
                    p.culmination_s,
                    "culmination",
                    "",
                    "",
                    p.max_elevation_deg,
                    nan,
                )
            )
        if not p.truncated_end:
            rows.append((station, satellite, p.end_s, "los", "", "", nan, nan))
    starts = [p.start_s for p in pair.passes]
    for event in pair.modcod_events:
        p = pair.passes[bisect_right(starts, event.decision_s) - 1]
        before, after = event.from_modcod or "", event.to_modcod or ""
        rows.append(
            (
                station,
                satellite,
                event.decision_s,
                "modcod_decision",
                before,
                after,
                nan,
                event.decision_s,
            )
        )
        if event.decision_s + delay < p.end_s:  # acm: at or after the pass end is dropped
            rows.append(
                (
                    station,
                    satellite,
                    event.effective_s,
                    "modcod_effective",
                    before,
                    after,
                    nan,
                    event.decision_s,
                )
            )
    rows.sort(key=lambda row: (row[2], _EVENT_ORDER[row[3]]))
    return rows


def _pass_rows(station: int, satellite: int, pair: _PairEvents, links: _PairLinks) -> list[dict]:
    rows = []
    segment_starts = [s.start_s for s in pair.segments]
    for index, p in enumerate(pair.passes):
        first = bisect_left(segment_starts, p.start_s)
        last = bisect_left(segment_starts, p.end_s)
        segments = pair.segments[first:last]
        rows.append(
            {
                "station_index": station,
                "satellite_index": satellite,
                "pass_index": index,
                "aos_offset_s": p.start_s,
                "los_offset_s": p.end_s,
                "culmination_offset_s": p.culmination_s,
                "truncated_start": p.truncated_start,
                "truncated_end": p.truncated_end,
                "duration_s": p.end_s - p.start_s,
                "rf_usable_duration_s": measure(
                    (s.start_s, s.end_s) for s in segments if s.rate_bps > 0.0
                ),
                "integrated_rate_bits": integrate_piecewise(
                    [s.start_s for s in segments],
                    [s.end_s for s in segments],
                    [s.rate_bps for s in segments],
                ),
                "integrated_shannon_upper_bound_bits": links.pass_shannon_bits[index],
                "max_elevation_deg": p.max_elevation_deg,
            }
        )
    return rows


# --- archival ------------------------------------------------------------------------------------


_archive_names = assembly.archive_names


def save_run(
    scenario: Scenario,
    results: Results,
    directory: Path,
    formats: tuple[str, ...] | None = None,
) -> Path:
    """Publish ``results`` at ``directory`` with the inputs that reproduce them; return it.

    The run directory gets ``inputs/scenario.json`` (the normalised scenario document, its
    catalog paths rewritten to ``catalogs/<basename>``, so ``load_scenario`` reads it in place)
    and ``inputs/catalogs/<basename>``, the catalog bytes verbatim, checked against their
    SHA-256 first. ``formats`` defaults to the scenario's ``outputs.formats``.
    """
    formats = tuple(scenario.outputs.formats) if formats is None else tuple(formats)
    document = scenario_document(scenario)
    names = _archive_names(scenario.space_segment.catalogs)
    with tempfile.TemporaryDirectory(prefix="openleo-inputs-") as temporary:
        staging = Path(temporary)
        (staging / "catalogs").mkdir()
        inputs = [(staging / "scenario.json", "scenario.json")]
        for index, (catalog, name) in enumerate(
            zip(scenario.space_segment.catalogs, names, strict=True)
        ):
            raw = _verified_bytes(catalog, f"/space_segment/catalogs/{index}")
            copy = staging / "catalogs" / f"{index}.bin"
            copy.write_bytes(raw)
            inputs.append((copy, f"catalogs/{name}"))
            document["space_segment"]["catalogs"][index]["path"] = f"catalogs/{name}"
        text = json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        (staging / "scenario.json").write_bytes(text.encode("utf-8"))
        return write_results(results, Path(directory), formats, inputs)
