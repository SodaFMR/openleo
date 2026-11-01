"""v1's DVB-S2 MODCOD rule in continuous time (design spec section 6.3).

``acm_segments`` finds the exact times the adaptation state changes inside each pass, so the
data rate is a piecewise-constant function whose integral is exact. ``acm_on_grid`` applies the
same rule at grid samples only (events off), reproducing v1's per-sample selection.

The rule is v1's ``select_modcod``, called as it is; nothing here re-implements it. It reads the
available Es/N0 A(t) = Es/N0(t) - implementation margin only through comparisons ``A >= level``
with the levels returned by ``threshold_levels_db``: every required Es/N0 and every required
Es/N0 plus hysteresis. So the state can change only where A crosses a level, and between two
crossings it is constant (the rule is idempotent: applying it again to the same A keeps the
state). The engine therefore:

1. Evaluates A at the pass breakpoints (grid times and culminations inside the pass, plus its
   ends) for every pass of the pair in one call.
2. Guards monotonicity (spec section 6.7) by evaluating every inter-breakpoint midpoint. The
   ACM analogue of the toward-the-mask rule of ``openleo.engine.events`` is exact: an interval
   is suspect only when the midpoint's level count (how many levels A is at or above) lies
   outside the range of its endpoints' counts, because only then does an excursion cross a
   level that monotone interpolation says is not crossed. Such intervals are bisected, up to
   ``MAX_BISECTION_LEVELS`` levels; a deviation that crosses no level is harmless and ignored.
   Persistent non-monotonicity raises ValueError. Midpoints become breakpoints.
3. Refines a local maximum (minimum) of A at a breakpoint by golden-section search when a level
   lies above (at or below) its value by at most ``PEAK_BAND_DB``. A's maximum sits a fraction
   of a second from the elevation culmination (on Iridium about 0.05-0.5 s, A exceeding its
   culmination value by about 1e-5 dB), and the hysteresis makes the state after a peak depend
   on whether the peak reached a level: an upgrade at the top is kept until A falls below the
   new mode's own requirement. The band is three orders of magnitude above that excess, so it
   costs nothing on ordinary passes.
4. Root-finds every level crossing of every interval with ``solve_brackets`` (one call per
   iteration for all brackets of all passes) and walks the crossings in time order, applying
   ``select_modcod`` at each. Crossings of levels the current state does not exit on leave it
   unchanged, so this is the walk of spec section 6.3 (exit levels req(m) down, req(m+1) +
   hysteresis up, req(MODCODS[0]) when unlocked) with every exit found in one batch. Decisions
   at the same instant merge; an instant that changes nothing (A touching req(MODCODS[0]) at a
   peak) records no event, but an instant that does matter (A touching an upgrade level at a
   peak, after which the hysteresis keeps the upgrade) is applied.

Times. A crossing time is the start of the run that follows the crossing, within
``tolerance_s`` of the true crossing (``solve_brackets``); a level met exactly (``A == level``)
counts as reached, v1's ``>=``. The link evaluator refuses times below the mask, and a
root-found LOS lies on the invisible side, so A is evaluated on [start_s, end_s - tolerance_s]
for a pass with a root-found end (``tolerance_s`` must be at least the tolerance the passes
were found with; then end_s - tolerance_s is on the visible side) and on [start_s, end_s] for
a pass truncated at the window end, which is visible there. The state at the last evaluated
time is held to end_s: a crossing in the last ``tolerance_s`` before LOS is not resolved. A pass
shorter than ``tolerance_s`` is evaluated at its start only.

Feedback delay d (``Adaptation.feedback_delay_s``), per decision at t_c:

- an acquisition or upgrade takes effect at t_c + d; the old rate (0 when unlocked) runs until
  then;
- a downgrade or loss takes effect at t_c + d; until then the mode in effect keeps running,
  but its rate is 0 while A is below its requirement (its frames are lost), which for A
  monotone across the window is all of [t_c, t_c + d);
- a decision whose effective time is at or after the pass end is dropped: the mode in effect
  stays (or the downgrade's zero rate runs) to the end, and the event records
  ``effective_s = end_s``.

Overlapping decisions compose: the mode in effect is the last decision whose effective time
has passed (the transmitter applies each decision d after it is made, and none is revoked), and
the rate is 0 exactly while A is below the requirement of the mode in effect. That can only
happen within d after a downgrade or loss, so only those windows are checked, against the A
crossings already root-found. For example, an upgrade decided near a peak still arrives d
later: if a downgrade was decided in between, the old mode keeps running until the upgrade
arrives, and the upgraded mode then has rate 0 until the downgrade arrives. With d = 0 this is
v1's rule in continuous time. Losing visibility resets the state; the reset at LOS is not an
event.

Segments tile every pass exactly: [start_s, end_s) is cut at decisions, effective times and,
within d after a downgrade or loss, the crossings of A; equal neighbours are merged, and a zero-duration pass has no segment. A segment's rate is
``symbol_rate_baud * efficiency_bits_per_symbol`` (v1), or 0.0 with ``modcod=None`` when no mode
is in effect or the mode in effect is below its requirement.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, replace
from itertools import pairwise
from math import isfinite
from typing import TYPE_CHECKING

import numpy as np

from openleo.engine.events import (
    MAX_BISECTION_LEVELS,
    Pass,
    _evaluate,
    _golden,
    _grid,
    _tolerance,
    solve_brackets,
)
from openleo.physics.adaptation import MODCODS, select_modcod

if TYPE_CHECKING:
    from collections.abc import Callable

    from numpy.typing import ArrayLike, NDArray

    from openleo.physics.adaptation import Modcod
    from openleo.scenario.model import Adaptation

PEAK_BAND_DB = 0.01


@dataclass(frozen=True, slots=True)
class RateSegment:
    """A constant data rate on [start_s, end_s), seconds after the window start."""

    start_s: float
    end_s: float
    modcod: str | None
    rate_bps: float


@dataclass(frozen=True, slots=True)
class ModcodEvent:
    """An adaptation decision at ``decision_s`` that takes effect at ``effective_s``.

    ``from_modcod`` and ``to_modcod`` are the decided states (None: no lock). A decision dropped
    at the pass end has ``effective_s`` equal to the pass end.
    """

    decision_s: float
    effective_s: float
    from_modcod: str | None
    to_modcod: str | None


def threshold_levels_db(adaptation: Adaptation) -> NDArray[np.float64]:
    """The sorted distinct levels v1's rule compares A with: req(m) and req(m) + hysteresis."""
    return _Rule(adaptation).levels


