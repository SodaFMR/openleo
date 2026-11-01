"""The v1 MODCOD rule in continuous time, checked against closed forms, v1 fixtures and a real pass."""

import functools
import gzip
import json
from itertools import pairwise
from pathlib import Path

import numpy as np
import pytest

from openleo.physics.adaptation import MODCODS, select_modcod
from openleo.scenario.model import Adaptation

ROOT = Path(__file__).resolve().parents[2]
V1_FIXTURES = ROOT / "tests" / "fixtures" / "v1"
V2_EXAMPLES = ROOT / "examples" / "v2"
CONSTELLATION_FIXTURES = (
    "iridium_global",
    "iridium_global_reference",
    "iridium_global_hydrometeors",
)

SYMBOL_RATE_BAUD = 1e6
TOLERANCE_S = 1e-6
PASS_END_S = 1500.0
STEP_S = 30.0
NAMES = [modcod.name for modcod in MODCODS]
RATE = {modcod.name: SYMBOL_RATE_BAUD * modcod.efficiency_bits_per_symbol for modcod in MODCODS}
RATE[None] = 0.0


def _adaptation(hysteresis_db: float = 0.5, delay_s: float = 0.0, margin_db: float = 0.0):
    return Adaptation(
        standard="dvb-s2",
        table="v1-reference-subset",
        symbol_rate_baud=SYMBOL_RATE_BAUD,
        rolloff=0.2,
        implementation_margin_db=margin_db,
        hysteresis_db=hysteresis_db,
        feedback_delay_s=delay_s,
    )


class Provider:
    """A(t) on a pass [start, end): counts calls and refuses times the link evaluator would."""

    def __init__(self, function, start_s: float = 0.0, end_s: float = PASS_END_S) -> None:
        self.function = function
        self.start_s = start_s
        self.end_s = end_s
        self.calls = 0
        self.sizes: list[int] = []

    def __call__(self, t):
        t = np.asarray(t, dtype=np.float64)
        if t.ndim != 1:
            raise AssertionError("the provider takes one-dimensional arrays")
        if (t < self.start_s).any() or (t >= self.end_s).any():
            raise AssertionError(f"evaluated outside [{self.start_s}, {self.end_s}): {t}")
        self.calls += 1
        self.sizes.append(t.size)
        return self.function(t)


def _pass(start_s: float = 0.0, end_s: float = PASS_END_S, truncated_end: bool = False):
    from openleo.engine.events import Pass

    return Pass(start_s, end_s, 0.5 * (start_s + end_s), 50.0, start_s == 0.0, truncated_end)


def _breakpoints(end_s: float = PASS_END_S, step_s: float = STEP_S) -> np.ndarray:
    return np.append(np.arange(0.0, end_s, step_s), end_s)


def _run(function, delay_s=0.0, hysteresis_db=0.5, passes=None, breakpoints=None):
    from openleo.engine.acm import acm_segments

    provider = Provider(function)
    segments, events = acm_segments(
        provider,
        passes if passes is not None else (_pass(),),
        _breakpoints() if breakpoints is None else breakpoints,
        _adaptation(hysteresis_db, delay_s),
        TOLERANCE_S,
    )
    _check_tiling(segments, passes if passes is not None else (_pass(),))
    return segments, events, provider


def _check_tiling(segments, passes) -> None:
    """Segments tile every pass exactly, carry v1 rates and never repeat a neighbour."""
    for p in passes:
        inside = [s for s in segments if p.start_s <= s.start_s < p.end_s]
        if p.end_s == p.start_s:
            assert inside == []
            continue
        assert inside[0].start_s == p.start_s and inside[-1].end_s == p.end_s
        for left, right in pairwise(inside):
            assert left.end_s == right.start_s
            assert (left.modcod, left.rate_bps) != (right.modcod, right.rate_bps)
    assert sum(1 for s in segments if any(p.start_s <= s.start_s < p.end_s for p in passes)) == len(
        segments
    )
    for segment in segments:
        assert segment.start_s < segment.end_s
        assert segment.rate_bps == RATE[segment.modcod]


def _at(segments, t: float):
    (segment,) = [s for s in segments if s.start_s <= t < s.end_s]
    return segment.modcod


def _up(t):
    return -4.0 + 0.01 * t


def _down(t):
    return -4.0 + 0.01 * (PASS_END_S - t)


