"""Per-pair and per-station link statistics on one time-measure convention (spec section 6.4)."""

import functools
import gzip
import json
import math
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from openleo.engine.acm import RateSegment
from openleo.engine.events import Pass
from openleo.physics.adaptation import MODCODS

ROOT = Path(__file__).resolve().parents[2]
V1_FIXTURES = ROOT / "tests" / "fixtures" / "v1"
V2_EXAMPLES = ROOT / "examples" / "v2"
CONSTELLATION_FIXTURES = (
    "iridium_global",
    "iridium_global_reference",
    "iridium_global_hydrometeors",
)
RATE = {m.name: 1e6 * m.efficiency_bits_per_symbol for m in MODCODS}
Q14, Q12 = RATE["QPSK 1/4"], RATE["QPSK 1/2"]


def _pass(start, end, *, truncated_start=False, truncated_end=False, peak=45.0):
    return Pass(start, end, 0.5 * (start + end), peak, truncated_start, truncated_end)


def _seg(start, end, modcod=None, rate=None):
    return RateSegment(start, end, modcod, RATE[modcod] if rate is None and modcod else rate or 0.0)


def _pair(satellite, passes, segments, *, station=0, shannon=0.0, peak=45.0, range_m=1e6):
    from openleo.engine.statistics import PairResult

    return PairResult(
        station_index=station,
        satellite_index=satellite,
        passes=tuple(passes),
        segments=tuple(segments),
        modcod_events=(),
        shannon_bits=shannon,
        max_elevation_deg=peak,
        min_range_m=range_m,
    )


def test_pair_metrics_hand_case() -> None:
    from openleo.engine.statistics import pair_metrics

    pair = _pair(
        7,
        [
            _pass(100.0, 400.0),
            _pass(500.0, 500.0),  # an exact mask touch: zero duration, not a contact
            _pass(900.0, 1000.0, truncated_end=True),
            _pass(1000.0, 1000.0, truncated_end=True),  # visible only at the instant T
        ],
        [
            _seg(100.0, 150.0),
            _seg(150.0, 300.0, "QPSK 1/4"),
            _seg(300.0, 400.0, "QPSK 1/2"),
            _seg(900.0, 950.0, "QPSK 1/4"),
            _seg(950.0, 1000.0),
        ],
        shannon=1.25e9,
        peak=61.5,
        range_m=812_345.5,
    )
    metrics = pair_metrics(pair)
    bits = math.fsum([Q14 * 150.0, Q12 * 100.0, Q14 * 50.0])
    expected = {
        "pass_count": 2,
        "visible_duration_s": 400.0,
        "rf_usable_duration_s": 300.0,
        "integrated_rate_bits": bits,
        "integrated_shannon_upper_bound_bits": 1.25e9,
        "max_elevation_deg": 61.5,
        "min_range_m": 812_345.5,
        "mean_rate_visible_bps": bits / 400.0,
        "residence_qpsk_1_4_s": 200.0,
        "residence_qpsk_1_2_s": 100.0,
        "residence_qpsk_3_4_s": 0.0,
        "residence_8psk_2_3_s": 0.0,
        "residence_8psk_3_4_s": 0.0,
    }
    assert metrics == expected
    assert list(metrics) == list(expected)
    assert type(metrics["pass_count"]) is int


def test_pair_metrics_never_visible() -> None:
    from openleo.engine.statistics import pair_metrics

    metrics = pair_metrics(_pair(0, [], [], peak=math.nan, range_m=math.nan))
    assert metrics["pass_count"] == 0
    assert metrics["visible_duration_s"] == metrics["integrated_rate_bits"] == 0.0
    assert math.isnan(metrics["mean_rate_visible_bps"])
    assert math.isnan(metrics["max_elevation_deg"])


def test_pair_metrics_rejects_unknown_modcod() -> None:
    from openleo.engine.statistics import pair_metrics

    pair = _pair(0, [_pass(0.0, 10.0)], [_seg(0.0, 10.0, "16APSK 9/10", 3e6)])
    with pytest.raises(ValueError, match="16APSK 9/10"):
        pair_metrics(pair)