def acm_segments(
    available_db: Callable[[NDArray[np.float64]], ArrayLike],
    passes: tuple[Pass, ...],
    breakpoints_s: ArrayLike,
    adaptation: Adaptation,
    tolerance_s: float,
) -> tuple[tuple[RateSegment, ...], tuple[ModcodEvent, ...]]:
    """Exact rate segments and MODCOD events of one pair (module docstring).

    ``available_db`` maps offsets in seconds to A = Es/N0 - implementation margin in dB (the
    link evaluator's ``available_db``) and is called only at times inside the passes.
    ``passes`` are the pair's passes in time order; ``breakpoints_s`` its breakpoints (grid
    times and culminations); those outside a pass are ignored.
    """
    rule = _Rule(adaptation)
    tolerance = _tolerance(tolerance_s)
    passes = _passes(passes)
    breakpoints = np.asarray(breakpoints_s, dtype=np.float64)
    if breakpoints.ndim != 1 or not np.isfinite(breakpoints).all():
        raise ValueError("breakpoints_s must be a one-dimensional array of finite times")
    if not passes:
        return (), ()

    times, owner = [], []
    for index, p in enumerate(passes):
        last = p.end_s if p.truncated_end else max(p.start_s, p.end_s - tolerance)
        inside = breakpoints[(breakpoints > p.start_s) & (breakpoints < last)]
        pass_times = np.unique(np.concatenate([[p.start_s], inside, [last]]))
        times.append(pass_times)
        owner.append(np.full(pass_times.size, index))
    times, owner = np.concatenate(times), np.concatenate(owner)
    values = _evaluate(available_db, times)
    times, values, owner = _guarded(available_db, rule, times, values, owner)
    times, values, owner = _refined(available_db, rule, times, values, owner, tolerance)
    crossings = _crossings(available_db, rule, times, values, owner, tolerance)

    segments, events = [], []
    for index, p in enumerate(passes):
        first = int(np.searchsorted(owner, index))
        levels_reached = [(p.start_s, int(rule.count(values[first])))]
        levels_reached += crossings.get(index, [])
        state = rule.select(levels_reached[0][1], None)
        decisions = [(p.start_s, None, state)]
        for t, count in levels_reached[1:]:
            new = rule.select(count, state)
            decisions.append((t, state, new))
            state = new
        pass_segments, pass_events = _schedule(p, _merged(decisions), levels_reached, rule)
        segments += pass_segments
        events += pass_events
    return tuple(segments), tuple(events)