# Level crossings of the ramps, (level + 4) / 0.01 s from either end.
UP = [  # acquisition at req(QPSK 1/4), then each upgrade at req(n) + 0.5 dB hysteresis
    (165.0, None, "QPSK 1/4"),
    (550.0, "QPSK 1/4", "QPSK 1/2"),
    (853.0, "QPSK 1/2", "QPSK 3/4"),
    (1112.0, "QPSK 3/4", "8PSK 2/3"),
    (1241.0, "8PSK 2/3", "8PSK 3/4"),
]
DOWN = [  # each downgrade at req(m) itself, no hysteresis; the last one loses lock
    (0.0, None, "8PSK 3/4"),
    (1500.0 - 1191.0, "8PSK 3/4", "8PSK 2/3"),
    (1500.0 - 1062.0, "8PSK 2/3", "QPSK 3/4"),
    (1500.0 - 803.0, "QPSK 3/4", "QPSK 1/2"),
    (1500.0 - 500.0, "QPSK 1/2", "QPSK 1/4"),
    (1500.0 - 165.0, "QPSK 1/4", None),
]


def _check_events(events, expected, delay_s=0.0, end_s=PASS_END_S) -> None:
    assert [(e.from_modcod, e.to_modcod) for e in events] == [(a, b) for _, a, b in expected]
    for event, (time_s, _, _) in zip(events, expected, strict=True):
        assert abs(event.decision_s - time_s) <= TOLERANCE_S, (event, time_s)
        assert event.effective_s == min(event.decision_s + delay_s, end_s)


# --- Step 1 tests from the brief ------------------------------------------------------------


def test_acquisition_and_upgrades_at_exact_thresholds() -> None:
    segments, events, provider = _run(_up)

    _check_events(events, UP)
    assert [s.modcod for s in segments] == [None, *NAMES]
    for segment, event in zip(segments[1:], events, strict=True):
        assert segment.start_s == event.decision_s
    # At or above the threshold is lock; just before it is not.
    for event in events:
        level = provider(np.array([event.decision_s, event.decision_s - TOLERANCE_S]))
        modcod = next(m for m in MODCODS if m.name == event.to_modcod)
        threshold = modcod.required_esn0_db + (0.5 if event.from_modcod else 0.0)
        assert level[0] >= threshold > level[1]


def test_immediate_downgrade_without_hysteresis() -> None:
    segments, events, _ = _run(_down)

    _check_events(events, DOWN)
    assert [s.modcod for s in segments] == [*reversed(NAMES), None]
    assert segments[0].start_s == 0.0 and segments[-1].end_s == PASS_END_S


def test_feedback_delay_semantics() -> None:
    delay = 2.0
    segments, events, _ = _run(_up, delay_s=delay)
    _check_events(events, UP, delay)
    t = [e.decision_s for e in events]
    # Acquisition: rate 0 until t + d. Upgrade: the old rate is kept until t + d.
    assert _at(segments, t[0] + 1.0) is None
    assert _at(segments, t[0] + delay) == "QPSK 1/4"
    assert _at(segments, t[1] + 1.999) == "QPSK 1/4"
    assert _at(segments, t[1] + delay) == "QPSK 1/2"
    assert [s.modcod for s in segments] == [None, *NAMES]
    assert segments[1].start_s == t[0] + delay

    segments, events, _ = _run(_down, delay_s=delay)
    _check_events(events, DOWN, delay)
    t = [e.decision_s for e in events]
    # Downgrade: rate 0 on [t, t + d), then the new mode. Loss: rate 0 from t.
    assert _at(segments, t[1] - 1e-3) == "8PSK 3/4"
    assert _at(segments, t[1]) is None
    assert _at(segments, t[1] + 1.999) is None
    assert _at(segments, t[1] + delay) == "8PSK 2/3"
    assert _at(segments, t[-1]) is None
    assert [s.modcod for s in segments] == [
        None,
        "8PSK 3/4",
        None,
        "8PSK 2/3",
        None,
        "QPSK 3/4",
        None,
        "QPSK 1/2",
        None,
        "QPSK 1/4",
        None,
    ]
    blackouts = [s for s in segments[1:-1] if s.modcod is None]
    assert [s.start_s for s in blackouts] == t[1:-1]
    assert all(abs(s.end_s - s.start_s - delay) <= 1e-9 for s in blackouts)


def test_feedback_delay_at_the_pass_end() -> None:
    from openleo.engine.events import Pass

    delay = 2.0
    # An upgrade decided at 1241 s would take effect at 1243 s, after LOS at 1242 s: dropped,
    # recorded with effective_s = end, and the old mode is kept to the end.
    short = Pass(0.0, 1242.0, 600.0, 50.0, True, False)
    segments, events, _ = _run(_up, delay_s=delay, passes=(short,), breakpoints=_breakpoints())
    assert events[-1].to_modcod == "8PSK 3/4" and events[-1].effective_s == 1242.0
    assert abs(events[-1].decision_s - 1241.0) <= TOLERANCE_S
    assert segments[-1].modcod == "8PSK 2/3" and segments[-1].end_s == 1242.0

    # A loss decided at 1335 s with LOS at 1336 s: rate 0 to the end.
    short = Pass(0.0, 1336.0, 600.0, 50.0, True, False)
    segments, events, _ = _run(_down, delay_s=delay, passes=(short,), breakpoints=_breakpoints())
    assert events[-1].to_modcod is None and events[-1].effective_s == 1336.0
    assert segments[-2].modcod == "QPSK 1/4" and abs(segments[-2].end_s - 1335.0) <= TOLERANCE_S
    assert segments[-1].modcod is None and segments[-1].end_s == 1336.0


