"""Exact and adaptive integration in seconds, on one time-measure convention (spec section 6.4).

- ``integrate_piecewise``: a piecewise-constant function (rates, indicators) integrated exactly,
  as the ``math.fsum`` of value x duration, one correctly rounded sum.
- ``integrate_smooth``: a smooth function (the Shannon upper bound) by composite 5-point
  Gauss-Legendre on every sub-interval between breakpoints, bisected adaptively.
- ``measure``: the Lebesgue measure of a union of intervals, in seconds. Durations and
  fractions everywhere are measures (fraction = measure / window length); nothing counts samples.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from numpy.typing import ArrayLike, NDArray

# 5-point Gauss-Legendre on [-1, 1]: every node is strictly inside, so neither end of a
# sub-interval is ever evaluated (a pass end below the mask must not be).
_NODES, _WEIGHTS = np.polynomial.legendre.leggauss(5)


def integrate_piecewise(starts: ArrayLike, ends: ArrayLike, values: ArrayLike) -> float:
    """Exact integral of a function equal to ``values[i]`` on [starts[i], ends[i]).

    ``math.fsum(values * (ends - starts))``: each product is rounded once and the sum is
    correctly rounded, so the result is the exact sum of the rounded products. Intervals are
    not checked for overlap; an overlap is counted twice.
    """
    starts, ends, values = (np.asarray(a, dtype=np.float64) for a in (starts, ends, values))
    if not (starts.shape == ends.shape == values.shape):
        raise ValueError(
            f"starts, ends and values must have the same shape, got {starts.shape}, "
            f"{ends.shape} and {values.shape}"
        )
    if starts.ndim != 1:
        raise ValueError(f"starts, ends and values must be one-dimensional, got {starts.shape}")
    if not (np.isfinite(starts).all() and np.isfinite(ends).all() and np.isfinite(values).all()):
        raise ValueError("starts, ends and values must be finite")
    if (starts > ends).any():
        index = int(np.argmax(starts > ends))
        raise ValueError(
            f"every interval needs start <= end, got [{float(starts[index])!r}, "
            f"{float(ends[index])!r}]"
        )
    return math.fsum((values * (ends - starts)).tolist())


def integrate_smooth(
    f: Callable[[NDArray[np.float64]], ArrayLike],
    breakpoints_s: ArrayLike,
    rtol: float = 1e-9,
    atol: float = 1.0,
    max_depth: int = 20,
) -> float:
    """Integral of a smooth ``f`` from the first breakpoint to the last.

    Every sub-interval between consecutive breakpoints is integrated with 5-point
    Gauss-Legendre and compared with the sum over its two halves; it is accepted when the change
    is below ``rtol`` times the refined value or below ``atol`` (bits, for a rate in bit/s), and
    bisected again otherwise. The refined value of every accepted piece is summed with
    ``math.fsum``. A piece still unaccepted after ``max_depth`` bisections raises ValueError.

    ``f`` maps a one-dimensional array of times to values of the same shape. It is called once
    for the first estimate and once per bisection level, for every piece together, and only at
    Gauss-Legendre nodes, which lie strictly inside each piece: it is never evaluated at a
    breakpoint, so it may be undefined at the ends (a link below the mask at LOS). A piece with
    no floating-point number strictly inside it (its ends are adjacent floats, about 1e-12 s
    apart at 7200 s) contributes nothing; its true integral is at most its width times sup |f|.
    Empty sub-intervals (repeated breakpoints) and fewer than two breakpoints integrate to 0.
    """
    breakpoints = np.asarray(breakpoints_s, dtype=np.float64)
    if breakpoints.ndim != 1:
        raise ValueError(f"breakpoints_s must be one-dimensional, got shape {breakpoints.shape}")
    if not np.isfinite(breakpoints).all():
        raise ValueError("breakpoints_s must be finite")
    if (np.diff(breakpoints) < 0.0).any():
        raise ValueError("breakpoints_s must be non-decreasing")
    rtol = _non_negative(rtol, "rtol")
    atol = _non_negative(atol, "atol")
    if rtol == 0.0 and atol == 0.0:
        raise ValueError("rtol or atol must be positive, or no piece could ever be accepted")
    if isinstance(max_depth, bool) or not isinstance(max_depth, int | np.integer) or max_depth < 0:
        raise ValueError(f"max_depth must be a non-negative integer, got {max_depth!r}")

    a, b = breakpoints[:-1], breakpoints[1:]
    keep = a < b
    a, b = a[keep], b[keep]
    if a.size == 0:
        return 0.0
    whole = _gauss_legendre(f, a, b)
    accepted: list[float] = []
    for depth in range(max_depth + 1):
        middle = a + 0.5 * (b - a)
        halves = _gauss_legendre(f, np.concatenate([a, middle]), np.concatenate([middle, b]))
        left, right = halves[: a.size], halves[a.size :]
        refined = left + right
        change = np.abs(refined - whole)
        done = (change <= rtol * np.abs(refined)) | (change <= atol)
        accepted.extend(refined[done].tolist())
        if done.all():
            return math.fsum(accepted)
        if depth == max_depth:
            break
        undone = ~done
        a, b = (
            np.concatenate([a[undone], middle[undone]]),
            np.concatenate([middle[undone], b[undone]]),
        )
        whole = np.concatenate([left[undone], right[undone]])
    index = int(np.argmax(~done))
    raise ValueError(
        f"adaptive Gauss-Legendre did not converge on [{float(a[index])!r}, "
        f"{float(b[index])!r}] within {max_depth} bisection levels (rtol={rtol!r}, "
        f"atol={atol!r}): the last change was {float(change[index])!r}"
    )


def measure(intervals: Iterable[tuple[float, float]]) -> float:
    """Lebesgue measure of the union of [start, end) intervals, in their unit (seconds).

    Overlapping and touching intervals are merged first, so every point counts once and a run
    of touching intervals measures as its last end minus its first start (one subtraction).
    """
    pairs = []
    for interval in intervals:
        try:
            start, end = interval
        except (TypeError, ValueError):
            raise ValueError(
                f"every interval must be a (start, end) pair, got {interval!r}"
            ) from None
        start, end = float(start), float(end)
        if not (math.isfinite(start) and math.isfinite(end)):
            raise ValueError(f"interval ends must be finite, got ({start!r}, {end!r})")
        if start > end:
            raise ValueError(f"every interval needs start <= end, got ({start!r}, {end!r})")
        if start < end:
            pairs.append((start, end))
    pairs.sort()
    lengths = []
    run_start = run_end = None
    for start, end in pairs:
        if run_end is not None and start <= run_end:
            run_end = max(run_end, end)
            continue
        if run_end is not None:
            lengths.append(run_end - run_start)
        run_start, run_end = start, end
    if run_end is not None:
        lengths.append(run_end - run_start)
    return math.fsum(lengths)


def _gauss_legendre(f, a: NDArray[np.float64], b: NDArray[np.float64]) -> NDArray[np.float64]:
    """5-point Gauss-Legendre on every [a, b], with one call of f for all of them.

    A piece with no float strictly inside (b <= nextafter(a)) is not evaluated and gives 0.
    """
    result = np.zeros(a.size)
    inside = np.flatnonzero(np.nextafter(a, np.inf) < b)
    if inside.size == 0:
        return result
    a, b = a[inside], b[inside]
    centre, half = a + 0.5 * (b - a), 0.5 * (b - a)
    nodes = centre[:, None] + half[:, None] * _NODES[None, :]
    # Rounding can put a node of a piece a few ulps wide on its end; keep nodes strictly inside.
    nodes = np.clip(nodes, np.nextafter(a, np.inf)[:, None], np.nextafter(b, -np.inf)[:, None])
    flat = nodes.ravel()
    values = np.asarray(f(flat), dtype=np.float64)
    if values.shape != flat.shape:
        raise ValueError(f"f returned shape {values.shape} for {flat.size} times")
    if not np.isfinite(values).all():
        raise ValueError("f returned values that are not finite")
    result[inside] = half * (values.reshape(nodes.shape) @ _WEIGHTS)
    return result


def _non_negative(value, name: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float | np.floating)
        or not math.isfinite(value)
        or value < 0.0
    ):
        raise ValueError(f"{name} must be a non-negative finite number, got {value!r}")
    return float(value)
