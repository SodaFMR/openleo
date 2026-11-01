"""Exact visibility events: AOS, LOS and culminations (design spec sections 6.1, 6.2 and 6.7).

``find_passes`` turns an elevation provider into passes, the maximal intervals where the
elevation is at or above the mask, without the sampling artefacts of a fixed grid:

1. The provider is evaluated on the grid.
2. Discrete local extrema of the samples are refined by golden-section search on
   [t_{k-1}, t_{k+1}] clipped to the window (``maximize_brackets``). Window edges are candidate
   extrema too. Maxima are culminations; minima are refined as well, at no extra provider
   calls, so that the elevation is monotone between consecutive breakpoints at troughs too.
3. The guard of spec section 6.7 evaluates every inter-breakpoint interval at its midpoint.
   A midpoint outside the endpoint range by more than ``MONOTONICITY_SLACK`` relative (to the
   larger endpoint magnitude, and at least 1 degree) *toward the mask* bisects the interval, up
   to ``MAX_BISECTION_LEVELS`` levels; such non-monotonicity that survives them raises
   ValueError. Toward the mask means below the endpoint range when either endpoint is visible,
   or above it when either endpoint is not. A deviation away from the mask cannot change
   visibility, so it is ignored: a refined culmination is only within ``tolerance_s`` of the
   true peak, and the interval next to it may hold the peak itself, a harmless excursion above
   both (visible) endpoints that coarse tolerances would otherwise report as non-monotone.
4. Every interval across which ``e >= mask`` changes is root-found (``solve_brackets``).

Breakpoints are the grid, the refined extrema and the guard's midpoints. A pass whose peak
lies between two samples is found through its refined culmination when the step is at most
P/8 (spec section 6.1; the parser enforces that bound).

Every step evaluates the provider once per iteration for all brackets of a pair, because a
geometry call costs about the same for one time as for thousands.

Accuracy. Crossings are within ``tolerance_s`` of the root of the provider as given; geometry
rounds times to the microsecond, so 1e-6 s is the useful floor. Culminations come from function
values alone, so their location is limited by value noise: near a maximum e(t) ≈ e_max −
k (t − t_max)², and a value noise ε hides location errors up to sqrt(ε / k). For float64
elevations that floor is of order 1e-5 s (up to about 1e-4 s), so culmination times are
accurate to max(``tolerance_s``, the value-noise floor), as amended in spec section 6.1, not to
``tolerance_s`` alone; ``max_elevation_deg`` is affected only to second order (by about ε).
"""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite, sqrt
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from collections.abc import Callable

    from numpy.typing import ArrayLike, NDArray

    Provider = Callable[[NDArray[np.float64]], ArrayLike]

MAX_ITERATIONS = 100
MAX_BISECTION_LEVELS = 6
MONOTONICITY_SLACK = 1e-9
_GOLDEN = (sqrt(5.0) - 1.0) / 2.0


@dataclass(frozen=True, slots=True)
class Pass:
    """A visibility interval [start_s, end_s) in seconds after the window start.

    ``start_s`` is the root-found AOS, or 0.0 when the satellite is visible at the window
    start (``truncated_start``); ``end_s`` is the root-found LOS, or the window length T when it
    is visible at the window end (``truncated_end``). A root-found AOS is on the visible side of
    the crossing and a root-found LOS on the invisible side, each within ``tolerance_s``.

    ``culmination_s`` and ``max_elevation_deg`` locate the highest elevation inside the window.
    For a truncated pass still rising at T, or already falling at 0, that is the window edge
    (``culmination_s`` = T or 0.0) and its elevation, not the culmination outside the window.

    A pass may have zero duration: visibility only at the single instant T, or an elevation that
    touches the mask exactly at one breakpoint.
    """

    start_s: float
    end_s: float
    culmination_s: float
    max_elevation_deg: float
    truncated_start: bool
    truncated_end: bool