def acm_on_grid(
    available_db_values: ArrayLike,
    grid_s: ArrayLike,
    passes: tuple[Pass, ...],
    adaptation: Adaptation,
) -> tuple[tuple[RateSegment, ...], tuple[ModcodEvent, ...]]:
    """v1's per-sample selection, left-held between samples (events off).

    In each pass, ``select_modcod`` is applied to the samples t_k with start_s <= t_k < end_s,
    and to t_k = end_s when the pass is truncated at the window end or has zero duration, in
    order, from no lock at the pass start, exactly as v1 walked its visible samples. A decision
    at t_k holds over [t_k, t_k+1) clipped to the pass, subject to the feedback delay as in
    ``acm_segments``. ``available_db_values`` holds A at every grid sample; samples outside the
    passes are never read and may be NaN.
    """
    rule = _Rule(adaptation)
    grid = _grid(grid_s)
    values = np.asarray(available_db_values, dtype=np.float64)
    if values.shape != grid.shape:
        raise ValueError(
            f"available_db_values has shape {values.shape}; the grid has shape {grid.shape}"
        )
    segments, events = [], []
    for p in _passes(passes):
        last = (grid == p.end_s) & (p.truncated_end or p.start_s == p.end_s)
        members = np.flatnonzero((grid >= p.start_s) & ((grid < p.end_s) | last))
        if not np.isfinite(values[members]).all():
            raise ValueError(
                f"available_db_values must be finite at the samples of the pass starting at "
                f"{p.start_s!r} s"
            )
        state, decisions = None, []
        for k in members.tolist():
            new = select_modcod(float(values[k]), rule.config, state)
            if new is not state:
                decisions.append((float(grid[k]), state, new))
                state = new
        # A is held from each sample to the next, so the levels it reaches are held too.
        levels_reached = list(
            zip(grid[members].tolist(), rule.count(values[members]).tolist(), strict=True)
        )
        pass_segments, pass_events = _schedule(p, decisions, levels_reached, rule)
        segments += pass_segments
        events += pass_events
    return tuple(segments), tuple(events)


class _Rule:
    """v1's selection with the margin already removed from A, its levels, rates and delay."""

    def __init__(self, adaptation: Adaptation) -> None:
        config = adaptation.kernel()  # validates the v1 parameters
        delay = adaptation.feedback_delay_s
        if isinstance(delay, bool) or not isinstance(delay, int | float) or not isfinite(delay):
            raise ValueError(f"adaptation.feedback_delay_s must be finite, got {delay!r}")
        if delay < 0.0:
            raise ValueError(f"adaptation.feedback_delay_s must be >= 0, got {delay!r}")
        self.delay_s = float(delay)
        # available_db is Es/N0 - margin already; select_modcod then subtracts 0.0, which
        # leaves every value bit-identical to v1's own available_db.
        self.config = replace(config, implementation_margin_db=0.0)
        # Exactly the sums select_modcod compares with (req + 0.0 is req).
        self.levels = np.unique(
            [m.required_esn0_db for m in MODCODS]
            + [m.required_esn0_db + config.hysteresis_db for m in MODCODS]
        )
        self.symbol_rate_baud = config.symbol_rate_baud

    def count(self, values):
        """How many levels each value is at or above (v1's >=)."""
        return np.searchsorted(self.levels, values, side="right")

    def select(self, count: int, previous: Modcod | None) -> Modcod | None:
        """select_modcod for any A at or above exactly the lowest ``count`` levels.

        The rule depends on A only through ``A >= level`` for these levels, so the highest level
        reached (or a value below them all) stands for every such A.
        """
        count = int(count)
        value = self.levels[count - 1] if count else self.levels[0] - 1.0
        return select_modcod(float(value), self.config, previous)

    def meets(self, modcod: Modcod, count: int) -> bool:
        """Whether an A at or above exactly the lowest ``count`` levels reaches req(modcod)."""
        return count > 0 and self.levels[count - 1] >= modcod.required_esn0_db

    def rate_bps(self, modcod: Modcod | None) -> float:
        return self.symbol_rate_baud * modcod.efficiency_bits_per_symbol if modcod else 0.0


