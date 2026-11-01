"""Exact AOS, LOS and culmination events, checked against closed forms and a dense ISS scan."""

import functools
import time
from datetime import UTC, datetime, timedelta
from math import asin, pi
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[2]
V2_EXAMPLES = ROOT / "examples" / "v2"

MASK_DEG = 10.0
PERIOD_S = 5400.0
OMEGA = 2.0 * pi / PERIOD_S
AMPLITUDE_DEG = 40.0
OFFSET_DEG = -5.0
STEP_S = 30.0


class Provider:
    """e(t) = 40 sin(2π t / 5400 + φ) − 5 on [0, T]; counts calls and raises outside the window."""

    def __init__(self, phase: float, window_s: float) -> None:
        self.phase = phase
        self.window_s = window_s
        self.calls = 0
        self.sizes: list[int] = []

    def __call__(self, t):
        t = np.asarray(t, dtype=np.float64)
        if t.ndim != 1:
            raise AssertionError("the provider takes one-dimensional arrays")
        if (t < 0.0).any() or (t > self.window_s).any():
            raise AssertionError(f"evaluated outside [0, {self.window_s}]: {t.min()}..{t.max()}")
        self.calls += 1
        self.sizes.append(t.size)
        return AMPLITUDE_DEG * np.sin(OMEGA * t + self.phase) + OFFSET_DEG


def _grid(window_s: float, step_s: float = STEP_S) -> np.ndarray:
    return np.append(np.arange(0.0, window_s, step_s), window_s)


def _analytic(phase: float, window_s: float):
    """Closed-form rising and falling crossings of MASK_DEG and maxima inside (0, window)."""
    theta = asin((MASK_DEG - OFFSET_DEG) / AMPLITUDE_DEG)
    rising, falling, maxima = [], [], []
    for n in range(-2, int(window_s / PERIOD_S) + 3):
        rising.append((theta + 2 * pi * n - phase) / OMEGA)
        falling.append((pi - theta + 2 * pi * n - phase) / OMEGA)
        maxima.append((pi / 2 + 2 * pi * n - phase) / OMEGA)

    def inside(values):
        return np.array(sorted(v for v in values if 0.0 < v < window_s))

    return inside(rising), inside(falling), inside(maxima)


# --- find_passes on the analytic provider ---------------------------------------------------


@pytest.mark.parametrize("tolerance_s", [1e-3, 1e-6])
def test_crossings_within_tolerance(tolerance_s: float) -> None:
    from openleo.engine.events import find_passes

    phase, window_s = -2.0, 12_007.5  # starts below the mask; the last interval is short
    provider = Provider(phase, window_s)
    rising, falling, _ = _analytic(phase, window_s)

    passes = find_passes(provider, _grid(window_s), MASK_DEG, tolerance_s)

    assert len(passes) == len(rising) == len(falling) == 2
    starts = np.array([p.start_s for p in passes])
    ends = np.array([p.end_s for p in passes])
    assert np.max(np.abs(starts - rising)) <= tolerance_s
    assert np.max(np.abs(ends - falling)) <= tolerance_s
    # Half-open [start, end): the start is visible and the end is not.
    assert (provider(starts) >= MASK_DEG).all()
    assert (provider(ends) < MASK_DEG).all()
    assert not any(p.truncated_start or p.truncated_end for p in passes)


@pytest.mark.parametrize(("tolerance_s", "time_error_s"), [(1e-3, 1e-3), (1e-6, 1e-4)])
def test_culmination_within_tolerance(tolerance_s: float, time_error_s: float) -> None:
    """Culminations are within tolerance_s down to the value-noise floor (module docstring)."""
    from openleo.engine.events import find_passes

    phase, window_s = -2.0, 12_007.5
    provider = Provider(phase, window_s)
    _, _, maxima = _analytic(phase, window_s)

    passes = find_passes(provider, _grid(window_s), MASK_DEG, tolerance_s)

    culminations = np.array([p.culmination_s for p in passes])
    assert np.max(np.abs(culminations - maxima)) <= time_error_s
    for p in passes:
        assert p.max_elevation_deg == pytest.approx(AMPLITUDE_DEG + OFFSET_DEG, abs=1e-9)
        assert p.max_elevation_deg == pytest.approx(
            provider(np.array([p.culmination_s]))[0], abs=1e-12
        )