def test_feedback_delay_overlapping_decisions() -> None:
    # Acquisition at 1.65 s and an upgrade at 5.5 s with d = 5 s: each takes effect d later.
    segments, events, _ = _run(lambda t: -4.0 + t, delay_s=5.0, breakpoints=_breakpoints())
    assert [(e.from_modcod, e.to_modcod) for e in events[:2]] == [
        (None, "QPSK 1/4"),
        ("QPSK 1/4", "QPSK 1/2"),
    ]
    assert abs(segments[1].start_s - 6.65) <= TOLERANCE_S and segments[1].modcod == "QPSK 1/4"
    assert abs(segments[2].start_s - 10.5) <= TOLERANCE_S and segments[2].modcod == "QPSK 1/2"


def test_threshold_equality_is_lock() -> None:
    from openleo.engine.acm import acm_on_grid

    for value, expected in ((-2.35, "QPSK 1/4"), (1.5, "QPSK 1/2"), (1.0, "QPSK 1/2")):
        segments, events, _ = _run(lambda t, value=value: np.full(t.shape, value))
        assert [(s.start_s, s.end_s, s.modcod) for s in segments] == [(0.0, PASS_END_S, expected)]
        assert [(e.decision_s, e.from_modcod, e.to_modcod) for e in events] == [
            (0.0, None, expected)
        ]
        grid = _breakpoints()
        on_grid = acm_on_grid(np.full(grid.size, value), grid, (_pass(),), _adaptation())
        assert on_grid == (segments, events)


@pytest.mark.parametrize("peak_s", [750.0, 761.3])
def test_tangent_peak(peak_s: float) -> None:
    # A touches req(QPSK 1/4) only at its peak (a breakpoint, as a culmination is): lock for an
    # instant only, which carries no time; no events and no loop.
    breakpoints = np.union1d(_breakpoints(), [peak_s])
    segments, events, provider = _run(
        lambda t: -2.35 - 1e-5 * (t - peak_s) ** 2, breakpoints=breakpoints
    )
    assert [(s.start_s, s.end_s, s.modcod) for s in segments] == [(0.0, PASS_END_S, None)]
    assert events == ()
    assert provider.calls <= 5

    # Touching req(QPSK 1/2) + hysteresis for an instant upgrades, and v1's rule then keeps
    # QPSK 1/2 while A >= req(QPSK 1/2): the instant matters through the hysteresis.
    segments, events, _ = _run(lambda t: 1.5 - 1e-4 * (t - peak_s) ** 2, breakpoints=breakpoints)
    assert [(e.from_modcod, e.to_modcod) for e in events] == [
        (None, "QPSK 1/4"),
        ("QPSK 1/4", "QPSK 1/2"),
        ("QPSK 1/2", "QPSK 1/4"),
        ("QPSK 1/4", None),
    ]
    assert events[1].decision_s == peak_s
    assert abs(events[2].decision_s - (peak_s + np.sqrt(0.5 / 1e-4))) <= TOLERANCE_S
    assert len(segments) == 5


def test_peak_between_breakpoints_just_above_a_threshold() -> None:
    # A's maximum lies 0.4 s after the breakpoint at 750 s (as A's peak sits a fraction of a
    # second from the elevation culmination) and exceeds req(QPSK 1/2) + hysteresis by 5e-6 dB,
    # while every breakpoint value is below it. The upgrade happens, and holds until A < 1.0.
    peak_s, excess, k = 750.4, 5e-6, 1e-4
    segments, events, _ = _run(lambda t: 1.5 + excess - k * (t - peak_s) ** 2)
    assert [(e.from_modcod, e.to_modcod) for e in events] == [
        (None, "QPSK 1/4"),
        ("QPSK 1/4", "QPSK 1/2"),
        ("QPSK 1/2", "QPSK 1/4"),
        ("QPSK 1/4", None),
    ]
    assert abs(events[1].decision_s - (peak_s - np.sqrt(excess / k))) <= TOLERANCE_S
    assert abs(events[2].decision_s - (peak_s + np.sqrt((0.5 + excess) / k))) <= TOLERANCE_S
    assert [s.modcod for s in segments] == [None, "QPSK 1/4", "QPSK 1/2", "QPSK 1/4", None]

    # The mirror image: a trough just below req(QPSK 1/2) downgrades, and the climb back above
    # 1.0 dB does not upgrade again until req(QPSK 1/2) + hysteresis.
    _, events, _ = _run(lambda t: 1.0 - excess + k * (t - peak_s) ** 2)
    pairs = [(e.from_modcod, e.to_modcod) for e in events]
    index = pairs.index(("QPSK 1/2", "QPSK 1/4"))
    assert abs(events[index].decision_s - (peak_s - np.sqrt(excess / k))) <= TOLERANCE_S
    assert pairs[index + 1] == ("QPSK 1/4", "QPSK 1/2")
    assert abs(events[index + 1].decision_s - (peak_s + np.sqrt((0.5 + excess) / k))) <= 1e-5