def _guarded(f, rule, times, values, owner):
    """Spec section 6.7 for A: bisect intervals whose midpoint crosses a level unseen."""
    same = owner[:-1] == owner[1:]
    a, b = times[:-1][same], times[1:][same]
    count_a, count_b = rule.count(values[:-1][same]), rule.count(values[1:][same])
    pass_of = owner[:-1][same]
    added_t, added_v, added_o = [], [], []
    for level in range(MAX_BISECTION_LEVELS + 1):
        if a.size == 0:
            break
        middle = a + 0.5 * (b - a)
        fm = _evaluate(f, middle)
        added_t.append(middle)
        added_v.append(fm)
        added_o.append(pass_of)
        count_m = rule.count(fm)
        bad = (count_m < np.minimum(count_a, count_b)) | (count_m > np.maximum(count_a, count_b))
        if not bad.any():
            break
        if level == MAX_BISECTION_LEVELS:
            first = int(np.argmax(bad))
            raise ValueError(
                f"non-monotone available Es/N0 between {a[first]:.6f} s and {b[first]:.6f} s "
                f"after the window start: the midpoint value {float(fm[first])!r} dB crosses a "
                f"MODCOD threshold that neither end reaches after {MAX_BISECTION_LEVELS} "
                "bisections; A is not monotone between breakpoints"
            )
        a, b = np.concatenate([a[bad], middle[bad]]), np.concatenate([middle[bad], b[bad]])
        count_a = np.concatenate([count_a[bad], count_m[bad]])
        count_b = np.concatenate([count_m[bad], count_b[bad]])
        pass_of = np.concatenate([pass_of[bad], pass_of[bad]])
    if not added_t:
        return times, values, owner
    return _merge(
        times,
        values,
        owner,
        np.concatenate(added_t),
        np.concatenate(added_v),
        np.concatenate(added_o),
    )


def _refined(f, rule, times, values, owner, tolerance):
    """Golden-section refinement of interior extrema that lie within PEAK_BAND_DB of a level."""
    if times.size < 3:
        return times, values, owner
    left, centre, right = values[:-2], values[1:-1], values[2:]
    interior = (owner[:-2] == owner[1:-1]) & (owner[1:-1] == owner[2:])
    count = rule.count(centre)
    above = rule.levels[np.minimum(count, rule.levels.size - 1)]
    below = rule.levels[np.maximum(count - 1, 0)]
    maxima = (
        interior
        & (centre > left)
        & (centre >= right)
        & (count < rule.levels.size)
        & (above - centre <= PEAK_BAND_DB)
    )
    minima = (
        interior
        & (centre < left)
        & (centre <= right)
        & (count > 0)
        & (centre - below <= PEAK_BAND_DB)
    )
    centres = np.concatenate([np.flatnonzero(maxima), np.flatnonzero(minima)]) + 1
    if centres.size == 0:
        return times, values, owner
    sense = np.concatenate([np.ones(maxima.sum()), -np.ones(minima.sum())])
    where, value = _golden(f, times[centres - 1], times[centres + 1], sense, tolerance)
    better = sense * value > sense * values[centres]
    return _merge(times, values, owner, where[better], value[better], owner[centres][better])