def test_provider_calls_are_vectorised() -> None:
    """Each solver iteration is one call over every bracket: the count does not grow with N."""
    from openleo.engine.events import find_passes

    counts = []
    for periods in (2, 20):
        window_s = periods * PERIOD_S
        provider = Provider(-2.0, window_s)
        passes = find_passes(provider, _grid(window_s), MASK_DEG, 1e-3)
        assert len(passes) == periods
        counts.append(provider.calls)
    assert counts[1] <= counts[0] + 2
    assert counts[1] <= 40


def test_short_pass_between_samples_found() -> None:
    from openleo.engine.events import find_passes

    window_s, peak_s = 20 * STEP_S, 7.5 * STEP_S + 3.0
    half_width_s = 0.2 * STEP_S  # visible for 0.4 step, entirely between two samples
    curvature = 0.5 / half_width_s**2

    def bump(t):
        t = np.asarray(t, dtype=np.float64)
        assert ((t >= 0.0) & (t <= window_s)).all()
        return 10.5 - curvature * (t - peak_s) ** 2

    grid = _grid(window_s)
    assert (bump(grid) < MASK_DEG).all()

    passes = find_passes(bump, grid, MASK_DEG, 1e-6)

    assert len(passes) == 1
    (p,) = passes
    assert p.start_s == pytest.approx(peak_s - half_width_s, abs=1e-6)
    assert p.end_s == pytest.approx(peak_s + half_width_s, abs=1e-6)
    assert p.culmination_s == pytest.approx(peak_s, abs=1e-3)
    assert p.max_elevation_deg == pytest.approx(10.5, abs=1e-9)
    assert not (p.truncated_start or p.truncated_end)


def test_truncated_edges() -> None:
    from openleo.engine.events import find_passes

    # Visible at t = 0 (falling) and at T (rising): both edge passes are truncated.
    window_s = 10_100.0  # off the 30 s grid; e(T) ≈ 32° and rising
    phase = 2.0  # e(0) = 40 sin(2) - 5 ≈ 31.4°, falling
    provider = Provider(phase, window_s)
    rising, falling, _ = _analytic(phase, window_s)
    assert provider(np.array([0.0, window_s])).min() >= MASK_DEG

    passes = find_passes(provider, _grid(window_s), MASK_DEG, 1e-3)

    first, last = passes[0], passes[-1]
    assert (first.start_s, first.truncated_start, first.truncated_end) == (0.0, True, False)
    assert first.end_s == pytest.approx(falling[0], abs=1e-3)
    # Falling at 0: the maximum inside the window is the edge itself.
    assert first.culmination_s == 0.0
    assert first.max_elevation_deg == pytest.approx(provider(np.array([0.0]))[0], abs=1e-12)
    assert (last.end_s, last.truncated_end, last.truncated_start) == (window_s, True, False)
    assert last.start_s == pytest.approx(rising[-1], abs=1e-3)
    # Still rising at T: the culmination is the window end.
    assert last.culmination_s == window_s
    assert last.max_elevation_deg == pytest.approx(provider(np.array([window_s]))[0], abs=1e-12)


def test_visible_for_whole_window() -> None:
    from openleo.engine.events import find_passes

    window_s = 3_000.0

    def high(t):
        t = np.asarray(t, dtype=np.float64)
        assert ((t >= 0.0) & (t <= window_s)).all()
        return 50.0 + 20.0 * np.sin(OMEGA * t)

    (p,) = find_passes(high, _grid(window_s), MASK_DEG, 1e-3)

    assert (p.start_s, p.end_s, p.truncated_start, p.truncated_end) == (
        0.0,
        window_s,
        True,
        True,
    )
    assert p.culmination_s == pytest.approx(PERIOD_S / 4, abs=1e-3)
    assert p.max_elevation_deg == pytest.approx(70.0, abs=1e-9)


def test_never_visible_returns_no_passes() -> None:
    from openleo.engine.events import find_passes

    window_s = 6_000.0
    provider = Provider(0.0, window_s)
    assert find_passes(provider, _grid(window_s), 40.0, 1e-3) == ()