def solve_brackets(
    f: Provider, lo: ArrayLike, hi: ArrayLike, tolerance_s: float
) -> NDArray[np.float64]:
    """Roots of ``f`` in every bracket [lo, hi], all brackets solved together.

    Each bracket needs ``f(lo)`` and ``f(hi)`` of opposite sign, or a zero at one end, which is
    then returned (``lo`` if both are zero). Otherwise the solver is Illinois regula falsi,
    safeguarded by bisection when two iterations fail to halve a bracket, with every trial point
    at least ``tolerance_s / 2`` inside it. It stops when the bracket is no wider than
    ``tolerance_s`` and returns its ``hi`` end, whose ``f`` has the sign of the original
    ``f(hi)``: the returned time is the start of the run that follows the crossing. An exact zero
    met on the way is returned as it is. A bracket that floating point cannot split further
    also stops. One call of ``f`` per iteration covers every unfinished bracket; more than
    ``MAX_ITERATIONS`` iterations raise ValueError.
    """
    lo, hi = _brackets(lo, hi)
    tolerance = _tolerance(tolerance_s)
    if lo.size == 0:
        return np.empty(0)
    values = _evaluate(f, np.concatenate([lo, hi]))
    return _solve(f, lo, hi, values[: lo.size], values[lo.size :], tolerance)


def maximize_brackets(
    f: Provider, lo: ArrayLike, hi: ArrayLike, tolerance_s: float
) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    """Maxima of ``f`` on every bracket [lo, hi] by golden-section search, all brackets together.

    Returns the best evaluated point of each bracket and its value, once the bracket is no
    wider than ``tolerance_s``. For ``f`` unimodal on a bracket the point is within
    ``tolerance_s`` of the maximiser, up to the value-noise floor (module docstring); for a
    monotone ``f`` it is within ``tolerance_s`` of the higher end, which is never evaluated.
    One call of ``f`` per iteration covers every unfinished bracket.
    """
    lo, hi = _brackets(lo, hi)
    tolerance = _tolerance(tolerance_s)
    return _golden(f, lo, hi, np.ones(lo.size), tolerance)


def find_passes(
    elevation: Provider, grid_s: ArrayLike, mask_deg: float, tolerance_s: float
) -> tuple[Pass, ...]:
    """Passes of one pair over a window, with root-found AOS and LOS (module docstring).

    ``elevation`` maps offsets in seconds after the window start to elevations in degrees and
    is never evaluated outside [0, T]. ``grid_s`` is the sample grid: it starts at 0 and ends at
    the window length T (``openleo.engine.timegrid.TimeGrid.offsets_s``). Visible means
    elevation >= ``mask_deg``.
    """
    grid = _grid(grid_s)
    mask = _mask(mask_deg)
    tolerance = _tolerance(tolerance_s)
    samples = _evaluate(elevation, grid)

    times, values = _with_extrema(elevation, grid, samples, tolerance)
    times, values = _monotone_breakpoints(elevation, times, values, mask)

    visible = values >= mask
    crossings = np.flatnonzero(visible[:-1] != visible[1:])
    roots = np.empty(times.size - 1)
    roots[crossings] = _solve(
        lambda t: _evaluate(elevation, t) - mask,
        times[crossings],
        times[crossings + 1],
        values[crossings] - mask,
        values[crossings + 1] - mask,
        tolerance,
    )
    return tuple(
        _pass(
            times,
            values,
            first,
            last,
            start=0.0 if first == 0 else roots[first - 1],
            end=times[-1] if last == times.size - 1 else roots[last],
        )
        for first, last in _runs(visible)
    )