def test_midpoint_guard_finds_a_hidden_excursion() -> None:
    # A narrow bump inside one inter-breakpoint interval reaches the acquisition threshold at
    # the interval midpoint: the guard bisects and the excursion is resolved.
    def bump(t):
        return -3.0 + 0.7 * np.exp(-(((t - 315.0) / 4.0) ** 2))

    _, events, _ = _run(bump)
    assert [(e.from_modcod, e.to_modcod) for e in events] == [
        (None, "QPSK 1/4"),
        ("QPSK 1/4", None),
    ]
    half_width = 4.0 * np.sqrt(np.log(0.7 / 0.65))
    assert abs(events[0].decision_s - (315.0 - half_width)) <= TOLERANCE_S
    assert abs(events[1].decision_s - (315.0 + half_width)) <= TOLERANCE_S

    # A bump that crosses no threshold is harmless and costs no bisection.
    _, events, provider = _run(lambda t: -3.0 + 0.5 * np.exp(-(((t - 315.0) / 4.0) ** 2)))
    assert events == () and provider.calls == 2


def test_persistent_non_monotonicity_raises() -> None:
    from openleo.engine.acm import acm_segments, threshold_levels_db

    calls = []
    thresholds = threshold_levels_db(_adaptation())

    def escalating(t):
        # Every guard call puts the midpoints one threshold above all values seen so far.
        calls.append(t.size)
        if len(calls) == 1:
            return np.full(t.shape, thresholds[0] - 0.05)
        return np.full(t.shape, thresholds[len(calls) - 2] + 0.05)

    with pytest.raises(ValueError, match="non-monotone"):
        acm_segments(escalating, (_pass(),), _breakpoints(), _adaptation(), TOLERANCE_S)
    assert len(calls) == 8


def test_grid_mode_matches_v1_select_modcod() -> None:
    from openleo.engine.acm import acm_on_grid
    from openleo.engine.events import passes_on_grid

    total = exact = 0
    for name in CONSTELLATION_FIXTURES:
        fixture = _v1_fixture(name)
        adaptation = _scenario(name).adaptation
        margin_db = adaptation.implementation_margin_db
        grid = (
            np.arange(len(fixture["timestamps_utc"])) * fixture["scenario"]["time_window"]["step_s"]
        )
        links = {}
        for link in fixture["links"]:
            links.setdefault((link["station_index"], link["satellite_index"]), {})[
                link["time_index"]
            ] = link
        for pair in links.values():
            visible = np.zeros(grid.size, dtype=bool)
            visible[list(pair)] = True
            available_db = np.full(grid.size, np.nan)  # never read outside passes
            for index, link in pair.items():
                available_db[index] = link["esn0_db"] - margin_db
            passes = passes_on_grid(np.where(visible, 20.0, 0.0), grid, 10.0)
            segments, events = acm_on_grid(available_db, grid, passes, adaptation)
            _check_tiling(segments, passes)
            for index, link in sorted(pair.items()):
                p = next(p for p in passes if p.start_s <= grid[index] <= p.end_s)
                # v1's state at the sample: the last decision at or before it in its pass.
                decided = [e for e in events if p.start_s <= e.decision_s <= grid[index]]
                modcod = decided[-1].to_modcod if decided else None
                if grid[index] < p.end_s:
                    assert _at(segments, grid[index]) == modcod
                    assert RATE[modcod] == link["rate_bps"]
                total += 1
                exact += modcod == link["modcod"]
    assert total == exact == sum(len(_v1_fixture(n)["links"]) for n in CONSTELLATION_FIXTURES)