def _crossings(f, rule, times, values, owner, tolerance):
    """Every level crossing, root-found together: {pass index: [(time, count after), ...]}."""
    intervals = np.flatnonzero(owner[:-1] == owner[1:])
    count_a, count_b = rule.count(values[intervals]), rule.count(values[intervals + 1])
    crossed = np.abs(count_b - count_a)
    interval = np.repeat(intervals, crossed)
    if interval.size == 0:
        return {}
    # The j-th crossing of an interval, in the order A meets the levels.
    step = np.arange(interval.size) - np.repeat(np.cumsum(crossed) - crossed, crossed)
    rising = np.repeat(count_b > count_a, crossed)
    start = np.repeat(count_a, crossed)
    level = np.where(rising, start + step, start - 1 - step)
    # The ends' values are those the levels were counted from: passing them avoids a second
    # evaluation and keeps every bracket bracketed even if the provider rounds differently in
    # another batch.
    roots = solve_brackets(
        f,
        times[interval],
        times[interval + 1],
        tolerance,
        levels=rule.levels[level],
        f_lo=values[interval],
        f_hi=values[interval + 1],
    )
    # Each root is within tolerance of its crossing; keep their order within an interval
    # consistent with the order A meets the levels (intervals are already in time order).
    roots = np.maximum.accumulate(roots)
    after = np.where(rising, level + 1, level)
    crossings: dict[int, list[tuple[float, int]]] = {}
    for t, count, index in zip(
        roots.tolist(), after.tolist(), owner[interval].tolist(), strict=True
    ):
        crossings.setdefault(index, []).append((t, count))
    return crossings


def _merge(times, values, owner, extra_t, extra_v, extra_o):
    t = np.concatenate([times, extra_t])
    v = np.concatenate([values, extra_v])
    o = np.concatenate([owner, extra_o])
    order = np.lexsort((t, o))
    t, v, o = t[order], v[order], o[order]
    keep = np.concatenate([[True], (np.diff(t) > 0.0) | (np.diff(o) != 0)])
    return t[keep], v[keep], o[keep]


def _merged(decisions):
    """Changes of state only, decisions at one instant merged into one."""
    merged = []
    for t, old, new in decisions:
        if merged and merged[-1][0] == t:
            old = merged.pop()[1]
        if new is not old:
            merged.append((t, old, new))
    return merged


def _schedule(p: Pass, decisions, levels_reached, rule: _Rule):
    """Rate segments and events of one pass from its decisions (module docstring).

    ``levels_reached`` is [(time, count), ...] in time order from the pass start: from each time
    on, A is at or above exactly the lowest ``count`` levels, until the next entry.
    """
    end, delay = p.end_s, rule.delay_s
    events, switches, blackouts = [], [], []
    for t, old, new in decisions:
        effective = t + delay
        events.append(ModcodEvent(t, min(end, effective), _name(old), _name(new)))
        if effective < end:
            switches.append((effective, new))
        downgrade = old is not None and (new is None or new.required_esn0_db < old.required_esn0_db)
        if downgrade and delay > 0.0:
            blackouts.append((t, min(effective, end)))
    reached_t = [t for t, _ in levels_reached]
    cuts = sorted(
        {p.start_s, end}
        | {t for t, _ in switches}
        | {t for blackout in blackouts for t in blackout}
        # Inside a downgrade's delay, the mode in effect fails or decodes as A crosses its level.
        | {t for t in reached_t if any(a < t < b for a, b in blackouts)}
    )
    segments: list[RateSegment] = []
    for start, stop in pairwise(cuts):
        modcod = next((new for t, new in reversed(switches) if t <= start), None)
        if (
            modcod is not None
            and any(a <= start < b for a, b in blackouts)
            and not rule.meets(modcod, levels_reached[bisect_right(reached_t, start) - 1][1])
        ):
            modcod = None  # the mode in effect is below its threshold: its frames are lost
        rate = rule.rate_bps(modcod)
        if segments and segments[-1].modcod == _name(modcod) and segments[-1].rate_bps == rate:
            segments[-1] = replace(segments[-1], end_s=stop)
        else:
            segments.append(RateSegment(start, stop, _name(modcod), rate))
    return segments, events


def _name(modcod: Modcod | None) -> str | None:
    return modcod.name if modcod else None


def _passes(passes) -> tuple[Pass, ...]:
    passes = tuple(passes)
    for index, p in enumerate(passes):
        if not isinstance(p, Pass):
            raise ValueError(f"passes[{index}] must be a Pass, got {p!r}")  # noqa: TRY004
        if not (isfinite(p.start_s) and isfinite(p.end_s)):
            raise ValueError(f"passes[{index}] must have finite start_s and end_s")
        if not p.start_s <= p.end_s:
            raise ValueError(f"passes[{index}] needs start_s <= end_s, got {p!r}")
        if index and p.start_s < passes[index - 1].end_s:
            raise ValueError(
                f"passes[{index}] starts at {p.start_s!r} s, before passes[{index - 1}] ends: "
                "passes must be in time order and must not overlap"
            )
    return passes