def passes_on_grid(
    elevation_values: ArrayLike, grid_s: ArrayLike, mask_deg: float
) -> tuple[Pass, ...]:
    """Passes from grid samples alone, as v1 held them (events off).

    A sample at or above the mask holds visibility until the next sample (left hold), so a pass
    spans [first visible sample, first invisible sample after it), or ends at T when the last
    sample is visible. The culmination is the highest visible sample. A pass visible only at the
    last sample has zero duration; it is kept because v1 reported a contact there.
    """
    grid = _grid(grid_s)
    mask = _mask(mask_deg)
    values = np.asarray(elevation_values, dtype=np.float64)
    if values.shape != grid.shape:
        raise ValueError(
            f"elevation_values has shape {values.shape}; the grid has shape {grid.shape}"
        )
    if not np.isfinite(values).all():
        raise ValueError("elevation_values must be finite")
    last_index = grid.size - 1
    return tuple(
        _pass(
            grid,
            values,
            first,
            last,
            start=grid[first],
            end=grid[min(last + 1, last_index)],
        )
        for first, last in _runs(values >= mask)
    )


def _pass(times, values, first: int, last: int, *, start: float, end: float) -> Pass:
    peak = first + int(np.argmax(values[first : last + 1]))
    return Pass(
        start_s=float(start),
        end_s=float(end),
        culmination_s=float(times[peak]),
        max_elevation_deg=float(values[peak]),
        truncated_start=first == 0,
        truncated_end=last == times.size - 1,
    )


def _runs(visible: NDArray[np.bool_]) -> list[tuple[int, int]]:
    """(first, last) indices of every maximal run of True."""
    edges = np.diff(np.concatenate([[False], visible, [False]]).astype(np.int8))
    return list(
        zip(np.flatnonzero(edges == 1).tolist(), (np.flatnonzero(edges == -1) - 1).tolist())
    )


def _with_extrema(f, grid, samples, tolerance):
    """The grid plus the refined discrete local maxima and minima, sorted, with their values."""
    low = np.concatenate([[-np.inf], samples, [-np.inf]])
    high = np.concatenate([[np.inf], samples, [np.inf]])
    maxima = np.flatnonzero((low[:-2] < samples) & (samples >= low[2:]))
    minima = np.flatnonzero((high[:-2] > samples) & (samples <= high[2:]))
    centres = np.concatenate([maxima, minima])
    sense = np.concatenate([np.ones(maxima.size), -np.ones(minima.size)])
    lo = grid[np.maximum(centres - 1, 0)]
    hi = grid[np.minimum(centres + 1, grid.size - 1)]
    where, value = _golden(f, lo, hi, sense, tolerance)
    # Keep refinements that beat their centre sample; otherwise the sample is the extremum.
    better = sense * value > sense * samples[centres]
    return _merge(grid, samples, where[better], value[better])


def _monotone_breakpoints(f, times, values, mask):
    """Spec section 6.7: bisect intervals whose midpoint leaves the endpoint range toward mask."""
    a, b, fa, fb = times[:-1], times[1:], values[:-1], values[1:]
    added_t, added_v = [], []
    for level in range(MAX_BISECTION_LEVELS + 1):
        middle = a + 0.5 * (b - a)
        fm = _evaluate(f, middle)
        added_t.append(middle)
        added_v.append(fm)
        slack = MONOTONICITY_SLACK * np.maximum(np.maximum(np.abs(fa), np.abs(fb)), 1.0)
        low, high = np.minimum(fa, fb), np.maximum(fa, fb)
        bad = ((fm < low - slack) & (high >= mask)) | ((fm > high + slack) & (low < mask))
        if not bad.any():
            break
        if level == MAX_BISECTION_LEVELS:
            first = int(np.argmax(bad))
            raise ValueError(
                f"non-monotone elevation between {a[first]:.6f} s and {b[first]:.6f} s after "
                f"the window start: the midpoint value {float(fm[first])!r} lies toward the mask "
                f"outside the endpoint range [{float(min(fa[first], fb[first]))!r}, "
                f"{float(max(fa[first], fb[first]))!r}] after "
                f"{MAX_BISECTION_LEVELS} bisections; elevation is not monotone between "
                "breakpoints (is the step longer than P/8?)"
            )
        a, b = np.concatenate([a[bad], middle[bad]]), np.concatenate([middle[bad], b[bad]])
        fa, fb = np.concatenate([fa[bad], fm[bad]]), np.concatenate([fm[bad], fb[bad]])
    return _merge(times, values, np.concatenate(added_t), np.concatenate(added_v))