@pytest.mark.parametrize(
    "function",
    [_up, _down, lambda t: -6.0 + 14.0 * np.sin(np.pi * t / PASS_END_S)],
    ids=["up", "down", "hump"],
)
@pytest.mark.parametrize("hysteresis_db", [0.0, 0.5])
def test_d0_continuous_matches_grid_at_samples(function, hysteresis_db: float) -> None:
    from openleo.engine.acm import acm_on_grid

    segments, events, _ = _run(function, hysteresis_db=hysteresis_db)
    grid = _breakpoints()
    values = np.where(grid < PASS_END_S, function(grid), np.nan)
    on_grid, grid_events = acm_on_grid(values, grid, (_pass(),), _adaptation(hysteresis_db))
    _check_tiling(on_grid, (_pass(),))
    decisions = np.array([e.decision_s for e in events])
    compared = 0
    for t in grid[grid < PASS_END_S]:
        if np.min(np.abs(decisions - t), initial=np.inf) > TOLERANCE_S:
            assert _at(segments, t) == _at(on_grid, t), t
            compared += 1
    assert compared >= grid.size - 2
    # The same decisions, each delayed to the first sample at or after it.
    assert [(e.from_modcod, e.to_modcod) for e in events] == [
        (e.from_modcod, e.to_modcod) for e in grid_events
    ]
    for exact, sampled in zip(events, grid_events, strict=True):
        assert exact.decision_s <= sampled.decision_s < exact.decision_s + STEP_S


# --- passes, ends and validation ------------------------------------------------------------


def test_several_passes_batched_and_reset() -> None:
    from openleo.engine.events import Pass

    first = Pass(100.0, 400.0, 250.0, 30.0, False, False)
    second = Pass(700.0, 1500.0, 1500.0, 40.0, False, True)
    breakpoints = np.union1d(_breakpoints(), [250.0])

    def a(t):
        assert not ((t >= 400.0) & (t < 700.0)).any(), t
        return np.where(t < 500.0, 3.0 - 1e-4 * (t - 250.0) ** 2, -4.0 + 0.01 * t)

    provider = Provider(a, 100.0, 1500.0 + 1e-9)
    from openleo.engine.acm import acm_segments

    segments, events = acm_segments(provider, (first, second), breakpoints, _adaptation(), 1e-6)
    _check_tiling(segments, (first, second))
    # Lock state resets at each pass start: the second pass acquires afresh at its AOS.
    assert (events[0].decision_s, events[0].from_modcod) == (100.0, None)
    second_events = [e for e in events if e.decision_s >= 700.0]
    assert (second_events[0].decision_s, second_events[0].from_modcod) == (700.0, None)
    # A(700 s) = 3.0 dB: acquisition takes the highest mode with A >= req, no hysteresis.
    assert second_events[0].to_modcod == "QPSK 1/2"
    # The truncated end is evaluated at the window end itself.
    assert segments[-1].end_s == 1500.0 and segments[-1].modcod == "8PSK 3/4"
    # Never evaluated in the gap between the passes, nor at or after the first LOS.
    assert provider.calls <= 20


def test_zero_duration_and_sub_tolerance_passes() -> None:
    from openleo.engine.acm import acm_on_grid, acm_segments
    from openleo.engine.events import Pass

    adaptation = _adaptation(delay_s=0.0)
    instant = Pass(1500.0, 1500.0, 1500.0, 10.0, False, True)
    provider = Provider(lambda t: np.full(t.shape, 2.0), 0.0, 1500.0 + 1e-9)
    segments, events = acm_segments(provider, (instant,), _breakpoints(), adaptation, 1e-3)
    # The acquisition at the single visible instant takes effect at or after the end: dropped.
    assert segments == ()
    assert [(e.decision_s, e.effective_s, e.to_modcod) for e in events] == [
        (1500.0, 1500.0, "QPSK 1/2")
    ]
    grid = _breakpoints()
    assert acm_on_grid(np.full(grid.size, 2.0), grid, (instant,), adaptation) == (segments, events)

    # A pass shorter than the tolerance is evaluated at its AOS only and held to its LOS.
    tiny = Pass(200.0, 200.0005, 200.0, 10.0, False, False)
    provider = Provider(_up, 200.0, 200.0005)
    segments, events = acm_segments(provider, (tiny,), _breakpoints(), adaptation, 1e-3)
    assert [(s.start_s, s.end_s, s.modcod) for s in segments] == [(200.0, 200.0005, "QPSK 1/4")]
    assert provider.calls == 1  # one value; no interval, so no guard or root-finding call