def test_guard_bisects_hidden_dips() -> None:
    """Samples all read 30° but e dips to 20° between them; mask 22° splits every interval."""
    from openleo.engine.events import find_passes

    step_s, intervals = 60.0, 5
    window_s = intervals * step_s

    def comb(t):
        t = np.asarray(t, dtype=np.float64)
        assert ((t >= 0.0) & (t <= window_s)).all()
        return 25.0 + 5.0 * np.cos(2.0 * pi * t / step_s)

    passes = find_passes(comb, _grid(window_s, step_s), 22.0, 1e-6)

    half_s = step_s * np.arccos(-0.6) / (2.0 * pi)
    assert len(passes) == intervals + 1
    np.testing.assert_allclose(
        [p.start_s for p in passes[1:]], np.arange(1, intervals + 1) * step_s - half_s, atol=1e-6
    )
    np.testing.assert_allclose(
        [p.end_s for p in passes[:-1]], np.arange(intervals) * step_s + half_s, atol=1e-6
    )
    assert passes[0].truncated_start and passes[-1].truncated_end


def test_non_monotone_raises() -> None:
    """A peak at one third of every interval stays off-centre at every bisection level."""
    from openleo.engine.events import find_passes

    step_s, window_s = 60.0, 600.0

    def sawtooth(t):
        t = np.asarray(t, dtype=np.float64)
        return 20.0 - 3.0 * np.abs(np.mod(t / step_s, 1.0) - 1.0 / 3.0)

    with pytest.raises(ValueError, match=r"^non-monotone elevation between \d"):
        find_passes(sawtooth, _grid(window_s, step_s), MASK_DEG, 1e-3)


def test_quantised_provider_terminates() -> None:
    """Geometry is piecewise constant below 1 µs; a 1 µs tolerance must still terminate."""
    from openleo.engine.events import find_passes

    phase, window_s = -2.0, 6_000.0
    provider = Provider(phase, window_s)
    rising, falling, _ = _analytic(phase, window_s)

    def quantised(t):
        return provider(np.round(np.asarray(t) * 1e6) / 1e6)

    passes = find_passes(quantised, _grid(window_s), MASK_DEG, 1e-6)

    assert len(passes) == 1
    assert abs(passes[0].start_s - rising[0]) <= 2e-6
    assert abs(passes[0].end_s - falling[0]) <= 2e-6


@pytest.mark.parametrize(
    ("grid", "mask", "tolerance", "message"),
    [
        (np.array([0.0]), 10.0, 1e-3, "at least two"),
        (np.array([[0.0, 1.0]]), 10.0, 1e-3, "one-dimensional"),
        (np.array([0.0, 2.0, 1.0]), 10.0, 1e-3, "strictly increasing"),
        (np.array([0.0, np.nan]), 10.0, 1e-3, "finite"),
        (np.array([1.0, 2.0]), 10.0, 1e-3, "start at 0"),
        (np.array([0.0, 1.0]), np.nan, 1e-3, "mask"),
        (np.array([0.0, 1.0]), 10.0, 0.0, "tolerance"),
        (np.array([0.0, 1.0]), 10.0, np.inf, "tolerance"),
    ],
)
def test_find_passes_rejects_bad_input(grid, mask, tolerance, message) -> None:
    from openleo.engine.events import find_passes

    with pytest.raises(ValueError, match=message):
        find_passes(lambda t: np.zeros_like(t), grid, mask, tolerance)


def test_find_passes_rejects_bad_provider_output() -> None:
    from openleo.engine.events import find_passes

    with pytest.raises(ValueError, match="finite"):
        find_passes(lambda t: np.full_like(t, np.nan), _grid(300.0), MASK_DEG, 1e-3)
    with pytest.raises(ValueError, match="shape"):
        find_passes(lambda t: np.zeros(3), _grid(300.0), MASK_DEG, 1e-3)


# --- solve_brackets and maximize_brackets ---------------------------------------------------


@pytest.mark.parametrize("tolerance_s", [1e-3, 1e-6, 1e-9])
def test_solve_brackets_returns_the_far_side_within_tolerance(tolerance_s: float) -> None:
    from openleo.engine.events import solve_brackets

    roots = np.array([0.3, 1.7, 2.2, 5.0 - 1e-12, 7.0 + 1e-12])
    lo, hi = np.array([0.0, 1.5, 2.1, 4.0, 7.0]), np.array([1.0, 2.0, 3.0, 5.0, 8.0])
    signs = np.array([1.0, -1.0, 1.0, -1.0, 1.0])
    calls = []

    def f(t):
        calls.append(t.size)
        index = np.searchsorted(hi, t, side="left").clip(0, len(hi) - 1)
        return signs[index] * np.tanh(t - roots[index]) * (1.0 + (t - roots[index]) ** 2)

    found = solve_brackets(f, lo, hi, tolerance_s)
    assert len(calls) <= 40

    assert np.all(np.abs(found - roots) <= tolerance_s)
    # Each returned point has the sign of f(hi): it is the start of the following run.
    assert np.all(np.sign(f(found)) == np.sign(f(hi)))