def _merge(times, values, extra_t, extra_v):
    t = np.concatenate([times, extra_t])
    v = np.concatenate([values, extra_v])
    order = np.argsort(t, kind="stable")
    t, v = t[order], v[order]
    keep = np.concatenate([[True], np.diff(t) > 0.0])
    return t[keep], v[keep]


def _solve(f, lo, hi, f_lo, f_hi, tolerance):
    lo, hi = lo.astype(np.float64, copy=True), hi.astype(np.float64, copy=True)
    f_lo, f_hi = f_lo.astype(np.float64, copy=True), f_hi.astype(np.float64, copy=True)
    zero_lo, zero_hi = f_lo == 0.0, f_hi == 0.0
    unbracketed = (np.sign(f_lo) == np.sign(f_hi)) & ~zero_lo & ~zero_hi
    if unbracketed.any():
        index = int(np.argmax(unbracketed))
        raise ValueError(
            f"f(lo) and f(hi) must have opposite sign or one must be zero; bracket "
            f"[{float(lo[index])!r}, {float(hi[index])!r}] has f = {float(f_lo[index])!r} and {float(f_hi[index])!r}"
        )
    result = np.where(zero_lo, lo, hi)
    sign_hi = np.sign(f_hi)
    active = ~zero_lo & ~zero_hi & (hi - lo > tolerance)
    # Illinois bookkeeping: which end the last step replaced (+1 hi, -1 lo, 0 none), and the
    # bracket widths one and two iterations ago for the bisection safeguard.
    side = np.zeros(lo.size, dtype=np.int8)
    width_1 = np.full(lo.size, np.inf)
    width_2 = np.full(lo.size, np.inf)
    half_tolerance = 0.5 * tolerance
    for _ in range(MAX_ITERATIONS):
        index = np.flatnonzero(active)
        if index.size == 0:
            return result
        a, b, fa, fb = lo[index], hi[index], f_lo[index], f_hi[index]
        width = b - a
        bisect = width > 0.5 * width_2[index]
        width_2[index], width_1[index] = width_1[index], width
        with np.errstate(divide="ignore", invalid="ignore"):
            falsi = b - fb * (width / (fb - fa))
        trial = np.where(bisect | ~np.isfinite(falsi), a + 0.5 * width, falsi)
        trial = np.minimum(np.maximum(trial, a + half_tolerance), b - half_tolerance)
        # Floating point cannot split the bracket: it is as narrow as it can be.
        stuck = (trial <= a) | (trial >= b)
        active[index[stuck]] = False
        index, trial = index[~stuck], trial[~stuck]
        if index.size == 0:
            continue
        f_trial = _evaluate(f, trial)
        exact = f_trial == 0.0
        result[index[exact]] = trial[exact]
        active[index[exact]] = False
        index, trial, f_trial = index[~exact], trial[~exact], f_trial[~exact]
        replace_hi = np.sign(f_trial) == sign_hi[index]
        up, down = index[replace_hi], index[~replace_hi]
        f_lo[up[side[up] == 1]] *= 0.5
        hi[up], f_hi[up], side[up] = trial[replace_hi], f_trial[replace_hi], 1
        f_hi[down[side[down] == -1]] *= 0.5
        lo[down], f_lo[down], side[down] = trial[~replace_hi], f_trial[~replace_hi], -1
        result[index] = hi[index]
        active[index] = hi[index] - lo[index] > tolerance
    if active.any():
        index = int(np.argmax(active))
        raise ValueError(
            f"root-finding did not converge to tolerance_s={tolerance!r} within "
            f"{MAX_ITERATIONS} iterations; bracket [{float(lo[index])!r}, {float(hi[index])!r}] is unresolved"
        )
    return result