def test_grid_mode_decides_at_the_last_sample_of_a_truncated_pass() -> None:
    from openleo.engine.acm import acm_on_grid
    from openleo.engine.events import passes_on_grid

    grid = np.array([0.0, 30.0, 60.0])
    passes = passes_on_grid(np.full(3, 20.0), grid, 10.0)
    segments, events = acm_on_grid(np.array([-2.0, -2.0, 2.0]), grid, passes, _adaptation())
    # v1 reported QPSK 1/2 at the last sample; it holds for no time, so it is dropped.
    assert [(e.decision_s, e.effective_s, e.to_modcod) for e in events] == [
        (0.0, 0.0, "QPSK 1/4"),
        (60.0, 60.0, "QPSK 1/2"),
    ]
    assert [(s.start_s, s.end_s, s.modcod) for s in segments] == [(0.0, 60.0, "QPSK 1/4")]


def test_no_passes() -> None:
    from openleo.engine.acm import acm_on_grid, acm_segments

    provider = Provider(_up)
    assert acm_segments(provider, (), _breakpoints(), _adaptation(), 1e-3) == ((), ())
    assert provider.calls == 0
    grid = _breakpoints()
    assert acm_on_grid(np.full(grid.size, np.nan), grid, (), _adaptation()) == ((), ())


def test_implementation_margin_is_not_applied_twice() -> None:
    # available_db already has the margin removed; the selection must not subtract it again.
    segments, events, _ = _run(lambda t: np.full(t.shape, -2.35))
    from openleo.engine.acm import acm_segments

    margin = _adaptation(margin_db=3.0)
    again, again_events = acm_segments(
        Provider(lambda t: np.full(t.shape, -2.35)), (_pass(),), _breakpoints(), margin, 1e-6
    )
    assert (again, again_events) == (segments, events)
    assert select_modcod(-2.35 + 3.0, margin.kernel()) is MODCODS[0]


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"tolerance_s": 0.0}, "tolerance_s"),
        ({"breakpoints_s": np.zeros((2, 2))}, "breakpoints_s"),
        ({"breakpoints_s": np.array([0.0, np.nan])}, "breakpoints_s"),
        ({"passes": ("pass",)}, "Pass"),
        ({"passes": "pass_end_first"}, "start_s <= end_s"),
        ({"passes": "overlapping"}, "overlap"),
        ({"passes": "infinite"}, "finite"),
        ({"adaptation": _adaptation(delay_s=-1.0)}, "feedback_delay_s"),
        ({"adaptation": _adaptation(delay_s=float("nan"))}, "feedback_delay_s"),
        ({"adaptation": _adaptation(hysteresis_db=-1.0)}, "hysteresis_db"),
        ({"available_db": lambda t: np.zeros(1)}, "shape"),
        ({"available_db": lambda t: np.full(t.shape, np.inf)}, "finite"),
    ],
)
def test_acm_segments_rejects_bad_input(change, message) -> None:
    from openleo.engine.acm import acm_segments
    from openleo.engine.events import Pass

    passes = {
        "pass_end_first": (Pass(10.0, 5.0, 7.0, 20.0, False, False),),
        "overlapping": (_pass(0.0, 100.0), Pass(50.0, 200.0, 100.0, 20.0, False, False)),
        "infinite": (Pass(0.0, np.inf, 7.0, 20.0, True, False),),
    }
    arguments = {
        "available_db": _up,
        "passes": (_pass(),),
        "breakpoints_s": _breakpoints(),
        "adaptation": _adaptation(),
        "tolerance_s": 1e-6,
    } | change
    arguments["passes"] = passes.get(arguments["passes"], arguments["passes"])
    with pytest.raises(ValueError, match=message):
        acm_segments(**arguments)


def test_acm_on_grid_rejects_bad_input() -> None:
    from openleo.engine.acm import acm_on_grid

    grid = _breakpoints()
    values = _up(grid)
    with pytest.raises(ValueError, match="shape"):
        acm_on_grid(values[:-1], grid, (_pass(),), _adaptation())
    holed = values.copy()
    holed[3] = np.nan
    with pytest.raises(ValueError, match="finite"):
        acm_on_grid(holed, grid, (_pass(),), _adaptation())
    with pytest.raises(ValueError, match="grid_s"):
        acm_on_grid(values, grid[::-1], (_pass(),), _adaptation())


# --- a real pass: Iridium with declared hydrometeors -----------------------------------------


@functools.cache
def _v1_fixture(name: str) -> dict:
    return json.loads(gzip.decompress((V1_FIXTURES / f"{name}.json.gz").read_bytes()))


@functools.cache
def _scenario(name: str):
    from openleo.scenario import load_scenario

    return load_scenario(V2_EXAMPLES / f"{name}.json")


@functools.cache
def _iridium():
    from openleo.engine.catalogs import load_catalogs
    from openleo.engine.evaluator import LinkEvaluator
    from openleo.engine.geometry import Geometry
    from openleo.engine.timegrid import build_grid, builtin_timescale

    scenario = _scenario("iridium_global_hydrometeors")
    timescale = builtin_timescale()
    orbits = load_catalogs(scenario.space_segment, timescale)
    grid = build_grid(scenario.time)
    geometry = Geometry(orbits, scenario.ground_segment.stations, timescale, grid.start_utc)
    return scenario, grid, geometry, LinkEvaluator(scenario, geometry)