def test_solve_brackets_zero_endpoints_and_empty_input() -> None:
    from openleo.engine.events import solve_brackets

    def f(t):
        return t - 1.0

    np.testing.assert_array_equal(
        solve_brackets(f, np.array([1.0, 0.0]), np.array([2.0, 1.0]), 1e-3), [1.0, 1.0]
    )
    # An exact zero hit during the iteration is returned as it is.
    assert solve_brackets(f, np.array([0.0]), np.array([2.0]), 1e-9)[0] == 1.0
    assert solve_brackets(f, np.array([]), np.array([]), 1e-3).shape == (0,)


def test_solve_brackets_terminates_on_flat_steps() -> None:
    from openleo.engine.events import solve_brackets

    def step(t):
        return np.where(t < 0.123456789, -1.0, 1.0)

    found = solve_brackets(step, np.array([0.0]), np.array([10.0]), 1e-6)
    assert 0.123456789 <= found[0] <= 0.123456789 + 1e-6


def test_solve_brackets_below_float_resolution_terminates() -> None:
    from openleo.engine.events import solve_brackets

    found = solve_brackets(lambda t: t - 7_000.1, np.array([7_000.0]), np.array([7_001.0]), 1e-15)
    assert abs(found[0] - 7_000.1) <= 2e-12


@pytest.mark.parametrize(
    ("lo", "hi", "tolerance", "message"),
    [
        (np.array([2.0]), np.array([3.0]), 1e-3, "sign"),
        (np.array([0.0]), np.array([3.0, 4.0]), 1e-3, "shape"),
        (np.array([3.0]), np.array([0.0]), 1e-3, "lo <= hi"),
        (np.array([np.nan]), np.array([0.0]), 1e-3, "finite"),
        (np.array([0.0]), np.array([3.0]), -1.0, "tolerance"),
    ],
)
def test_solve_brackets_rejects_bad_input(lo, hi, tolerance, message) -> None:
    from openleo.engine.events import solve_brackets

    with pytest.raises(ValueError, match=message):
        solve_brackets(lambda t: t - 1.0, lo, hi, tolerance)


def test_solve_brackets_iteration_limit(monkeypatch) -> None:
    from openleo.engine import events

    monkeypatch.setattr(events, "MAX_ITERATIONS", 2)
    with pytest.raises(ValueError, match="did not converge"):
        events.solve_brackets(np.cbrt, np.array([-1.0]), np.array([3.0]), 1e-9)
    with pytest.raises(ValueError, match="did not converge"):
        events.maximize_brackets(np.cos, np.array([-1.0]), np.array([3.0]), 1e-9)


def test_maximize_brackets() -> None:
    from openleo.engine.events import maximize_brackets

    peaks = np.array([0.25, 3.9, 6.0])
    lo, hi = np.array([0.0, 2.0, 6.0]), np.array([1.0, 4.0, 9.0])
    calls = []

    def f(t):
        calls.append(t.size)
        index = np.searchsorted(hi, t, side="left").clip(0, 2)
        return 5.0 - (t - peaks[index]) ** 2

    where, value = maximize_brackets(f, lo, hi, 1e-3)
    assert len(calls) <= 25

    # The last bracket is decreasing: the maximum sits at its left end.
    assert np.all(np.abs(where - peaks) <= 1e-3)
    np.testing.assert_array_equal(value, f(where))
    empty_where, empty_value = maximize_brackets(f, np.array([]), np.array([]), 1e-3)
    assert empty_where.shape == empty_value.shape == (0,)
    with pytest.raises(ValueError, match="tolerance"):
        maximize_brackets(f, lo, hi, 0.0)


# --- passes_on_grid (events off) ------------------------------------------------------------