def _golden(f, lo, hi, sense, tolerance):
    """Golden-section search maximising ``sense * f`` on every bracket; returns f values."""
    if lo.size == 0:
        return np.empty(0), np.empty(0)
    a, b = lo.astype(np.float64, copy=True), hi.astype(np.float64, copy=True)
    c, d = b - _GOLDEN * (b - a), a + _GOLDEN * (b - a)
    values = _evaluate(f, np.concatenate([c, d])) * np.concatenate([sense, sense])
    fc, fd = values[: a.size], values[a.size :]
    active = b - a > tolerance
    for _ in range(MAX_ITERATIONS):
        index = np.flatnonzero(active)
        if index.size == 0:
            break
        width = b[index] - a[index]
        left = fc[index] >= fd[index]  # the maximum lies in [a, d]
        keep_left, keep_right = index[left], index[~left]
        b[keep_left], d[keep_left], fd[keep_left] = d[keep_left], c[keep_left], fc[keep_left]
        a[keep_right], c[keep_right], fc[keep_right] = (
            c[keep_right],
            d[keep_right],
            fd[keep_right],
        )
        new = np.where(
            left,
            b[index] - _GOLDEN * (b[index] - a[index]),
            a[index] + _GOLDEN * (b[index] - a[index]),
        )
        f_new = _evaluate(f, new) * sense[index]
        c[keep_left], fc[keep_left] = new[left], f_new[left]
        d[keep_right], fd[keep_right] = new[~left], f_new[~left]
        narrowed = b[index] - a[index]
        active[index] = (narrowed > tolerance) & (narrowed < width)
    else:
        if active.any():
            index = int(np.argmax(active))
            raise ValueError(
                f"golden-section search did not converge to tolerance_s={tolerance!r} within "
                f"{MAX_ITERATIONS} iterations; bracket [{float(a[index])!r}, {float(b[index])!r}]"
            )
    best_c = fc >= fd
    return np.where(best_c, c, d), np.where(best_c, fc, fd) * sense


def _evaluate(f, times: NDArray[np.float64]) -> NDArray[np.float64]:
    values = np.asarray(f(times), dtype=np.float64)
    if values.shape != times.shape:
        raise ValueError(f"the provider returned shape {values.shape} for {times.shape[0]} times")
    if not np.isfinite(values).all():
        raise ValueError("the provider returned values that are not finite")
    return values


def _brackets(lo, hi) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    lo, hi = np.asarray(lo, dtype=np.float64), np.asarray(hi, dtype=np.float64)
    if lo.ndim != 1 or lo.shape != hi.shape:
        raise ValueError(
            f"lo and hi must be one-dimensional of equal shape, got {lo.shape} and {hi.shape}"
        )
    if not (np.isfinite(lo).all() and np.isfinite(hi).all()):
        raise ValueError("bracket ends must be finite")
    if (lo > hi).any():
        raise ValueError("every bracket needs lo <= hi")
    return lo, hi


def _grid(grid_s) -> NDArray[np.float64]:
    grid = np.asarray(grid_s, dtype=np.float64)
    if grid.ndim != 1:
        raise ValueError(f"grid_s must be one-dimensional, got shape {grid.shape}")
    if grid.size < 2:
        raise ValueError("grid_s needs at least two samples")
    if not np.isfinite(grid).all():
        raise ValueError("grid_s must be finite")
    if grid[0] != 0.0:
        raise ValueError(f"grid_s must start at 0 (the window start), got {float(grid[0])!r}")
    if not (np.diff(grid) > 0.0).all():
        raise ValueError("grid_s must be strictly increasing")
    return grid


def _mask(mask_deg) -> float:
    if (
        isinstance(mask_deg, bool)
        or not isinstance(mask_deg, int | float | np.floating)
        or not isfinite(mask_deg)
    ):
        raise ValueError(f"mask_deg must be a finite number, got {mask_deg!r}")
    return float(mask_deg)


def _tolerance(tolerance_s) -> float:
    if (
        isinstance(tolerance_s, bool)
        or not isinstance(tolerance_s, int | float | np.floating)
        or not isfinite(tolerance_s)
        or tolerance_s <= 0.0
    ):
        raise ValueError(f"tolerance_s must be a positive finite number, got {tolerance_s!r}")
    return float(tolerance_s)