# (station, satellite): passes whose A crosses acquisition, hysteresis and upgrade levels.
REAL_PAIRS = ((3, 31), (3, 76))


@pytest.mark.parametrize(("station", "satellite"), REAL_PAIRS)
@pytest.mark.parametrize("tolerance_s", [1e-3, 1e-6])
def test_real_iridium_pass_against_a_dense_v1_walk(station, satellite, tolerance_s) -> None:
    from openleo.engine.acm import acm_segments
    from openleo.engine.events import find_passes

    scenario, grid, geometry, evaluator = _iridium()
    mask = scenario.ground_segment.stations[station].minimum_elevation_deg
    passes = find_passes(
        lambda t: geometry.elevation_deg(station, satellite, t), grid.offsets_s, mask, tolerance_s
    )
    breakpoints = np.union1d(grid.offsets_s, [p.culmination_s for p in passes])
    calls = []

    def available_db(t):
        calls.append(t.size)
        return evaluator.available_db(station, satellite, t)

    segments, events = acm_segments(
        available_db, passes, breakpoints, scenario.adaptation, tolerance_s
    )
    _check_tiling(segments, passes)
    assert len(events) >= 4
    assert len(calls) <= 25

    rule = scenario.adaptation.kernel()
    margin = scenario.adaptation.implementation_margin_db
    for p in passes:
        # v1's rule walked on a 1 s scan (A is monotone between its samples except at the peak,
        # whose sample is the culmination) equals the segments away from decisions.
        scan = np.append(np.arange(p.start_s, p.end_s - tolerance_s, 1.0), p.culmination_s)
        scan = np.sort(scan)
        values = evaluator.available_db(station, satellite, scan)
        decisions = np.array([e.decision_s for e in events])
        previous = None
        for t, value in zip(scan, values, strict=True):
            previous = select_modcod(value + margin, rule, previous)
            if np.min(np.abs(decisions - t)) > 1.0:
                assert _at(segments, t) == (previous.name if previous else None), t
        # Each decision brackets its threshold within tolerance_s.
        for event in (e for e in events if p.start_s <= e.decision_s < p.end_s):
            after, before = evaluator.available_db(
                station,
                satellite,
                [event.decision_s, max(p.start_s, event.decision_s - tolerance_s)],
            )
            old = next((m for m in MODCODS if m.name == event.from_modcod), None)
            new = next((m for m in MODCODS if m.name == event.to_modcod), None)
            assert select_modcod(after + margin, rule, old) is new
            if event.decision_s > p.start_s:
                assert select_modcod(before + margin, rule, old) is old


# --- feedback delay: zero rate only while the mode in effect fails (Task 9 ruling) -------------


def _peak(t):
    """A peaks 1e-4 dB above req(QPSK 1/2) = 1.0 dB at 750 s; >= 1.0 on about [749, 751]."""
    return 1.0 + 1e-4 - 1e-4 * (t - 750.0) ** 2


def test_downgrade_while_an_upgrade_is_pending_keeps_the_running_mode() -> None:
    # Reviewer probe (Task 9): h = 0, d = 2 s. QPSK 1/4 runs; the upgrade to QPSK 1/2 decided at
    # ~749 s takes effect at ~751 s; the downgrade back to QPSK 1/4 is decided at ~751 s, just
    # before that. QPSK 1/4 meets its threshold, so the downgrade decision zeroes nothing.
    breakpoints = np.union1d(_breakpoints(), [750.0])
    segments, events, _ = _run(_peak, delay_s=2.0, hysteresis_db=0.0, breakpoints=breakpoints)
    upgrade, downgrade = events[1], events[2]
    assert (upgrade.from_modcod, upgrade.to_modcod) == ("QPSK 1/4", "QPSK 1/2")
    assert (downgrade.from_modcod, downgrade.to_modcod) == ("QPSK 1/2", "QPSK 1/4")
    assert abs(upgrade.decision_s - 749.0) <= 1e-3 and abs(downgrade.decision_s - 751.0) <= 1e-3
    assert downgrade.decision_s < upgrade.effective_s < downgrade.effective_s
    # The Task 9 output had a zero-rate segment from the downgrade decision to its effect.
    assert all(s.start_s != downgrade.decision_s for s in segments)
    assert _at(segments, downgrade.decision_s) == "QPSK 1/4"
    assert _at(segments, 0.5 * (downgrade.decision_s + upgrade.effective_s)) == "QPSK 1/4"
    # The pending upgrade still arrives (the transmitter cannot know it was revoked): QPSK 1/2
    # is in effect on [upgrade effective, downgrade effective) with A below 1.0 dB, so frames
    # are lost there, and only there.
    zero = [s for s in segments if s.modcod is None and 600.0 < s.start_s < 900.0]
    assert [(s.start_s, s.end_s) for s in zero] == [(upgrade.effective_s, downgrade.effective_s)]
    assert _at(segments, downgrade.effective_s) == "QPSK 1/4"