# Hand-built station 0. Satellite 1 is best first; 0 ties it at 5 bit/s (lower index wins) and
# again at 8 bit/s; 3 follows 0 without a gap; a gap separates 3 from 1's second pass; 2 follows 1
# at the same rate (a handover, not a merge); 2's own zero-rate stretch breaks its run.
def _station_pairs():
    return [
        _pair(1, [_pass(0.0, 20.0), _pass(40.0, 50.0)], [
            _seg(0.0, 10.0, "x", 5.0), _seg(10.0, 20.0, "x", 8.0), _seg(40.0, 50.0, "x", 2.0),
        ]),
        _pair(0, [_pass(5.0, 25.0)], [_seg(5.0, 15.0, "x", 5.0), _seg(15.0, 25.0, "x", 8.0)]),
        _pair(3, [_pass(25.0, 35.0)], [_seg(25.0, 30.0, "x", 3.0), _seg(30.0, 35.0)]),
        _pair(2, [_pass(50.0, 80.0)], [
            _seg(50.0, 60.0, "x", 2.0), _seg(60.0, 70.0), _seg(70.0, 80.0, "x", 4.0),
        ]),
    ]  # fmt: skip


def test_best_link_ties_and_handovers() -> None:
    from openleo.engine.statistics import best_link_segments, station_metrics

    pairs = _station_pairs()
    assert best_link_segments(pairs) == (
        (0.0, 5.0, 1, 5.0),
        (5.0, 10.0, 0, 5.0),
        (10.0, 15.0, 1, 8.0),
        (15.0, 25.0, 0, 8.0),
        (25.0, 30.0, 3, 3.0),
        (30.0, 40.0, None, 0.0),
        (40.0, 50.0, 1, 2.0),
        (50.0, 60.0, 2, 2.0),
        (60.0, 70.0, None, 0.0),
        (70.0, 80.0, 2, 4.0),
    )
    assert best_link_segments(pairs[::-1]) == best_link_segments(pairs)
    metrics = station_metrics(0, pairs, 100.0, 10.0)
    assert metrics == {
        "visible_fraction": 0.75,  # [0, 35) and [40, 80)
        "rf_usable_fraction": 0.6,  # [0, 30), [40, 60) and [70, 80)
        "best_link_integrated_bits": 265.0,
        "best_link_handovers": 5,  # 1->0, 0->1, 1->0, 0->3, 1->2; not 3->1 across the gap
        "best_link_mean_rate_bps": 2.65,
        "fixed_capacity_integrated_bits": 750.0,
    }
    assert type(metrics["best_link_handovers"]) is int
    assert "fixed_capacity_integrated_bits" not in station_metrics(0, pairs, 100.0, None)


def test_best_link_empty_and_ulp_neighbours() -> None:
    from openleo.engine.statistics import best_link_segments, station_metrics

    assert best_link_segments([]) == ()
    assert best_link_segments([_pair(0, [], [])]) == ()
    # Cut times one ulp apart still give exact, gap-free envelopes.
    t = np.nextafter(10.0, 0.0)
    pairs = [
        _pair(0, [_pass(0.0, t)], [_seg(0.0, t, "x", 1.0)]),
        _pair(1, [_pass(10.0, 20.0)], [_seg(10.0, 20.0, "x", 1.0)]),
    ]
    assert best_link_segments(pairs) == (
        (0.0, t, 0, 1.0),
        (t, 10.0, None, 0.0),
        (10.0, 20.0, 1, 1.0),
    )
    metrics = station_metrics(0, pairs, 20.0, None)
    assert metrics["best_link_handovers"] == 0
    assert metrics["best_link_integrated_bits"] == t + 10.0
    assert station_metrics(0, [], 20.0, 5.0) == {
        "visible_fraction": 0.0,
        "rf_usable_fraction": 0.0,
        "best_link_integrated_bits": 0.0,
        "best_link_handovers": 0,
        "best_link_mean_rate_bps": 0.0,
        "fixed_capacity_integrated_bits": 0.0,
    }


