"""Exact integration of piecewise-constant functions, adaptive Gauss-Legendre and time measure."""

from math import fsum, pi

import numpy as np
import pytest

from openleo.engine.integrate import integrate_piecewise, integrate_smooth, measure


def test_piecewise_exact() -> None:
    starts = np.array([0.0, 0.1, 0.3, 10.0])
    ends = np.array([0.1, 0.3, 0.7, 10.0])
    values = np.array([3.0, 1e9, 7.5, 1e12])
    # math.fsum of the exact products: one rounding at the end, nothing lost to cancellation.
    expected = fsum([3.0 * 0.1, 1e9 * (0.3 - 0.1), 7.5 * (0.7 - 0.3), 1e12 * 0.0])
    assert integrate_piecewise(starts, ends, values) == expected
    # Cancellation that naive summation gets wrong.
    assert integrate_piecewise([0.0, 1.0, 2.0], [1.0, 2.0, 3.0], [1e16, 1.0, -1e16]) == 1.0
    assert integrate_piecewise([], [], []) == 0.0
    assert integrate_piecewise(np.array([5.0]), np.array([5.0]), np.array([2.0])) == 0.0


@pytest.mark.parametrize(
    ("starts", "ends", "values", "message"),
    [
        ([0.0, 1.0], [1.0], [1.0, 1.0], "same shape"),
        ([[0.0]], [[1.0]], [[1.0]], "one-dimensional"),
        ([0.0], [np.nan], [1.0], "finite"),
        ([0.0], [1.0], [np.inf], "finite"),
        ([1.0], [0.0], [1.0], "start <= end"),
    ],
)
def test_piecewise_rejects_bad_input(starts, ends, values, message) -> None:
    with pytest.raises(ValueError, match=message):
        integrate_piecewise(starts, ends, values)


def test_smooth_matches_analytic() -> None:
    assert abs(integrate_smooth(np.sin, np.array([0.0, pi]), atol=0.0) - 2.0) <= 1e-12

    # A Shannon-like integrand: B log2(1 + 10^((a + b t) / 10)) over a pass with breakpoints.
    bandwidth_hz, a_db, b_db_per_s = 1.2e6, -5.0, 0.04

    def shannon(t):
        return bandwidth_hz * np.log2(1.0 + 10.0 ** ((a_db + b_db_per_s * t) / 10.0))

    breakpoints = np.array([0.0, 60.0, 120.0, 150.5, 300.0, 420.0, 600.0])
    got = integrate_smooth(shannon, breakpoints)
    # Composite Simpson with 1e5 intervals (1e5 + 1 points): error ~ h^4 f'''' far below 1 bit.
    t = np.linspace(0.0, 600.0, 100_001)
    y = shannon(t)
    h = t[1] - t[0]
    simpson = h / 3.0 * (y[0] + y[-1] + 4.0 * y[1:-1:2].sum() + 2.0 * y[2:-1:2].sum())
    assert abs(got - simpson) <= 1e-9 * simpson
    assert abs(got - simpson) <= 1.0


def test_smooth_batches_and_never_evaluates_at_breakpoints() -> None:
    calls = []

    def f(t):
        t = np.asarray(t)
        calls.append(t.copy())
        return np.exp(np.sin(t))  # needs bisection at rtol 1e-12

    breakpoints = np.array([0.0, 0.5, 2.0, 2.0, 7.0])
    value = integrate_smooth(f, breakpoints, rtol=1e-12, atol=0.0)
    reference = integrate_smooth(np.vectorize(lambda x: np.exp(np.sin(x))), np.linspace(0, 7, 201))
    assert abs(value - reference) <= 1e-9 * reference
    evaluated = np.concatenate(calls)
    assert not np.isin(evaluated, breakpoints).any()
    assert (evaluated > 0.0).all() and (evaluated < 7.0).all()
    # One call for the first pass, then at most one per bisection level.
    assert 2 <= len(calls) <= 21


def test_smooth_tiny_interval_has_no_interior_point() -> None:
    a = 5000.0
    b = np.nextafter(a, np.inf)

    def f(t):
        t = np.asarray(t)
        assert ((t > a) & (t < b)).all() or not ((t >= a) & (t <= b)).any()
        return np.ones_like(t)

    # [a, b] holds no float strictly inside: it contributes nothing and f is not called there.
    assert integrate_smooth(f, np.array([a, b])) == 0.0
    assert integrate_smooth(f, np.array([a, b, 5001.0])) == pytest.approx(1.0, abs=1e-12)


def test_smooth_degenerate_breakpoints() -> None:
    def never(t):
        raise AssertionError("not evaluated")

    assert integrate_smooth(never, np.array([])) == 0.0
    assert integrate_smooth(never, np.array([3.0])) == 0.0
    assert integrate_smooth(never, np.array([3.0, 3.0])) == 0.0


def test_smooth_failure_to_converge_raises() -> None:
    rng = np.random.default_rng(0)

    def noise(t):
        return rng.normal(size=np.shape(t))

    with pytest.raises(ValueError, match="did not converge"):
        integrate_smooth(noise, np.array([0.0, 1.0]), atol=0.0, max_depth=5)


@pytest.mark.parametrize(
    ("breakpoints", "kwargs", "message"),
    [
        ([0.0, np.nan], {}, "finite"),
        ([[0.0, 1.0]], {}, "one-dimensional"),
        ([1.0, 0.0], {}, "non-decreasing"),
        ([0.0, 1.0], {"rtol": -1.0}, "rtol"),
        ([0.0, 1.0], {"atol": np.nan}, "atol"),
        ([0.0, 1.0], {"max_depth": -1}, "max_depth"),
        ([0.0, 1.0], {"max_depth": 2.5}, "max_depth"),
        ([0.0, 1.0], {"rtol": 0.0, "atol": 0.0}, "rtol or atol"),
    ],
)
def test_smooth_rejects_bad_input(breakpoints, kwargs, message) -> None:
    with pytest.raises(ValueError, match=message):
        integrate_smooth(np.cos, np.asarray(breakpoints, dtype=float), **kwargs)


def test_smooth_rejects_bad_provider_output() -> None:
    with pytest.raises(ValueError, match="shape"):
        integrate_smooth(lambda t: np.ones(3), np.array([0.0, 1.0]))
    with pytest.raises(ValueError, match="finite"):
        integrate_smooth(lambda t: np.full(np.shape(t), np.nan), np.array([0.0, 1.0]))


def test_measure_union_overlaps() -> None:
    assert measure([]) == 0.0
    assert measure([(0.0, 10.0)]) == 10.0
    # Overlap, containment, touching and zero-length intervals, out of order.
    intervals = [(5.0, 12.0), (0.0, 10.0), (2.0, 3.0), (12.0, 15.0), (20.0, 20.0), (30.0, 31.5)]
    assert measure(intervals) == 15.0 + 1.5
    assert measure(iter(intervals)) == 16.5
    # Touching intervals merge first, so the measure is end - start, not 0.1 + 0.2.
    assert measure([(0.0, 0.1), (0.1, 0.3)]) == 0.3
    assert measure([(1.0, 1.0), (1.0, 1.0)]) == 0.0


@pytest.mark.parametrize(
    ("intervals", "message"),
    [
        ([(1.0, 0.0)], "start <= end"),
        ([(0.0, np.inf)], "finite"),
        ([(0.0,)], "pair"),
    ],
)
def test_measure_rejects_bad_input(intervals, message) -> None:
    with pytest.raises(ValueError, match=message):
        measure(intervals)