def test_passes_on_grid_left_hold() -> None:
    from openleo.engine.events import Pass, passes_on_grid

    grid = np.array([0.0, 10.0, 20.0, 30.0, 40.0, 50.0, 55.0])
    values = np.array([12.0, 9.0, 10.0, 15.0, 11.0, 3.0, 10.0])

    passes = passes_on_grid(values, grid, MASK_DEG)

    assert passes == (
        Pass(0.0, 10.0, 0.0, 12.0, True, False),
        Pass(20.0, 50.0, 30.0, 15.0, False, False),
        # Visible only at the stop point: zero duration under left-hold, kept like v1's contact.
        Pass(55.0, 55.0, 55.0, 10.0, False, True),
    )
    assert passes_on_grid(np.full(3, 20.0), grid[:3], MASK_DEG) == (
        Pass(0.0, 20.0, 0.0, 20.0, True, True),
    )
    assert passes_on_grid(np.zeros(3), grid[:3], MASK_DEG) == ()
    with pytest.raises(ValueError, match="shape"):
        passes_on_grid(values[:-1], grid, MASK_DEG)
    with pytest.raises(ValueError, match="finite"):
        passes_on_grid(np.full(grid.size, np.nan), grid, MASK_DEG)


def test_passes_on_grid_matches_v1_remaining_contacts() -> None:
    from openleo.constellation import _remaining_contacts
    from openleo.engine.events import passes_on_grid

    rng = np.random.default_rng(8)
    start = datetime(2026, 9, 10, 12, tzinfo=UTC)
    grid = np.append(np.arange(0.0, 2_000.0, 20.0), 2_007.0)
    timestamps = [start + timedelta(seconds=float(t)) for t in grid]
    for _ in range(20):
        values = rng.uniform(0.0, 20.0, grid.size)
        visible = values >= MASK_DEG
        passes = passes_on_grid(values, grid, MASK_DEG)
        contacts = _remaining_contacts(timestamps, visible)
        held = np.zeros(grid.size, dtype=bool)
        for p in passes:
            members = (grid >= p.start_s) & (grid < p.end_s)
            if p.truncated_end:
                members |= grid == grid[-1]
            held |= members
            for index in np.flatnonzero(members):
                assert contacts[index] == (p.end_s - grid[index], p.truncated_end)
            assert p.max_elevation_deg == values[members].max()
        np.testing.assert_array_equal(held, visible)


# --- the v2 ISS example against a dense scan ------------------------------------------------


@functools.cache
def _iss():
    from openleo.engine.catalogs import load_catalogs
    from openleo.engine.geometry import Geometry
    from openleo.engine.timegrid import build_grid, builtin_timescale
    from openleo.scenario import load_scenario

    scenario = load_scenario(V2_EXAMPLES / "iss_cartagena.json")
    timescale = builtin_timescale()
    orbits = load_catalogs(scenario.space_segment, timescale)
    grid = build_grid(scenario.time)
    geometry = Geometry(orbits, scenario.ground_segment.stations, timescale, grid.start_utc)
    return scenario, grid, geometry


def _dense_crossing(geometry, mask: float, centre_s: float, window_s: float) -> tuple:
    """Bracket of a 0.01 s scan around centre_s, and its linearly interpolated crossing."""
    times = np.round(np.arange(-500, 501) * 0.01 + centre_s, 6)
    times = times[(times >= 0.0) & (times <= window_s)]
    excess = geometry.elevation_deg(0, 0, times) - mask
    (index,) = np.flatnonzero(np.diff(np.sign(excess)) != 0)
    a, b = times[index], times[index + 1]
    fa, fb = excess[index], excess[index + 1]
    return a, b, a - fa * (b - a) / (fb - fa)


def test_iss_pass_matches_dense_search() -> None:
    from openleo.engine.events import find_passes

    scenario, grid, geometry = _iss()
    mask = scenario.ground_segment.stations[0].minimum_elevation_deg
    tolerance = scenario.time.events.tolerance_s
    window_s = float(grid.offsets_s[-1])
    calls = []

    def elevation(t):
        calls.append(t.size)
        return geometry.elevation_deg(0, 0, t)

    began = time.perf_counter()
    (p,) = find_passes(elevation, grid.offsets_s, mask, tolerance)
    assert time.perf_counter() - began < 10.0
    assert not (p.truncated_start or p.truncated_end)
    assert len(calls) <= 40

    for event in (p.start_s, p.end_s):
        a, b, interpolated = _dense_crossing(geometry, mask, event, window_s)
        assert a - 2e-3 <= event <= b + 2e-3
        assert abs(event - interpolated) <= 2e-3

    # Culmination against a 0.01 s scan of the whole pass.
    scan = np.round(np.arange(p.start_s, p.end_s - tolerance, 0.01), 6)
    values = geometry.elevation_deg(0, 0, scan)
    assert abs(p.culmination_s - scan[np.argmax(values)]) <= 0.01
    assert values.max() <= p.max_elevation_deg + 1e-9
    assert (values >= mask).all()