def test_fractions_use_time_measure() -> None:
    from openleo.engine.statistics import pair_metrics, station_metrics

    # On a 60 s grid over a 100 s window [0, 60, 100], sample counting would give 1/3 for a
    # satellite seen at 0 s only, and 2/3 for one seen at 60 s and at T. Time measure counts
    # seconds: a 0.5 s pass is 0.5 % of the window; a pass truncated at T ends at T (no extra
    # interval after the last sample), and an instant at T adds nothing.
    early = _pair(0, [_pass(0.0, 0.5, truncated_start=True)], [_seg(0.0, 0.5, "QPSK 1/4")])
    late = _pair(
        1,
        [_pass(59.5, 100.0, truncated_end=True)],
        [_seg(59.5, 60.0), _seg(60.0, 100.0, "QPSK 1/2")],
    )
    instant = _pair(2, [_pass(100.0, 100.0, truncated_end=True)], [])
    metrics = station_metrics(0, [early, late, instant], 100.0, 1e6)
    assert metrics["visible_fraction"] == (0.5 + 40.5) / 100.0
    assert metrics["rf_usable_fraction"] == (0.5 + 40.0) / 100.0
    assert metrics["fixed_capacity_integrated_bits"] == 1e6 * 41.0
    assert pair_metrics(late)["visible_duration_s"] == 40.5
    assert pair_metrics(late)["rf_usable_duration_s"] == 40.0
    assert pair_metrics(instant)["pass_count"] == 0
    # Overlapping visibility of two satellites is counted once.
    a = _pair(0, [_pass(10.0, 30.0)], [_seg(10.0, 30.0, "QPSK 1/4")])
    b = _pair(1, [_pass(20.0, 50.0)], [_seg(20.0, 50.0, "QPSK 1/4")])
    metrics = station_metrics(0, [a, b], 200.0, None)
    assert metrics["visible_fraction"] == metrics["rf_usable_fraction"] == 0.2


@pytest.mark.parametrize(
    ("pairs", "window_s", "fixed", "message"),
    [
        ([_pair(0, [], [], station=1)], 10.0, None, "station_index"),
        ([_pair(0, [], []), _pair(0, [], [])], 10.0, None, "more than once"),
        ([], 0.0, None, "window_s"),
        ([], math.inf, None, "window_s"),
        ([], 10.0, -1.0, "fixed_capacity_bps"),
        ([], 10.0, math.nan, "fixed_capacity_bps"),
        ([_pair(0, [_pass(5.0, 11.0)], [])], 10.0, None, "outside the window"),
        ([_pair(0, [_pass(0.0, 5.0)], [_seg(4.0, 6.0, "x", 1.0)])], 10.0, None, "outside"),
        ([_pair(0, [_pass(0.0, 5.0)], [_seg(0.0, 2.0, "x", -1.0)])], 10.0, None, "rate"),
        ([_pair(0, [_pass(0.0, 5.0)], [_seg(0.0, 3.0, "x", 1.0), _seg(2.0, 5.0)])], 10.0, None,
         "overlap"),
        ([_pair(0, [_pass(0.0, math.nan)], [])], 10.0, None, "non-finite"),
        ([_pair(0, [_pass(0.0, 5.0)], [_seg(3.0, 2.0)])], 10.0, None, "start_s > end_s"),
    ],
)  # fmt: skip
def test_station_metrics_rejects_bad_input(pairs, window_s, fixed, message) -> None:
    from openleo.engine.statistics import station_metrics

    with pytest.raises(ValueError, match=message):
        station_metrics(0, pairs, window_s, fixed)


# --- events off: v1 equivalence ---------------------------------------------------------------


@functools.cache
def _v1_fixture(name: str) -> dict:
    return json.loads(gzip.decompress((V1_FIXTURES / f"{name}.json.gz").read_bytes()))