def test_zero_rate_ends_when_the_mode_in_effect_recovers() -> None:
    # A dips 1e-4 dB below req(QPSK 1/2) on about (749, 751) and recovers; h = 0, d = 4 s.
    def trough(t):
        return 1.0 - 1e-4 + 1e-4 * (t - 750.0) ** 2

    breakpoints = np.union1d(_breakpoints(), [750.0])
    passes = (_pass(500.0, 1000.0),)
    segments, events, _ = _run(
        trough, delay_s=4.0, hysteresis_db=0.0, passes=passes, breakpoints=breakpoints
    )
    down = next(e for e in events if abs(e.decision_s - 749.0) <= 1e-3)
    up = next(e for e in events if abs(e.decision_s - 751.0) <= 1e-3)
    assert (down.from_modcod, down.to_modcod, up.to_modcod) == ("QPSK 1/2", "QPSK 1/4", "QPSK 1/2")
    tail = [(s.start_s, s.end_s, s.modcod) for s in segments if s.start_s >= down.decision_s]
    assert tail[:3] == [
        # QPSK 1/2 is in effect and fails until A is back at 1.0 dB ...
        (down.decision_s, up.decision_s, None),
        # ... then decodes again until the downgrade arrives;
        (up.decision_s, down.effective_s, "QPSK 1/2"),
        # QPSK 1/4 runs until the upgrade arrives.
        (down.effective_s, up.effective_s, "QPSK 1/4"),
    ]
    assert tail[3][::2] == (up.effective_s, "QPSK 1/2")


def test_grid_mode_zero_rate_only_while_the_mode_in_effect_fails() -> None:
    from openleo.engine.acm import acm_on_grid
    from openleo.engine.events import passes_on_grid

    grid = np.arange(0.0, 70.0, 10.0)
    passes = passes_on_grid(np.full(grid.size, 20.0), grid, 10.0)
    # QPSK 1/2 from 0 s; A dips below 1.0 dB at the 20 s sample only; d = 25 s.
    available = np.array([2.0, 2.0, 0.5, 2.0, 2.0, 2.0, 2.0])
    segments, events = acm_on_grid(available, grid, passes, _adaptation(0.0, 25.0))
    assert [(e.decision_s, e.to_modcod) for e in events] == [
        (0.0, "QPSK 1/2"),
        (20.0, "QPSK 1/4"),
        (30.0, "QPSK 1/2"),
    ]
    assert [(s.start_s, s.end_s, s.modcod) for s in segments] == [
        (0.0, 30.0, None),  # acquisition delay, then QPSK 1/2 with A = 0.5 dB held to 30 s
        (30.0, 45.0, "QPSK 1/2"),  # A is back above 1.0 dB: QPSK 1/2 decodes again
        (45.0, 55.0, "QPSK 1/4"),  # the downgrade arrives, then the upgrade
        (55.0, 60.0, "QPSK 1/2"),
    ]
    # d = 5 s: QPSK 1/2 is in effect and fails on [20, 25); QPSK 1/4 then runs until 35 s.
    segments, _ = acm_on_grid(available, grid, passes, _adaptation(0.0, 5.0))
    assert [(s.start_s, s.end_s, s.modcod) for s in segments] == [
        (0.0, 5.0, None),
        (5.0, 20.0, "QPSK 1/2"),
        (20.0, 25.0, None),
        (25.0, 35.0, "QPSK 1/4"),
        (35.0, 60.0, "QPSK 1/2"),
    ]


def test_crossings_use_the_values_already_known_at_breakpoints() -> None:
    # A provider that returns an ulp-level different value at a breakpoint when evaluated in
    # another batch: the crossing bracket must keep the sign seen when the level was counted.
    level = 1.0  # req(QPSK 1/2); with h = 0 it is an upgrade level too
    batches = []

    def jumpy(t):
        batches.append(t.size)
        near = level - 1e-12 if len(batches) == 1 else level + 1e-12
        return np.where(t == 300.0, near, level + 0.01 * (t - 300.0))

    _, events, _ = _run(jumpy, hysteresis_db=0.0)
    upgrade = next(e for e in events if e.to_modcod == "QPSK 1/2")
    assert 300.0 < upgrade.decision_s <= 300.0 + TOLERANCE_S