@functools.cache
def _grid_left_hold(name: str):
    """Every pair of the v2 twin of a v1 example, with events disabled (spec section 6.4)."""
    from openleo.engine.acm import acm_on_grid
    from openleo.engine.catalogs import load_catalogs
    from openleo.engine.evaluator import LinkEvaluator
    from openleo.engine.events import passes_on_grid
    from openleo.engine.geometry import Geometry
    from openleo.engine.integrate import integrate_piecewise
    from openleo.engine.statistics import PairResult
    from openleo.engine.timegrid import build_grid, builtin_timescale
    from openleo.scenario import load_scenario

    scenario = load_scenario(V2_EXAMPLES / f"{name}.json")
    scenario = replace(
        scenario, time=replace(scenario.time, events=replace(scenario.time.events, enabled=False))
    )
    timescale = builtin_timescale()
    orbits = load_catalogs(scenario.space_segment, timescale)
    grid = build_grid(scenario.time)
    offsets = grid.offsets_s
    geometry = Geometry(orbits, scenario.ground_segment.stations, timescale, grid.start_utc)
    evaluator = LinkEvaluator(scenario, geometry)
    margin_db = scenario.adaptation.implementation_margin_db
    stations = []
    for station_index, station in enumerate(scenario.ground_segment.stations):
        mask_deg = (
            station.minimum_elevation_deg
            if station.minimum_elevation_deg is not None
            else scenario.ground_segment.default_minimum_elevation_deg
        )
        pairs = []
        for satellite_index in range(len(orbits)):
            topocentric = geometry.topocentric(station_index, satellite_index, offsets)
            passes = passes_on_grid(topocentric.elevation_deg, offsets, mask_deg)
            visible = np.flatnonzero(topocentric.elevation_deg >= mask_deg)
            available_db = np.full(offsets.size, np.nan)
            shannon_bps = np.zeros(offsets.size)
            if visible.size:
                link = evaluator.evaluate(station_index, satellite_index, offsets[visible])
                available_db[visible] = link["esn0_db"] - margin_db
                shannon_bps[visible] = link["shannon_upper_bound_bps"]
            segments, events = acm_on_grid(available_db, offsets, passes, scenario.adaptation)
            held = visible[visible < offsets.size - 1]
            pairs.append(
                PairResult(
                    station_index=station_index,
                    satellite_index=satellite_index,
                    passes=passes,
                    segments=segments,
                    modcod_events=events,
                    shannon_bits=integrate_piecewise(
                        offsets[held], offsets[held + 1], shannon_bps[held]
                    ),
                    max_elevation_deg=float(topocentric.elevation_deg[visible].max())
                    if visible.size
                    else math.nan,
                    min_range_m=float(topocentric.range_m[visible].min())
                    if visible.size
                    else math.nan,
                )
            )
        stations.append(pairs)
    return scenario, float(offsets[-1]), stations


def _relative(actual: float, expected: float) -> float:
    return abs(actual - expected) / abs(expected) if expected else abs(actual)


@pytest.mark.parametrize("name", CONSTELLATION_FIXTURES)
def test_grid_left_hold_bits_match_v1(name: str) -> None:
    from openleo.engine.statistics import station_metrics

    scenario, window_s, stations = _grid_left_hold(name)
    v1 = _v1_fixture(name)["station_statistics"]
    assert len(v1) == len(stations)
    fixed_bps = scenario.network.fixed_capacity_bps
    for record, pairs in zip(v1, stations, strict=True):
        metrics = station_metrics(record["station_index"], pairs, window_s, fixed_bps)
        assert (
            _relative(metrics["best_link_integrated_bits"], record["best_link_integrated_bits"])
            <= 1e-9
        ), (name, record["name"], metrics, record)
        assert (
            _relative(
                metrics["fixed_capacity_integrated_bits"], record["fixed_baseline_integrated_bits"]
            )
            <= 1e-9
        ), (name, record["name"], metrics, record)
        # Handovers are not a time measure and match too here; they could differ only by a
        # handover at the last sample, which v1 counted and which holds for no time in v2.
        assert metrics["best_link_handovers"] == record["handover_count"], (name, record["name"])
