"""Snapshot routing (spec section 6.5): the v1 algorithms and their evaluation on breakpoints."""

import functools
import gzip
import json
import math
from dataclasses import replace
from itertools import pairwise
from pathlib import Path

import numpy as np
import pytest

from openleo.engine import routing
from openleo.engine.acm import RateSegment
from openleo.engine.events import Pass
from openleo.engine.statistics import PairResult
from openleo.physics.link import propagation_delay_s
from openleo.scenario.model import ROUTE_MODELS, IslConfig, NetworkConfig, RoutePair

ROOT = Path(__file__).resolve().parents[2]
V1_FIXTURES = ROOT / "tests" / "fixtures" / "v1"
V1_ISL_EDGES = V1_FIXTURES / "isl_edges.json"
CONSTELLATION_FIXTURES = (
    "iridium_global",
    "iridium_global_reference",
    "iridium_global_hydrometeors",
)


def _example_path(name: str) -> Path:
    """A shipped example scenario: the ISS single pass or one of the Iridium constellations."""
    folder = "scenarios" if name == "iss_cartagena" else "constellations"
    return ROOT / "examples" / folder / f"{name}.json"


def _graph(size, edges):
    return tuple(
        tuple((b if a == node else a, delay, rate) for a, b, delay, rate in edges if node in (a, b))
        for node in range(size)
    )


def test_diamond_routes_match_hand_calculation_and_are_reciprocal():
    graph = _graph(4, [(0, 1, 1.0, 20.0), (1, 3, 1.0, 20.0), (0, 2, 2.0, 100.0), (2, 3, 2.0, 80.0)])
    assert routing.shortest_path(graph, 0, 3) == {
        "nodes": [0, 1, 3],
        "delay_s": 2.0,
        "bottleneck_bps": 20.0,
    }
    assert routing.widest_path(graph, 0, 3) == {
        "nodes": [0, 2, 3],
        "delay_s": 4.0,
        "bottleneck_bps": 80.0,
    }
    assert routing.widest_path(graph, 3, 0)["nodes"] == [3, 2, 0]


def test_equal_widest_bottlenecks_choose_global_minimum_delay():
    # The narrow final edge makes both prefixes eligible. Retaining only the
    # widest prefix at node 3 would wrongly choose the ten-second path.
    graph = _graph(
        5,
        [
            (0, 1, 5.0, 100.0),
            (1, 3, 5.0, 100.0),
            (0, 2, 1.0, 50.0),
            (2, 3, 1.0, 50.0),
            (3, 4, 1.0, 10.0),
        ],
    )
    assert routing.widest_path(graph, 0, 4) == {
        "nodes": [0, 2, 3, 4],
        "delay_s": 3.0,
        "bottleneck_bps": 10.0,
    }


def test_ties_are_deterministic_and_zero_delay_cycles_are_not_routes():
    edges = [
        (0, 2, 1.0, 10.0),
        (2, 3, 1.0, 10.0),
        (0, 1, 1.0, 10.0),
        (1, 3, 1.0, 10.0),
        (1, 2, 0.0, 10.0),
    ]
    for ordered_edges in (edges, list(reversed(edges))):
        graph = _graph(4, ordered_edges)
        for route in (routing.shortest_path(graph, 0, 3), routing.widest_path(graph, 0, 3)):
            assert route["nodes"] == [0, 1, 3]
            assert route["delay_s"] == 2.0


def test_disconnected_graph_has_no_route():
    graph = _graph(4, [(0, 1, 1.0, 10.0), (2, 3, 1.0, 10.0)])
    assert routing.shortest_path(graph, 0, 3) is None
    assert routing.widest_path(graph, 0, 3) is None


@pytest.mark.parametrize(
    ("first", "second", "visible"),
    [
        ([7_000_000, 0, 0], [7_000_000, 100_000, 0], True),
        ([7_000_000, 0, 0], [-7_000_000, 0, 0], False),
        ([-1_000_000, 0, 6_360_000], [1_000_000, 0, 6_360_000], True),
        ([6_360_000, -1_000_000, 0], [6_360_000, 1_000_000, 0], False),
        ([6_378_137, -1_000_000, 0], [6_378_137, 1_000_000, 0], False),
        ([7_000_000, 0, 0], [7_000_000, 0, 0], True),
        ([0, 0, 0], [7_000_000, 0, 0], False),
    ],
)
def test_earth_occlusion_uses_wgs84_ellipsoid_including_tangency(first, second, visible):
    assert routing.has_line_of_sight(first, second) is visible


# --- evaluation on breakpoints ----------------------------------------------------------------

# Two satellites 100 km apart (nodes 0 and 1), held still: one ISL of 100 bit/s.
STILL_ECEF_M = np.array([[7_000_000.0, 0.0, 0.0], [7_000_000.0, 100_000.0, 0.0]])
ISL_DELAY_S = propagation_delay_s(100_000.0)
NAMES = ("Source", "Target", "Other")  # nodes 2, 3 and 4


def _network(pairs=(("Source", "Target"),), *, max_range_m=200_000.0, fixed_bps=20.0, models=None):
    return NetworkConfig(
        pairs=tuple(RoutePair(source, target) for source, target in pairs),
        isl=IslConfig("all_pairs_within_range", max_range_m, 100.0),
        fixed_capacity_bps=fixed_bps,
        route_models=ROUTE_MODELS if models is None else tuple(models),
    )


def _still(times):
    return np.broadcast_to(STILL_ECEF_M, (len(times), 2, 3)).copy()


def _schedule(table):
    """A LinkState from {(station, satellite): [(start, end, rate, delay), ...]}, half-open."""

    def state(station, satellite, t):
        for start, end, rate, delay in table.get((station, satellite), ()):
            if start <= t < end:
                return rate, delay
        return None

    return state


def _rows(intervals, model):
    return [
        (i.start_s, i.end_s, i.nodes, i.delay_s, i.bottleneck_bps)
        for i in intervals
        if i.model == model
    ]


def test_hand_graph_breakpoints():
    # Source sees satellite 0 at 40, then 0 bit/s (no lock), then 80 bit/s; it also sees
    # satellite 1 directly on [20, 30) and [35, 50). Target sees satellite 1 from 5 s. The
    # third station sees both satellites at a high rate and must never relay.
    table = {
        (0, 0): [(0.0, 10.0, 40.0, 0.01), (10.0, 20.0, 0.0, 0.01), (20.0, 30.0, 80.0, 0.01)],
        (0, 1): [(20.0, 30.0, 30.0, 0.005), (35.0, 50.0, 60.0, 0.03)],
        (1, 1): [(5.0, 50.0, 40.0, 0.02)],
        (2, 0): [(0.0, 50.0, 1e6, 0.001)],
        (2, 1): [(0.0, 50.0, 1e6, 0.001)],
    }
    breakpoints = np.array([0.0, 5.0, 10.0, 20.0, 30.0, 35.0, 50.0])
    intervals = routing.evaluate_routes(
        _network(), NAMES, _still, _schedule(table), breakpoints, satellite_count=2
    )
    via = 0.01 + ISL_DELAY_S + 0.02  # 2 -> 0 -> 1 -> 3
    direct, late = 0.005 + 0.02, 0.03 + 0.02  # 2 -> 1 -> 3
    assert {i.pair_index for i in intervals} == {0}
    assert _rows(intervals, "minimum_delay") == [
        (0.0, 5.0, None, None, None),
        (5.0, 10.0, (2, 0, 1, 3), via, 40.0),
        (10.0, 20.0, None, None, None),  # a 0 bit/s ground link is no edge
        (20.0, 30.0, (2, 1, 3), direct, 30.0),
        (30.0, 35.0, None, None, None),
        (35.0, 50.0, (2, 1, 3), late, 40.0),
    ]
    assert _rows(intervals, "maximum_rate") == [
        (0.0, 5.0, None, None, None),
        (5.0, 10.0, (2, 0, 1, 3), via, 40.0),
        (10.0, 20.0, None, None, None),
        (20.0, 30.0, (2, 0, 1, 3), via, 40.0),
        (30.0, 35.0, None, None, None),
        (35.0, 50.0, (2, 1, 3), late, 40.0),
    ]
    # The fixed baseline keeps every visible ground link, at 20 bit/s; equal neighbours merge.
    assert _rows(intervals, "fixed_capacity") == [
        (0.0, 5.0, None, None, None),
        (5.0, 20.0, (2, 0, 1, 3), via, 20.0),
        (20.0, 30.0, (2, 1, 3), direct, 20.0),
        (30.0, 35.0, None, None, None),
        (35.0, 50.0, (2, 1, 3), late, 20.0),
    ]
    metrics = routing.route_metrics(intervals, 50.0)
    assert list(metrics) == [(0, model) for model in ROUTE_MODELS]
    expected = {
        "minimum_delay": (0.6, 1100.0, 0, [(5.0, via), (10.0, direct), (15.0, late)]),
        "maximum_rate": (0.6, 1200.0, 0, [(15.0, via), (15.0, late)]),
        "fixed_capacity": (0.8, 800.0, 1, [(15.0, via), (10.0, direct), (15.0, late)]),
    }
    for model, (fraction, bits, changes, delays) in expected.items():
        connected = math.fsum(seconds for seconds, _ in delays)
        assert metrics[(0, model)] == {
            "connected_fraction": fraction,
            "integrated_bottleneck_bits": bits,
            "route_changes": changes,
            "mean_delay_connected_s": pytest.approx(
                math.fsum(seconds * delay for seconds, delay in delays) / connected, rel=1e-15
            ),
        }

    # Without ISLs the third station still cannot relay between the two satellites: only the
    # direct routes through satellite 1 remain.
    isolated = routing.evaluate_routes(
        _network(max_range_m=0.0), NAMES, _still, _schedule(table), breakpoints, 2
    )
    assert {i.nodes for i in isolated} == {None, (2, 1, 3)}


def test_routes_hold_the_result_at_each_breakpoint_and_move_satellites():
    # Satellite 1 drifts out of ISL range: within range at 0 and 10 s, beyond it at 20 s. The
    # route is evaluated at each breakpoint and held until the next one.
    def moving(times):
        positions = _still(times)
        positions[:, 1, 1] = 100_000.0 + 10_000.0 * np.asarray(times)
        return positions

    table = {(0, 0): [(0.0, 30.0, 40.0, 0.01)], (1, 1): [(0.0, 30.0, 40.0, 0.02)]}
    intervals = routing.evaluate_routes(
        _network(pairs=(("Target", "Source"),), models=("minimum_delay",), fixed_bps=None),
        NAMES,
        moving,
        _schedule(table),
        np.array([0.0, 10.0, 20.0, 30.0]),
        2,
    )
    assert _rows(intervals, "minimum_delay") == [
        (0.0, 10.0, (3, 1, 0, 2), 0.02 + ISL_DELAY_S + 0.01, 40.0),
        (10.0, 20.0, (3, 1, 0, 2), 0.02 + propagation_delay_s(200_000.0) + 0.01, 40.0),
        (20.0, 30.0, None, None, None),
    ]


def test_isl_edges_are_v1_edges_at_range_and_tangency_thresholds():
    rng = np.random.default_rng(11)
    directions = rng.normal(size=(60, 3))
    directions /= np.linalg.norm(directions, axis=1)[:, None]
    shell = directions * rng.uniform(6.9e6, 7.4e6, size=(60, 1))
    crafted = np.array(
        [
            [7_000_000.0, 0.0, 0.0],
            [7_000_000.0, 3_000_000.0, 0.0],  # exactly at the maximum range: an edge (v1 "<=")
            [6_378_137.0, -1_000_000.0, 0.0],
            [6_378_137.0, 1_000_000.0, 0.0],  # tangent to the equator: occluded (v1)
            [7_000_000.0, 0.0, 0.0],  # coincident with the first: range 0, no edge
            [-1_000_000.0, 0.0, 6_360_000.0],
            [1_000_000.0, 0.0, 6_360_000.0],  # clears the flattened pole
            [6_200_000.0, -1_400_000.0, 1_000_000.0],
            [6_200_000.0, 1_400_000.0, 1_000_000.0],  # in range, through the Earth
        ]
    )
    positions = np.concatenate([crafted, shell])
    # Frozen oracle: v1.0.0 ``openleo.network._frame(positions, (), NetworkConfig("A", "B",
    # 3e6, 1.0, 1.0), 72, 69, 70)["isl_edges"]`` on these positions, run in a v1.0.0 checkout.
    expected = json.loads(V1_ISL_EDGES.read_text(encoding="utf-8"))
    edges = routing._isl_edges(positions, 3_000_000.0)
    assert len(expected) == 92
    # Edge membership and order must match v1 exactly; delays are range / c, so a platform's
    # libm may differ in the last bit (seen on macOS). Hold them to 1e-13 s, the agreed v1 delay
    # tolerance.
    assert [edge[:2] for edge in edges] == [tuple(edge[:2]) for edge in expected]
    np.testing.assert_allclose(
        [edge[2] for edge in edges], [edge[2] for edge in expected], rtol=0, atol=1e-13
    )
    assert (0, 1) in {edge[:2] for edge in edges}
    assert (2, 3) not in {edge[:2] for edge in edges}
    assert (0, 4) not in {edge[:2] for edge in edges}
    assert (5, 6) in {edge[:2] for edge in edges}
    assert (7, 8) not in {edge[:2] for edge in edges}
    assert routing._isl_edges(positions, 0.0) == []


def test_route_inputs_are_validated():
    graph = _graph(3, [(0, 1, 1.0, 1.0)])
    for source, target in ((0, 3), (True, 1), (1, 1)):
        with pytest.raises(ValueError, match="endpoint"):
            routing.shortest_path(graph, source, target)
    with pytest.raises(ValueError, match="three ECEF"):
        routing.has_line_of_sight([7e6, 0.0], [7e6, 0.0, 0.0])
    with pytest.raises(ValueError, match="itself"):
        routing.evaluate_routes(
            _network(pairs=(("Source", "Source"),)),
            NAMES,
            _still,
            lambda station, satellite, t: None,
            np.array([0.0, 1.0]),
            2,
        )
    pair = _pair(0, 1, [], [])
    with pytest.raises(ValueError, match="more than once"):
        routing.link_state_from_pairs([pair, pair], lambda station, satellite, t: 0.01)


def test_route_changes_ignore_outages():
    a, b = (4, 0, 5), (4, 1, 5)
    routes = [a, b, None, a, a, b]  # one change, then an outage that hides a change
    intervals = [
        routing.RouteInterval(
            0,
            "minimum_delay",
            10.0 * k,
            10.0 * (k + 1),
            nodes,
            *((0.01, 50.0) if nodes else (None, None)),
        )
        for k, nodes in enumerate(routes)
    ]
    metrics = routing.route_metrics(intervals, 60.0)[(0, "minimum_delay")]
    assert metrics["route_changes"] == 2  # a -> b, and a -> b at the end; not b, outage, a
    assert metrics["connected_fraction"] == 50.0 / 60.0
    assert metrics["integrated_bottleneck_bits"] == 2500.0
    assert metrics["mean_delay_connected_s"] == pytest.approx(0.01, rel=1e-15)
    # v1's rule on consecutive samples (v1.0.0 ``openleo.network._summary``): count node changes
    # between consecutive samples that are both connected.
    v1_changes = sum(
        first != second
        for first, second in pairwise(routes)
        if first is not None and second is not None
    )
    assert v1_changes == metrics["route_changes"]


def test_never_connected_pair():
    intervals = routing.evaluate_routes(
        _network(pairs=(("Source", "Target"), ("Other", "Target"))),
        NAMES,
        _still,
        lambda station, satellite, t: None,
        np.array([0.0, 60.0, 100.0]),
        2,
    )
    assert [(i.pair_index, i.model, i.start_s, i.end_s, i.nodes) for i in intervals] == [
        (pair, model, 0.0, 100.0, None) for pair in (0, 1) for model in ROUTE_MODELS
    ]
    metrics = routing.route_metrics(intervals, 100.0)
    for key in ((0, "maximum_rate"), (1, "fixed_capacity")):
        record = metrics[key]
        assert record["connected_fraction"] == 0.0
        assert record["integrated_bottleneck_bits"] == 0.0
        assert record["route_changes"] == 0
        assert math.isnan(record["mean_delay_connected_s"])
    # Writing NaN as an empty CSV cell belongs to the results writer (openleo.results).


def _pair(station, satellite, passes, segments):
    return PairResult(station, satellite, tuple(passes), tuple(segments), (), 0.0, 1.0, 1.0)


def test_route_breakpoints_join_grid_and_endpoint_station_boundaries():
    pairs = [
        _pair(
            0,
            0,
            [Pass(12.5, 47.25, 30.0, 40.0, False, False)],
            [RateSegment(12.5, 20.0, None, 0.0), RateSegment(20.0, 47.25, "QPSK 1/4", 1.0)],
        ),
        _pair(
            1,
            3,
            [Pass(80.0, 100.0, 100.0, 20.0, False, True)],
            [RateSegment(80.0, 100.0, None, 0.0)],
        ),
        _pair(2, 1, [Pass(5.5, 7.5, 6.0, 11.0, False, False)], []),  # not an endpoint station
    ]
    grid = np.array([0.0, 60.0, 100.0])
    assert routing.route_breakpoints(grid, pairs, {0, 1}, 100.0).tolist() == [
        0.0,
        12.5,
        20.0,
        47.25,
        60.0,
        80.0,
        100.0,
    ]
    assert routing.route_breakpoints(grid, pairs, set(), 100.0).tolist() == grid.tolist()
    with pytest.raises(ValueError, match="window_s"):
        routing.route_breakpoints(grid, pairs, {0}, 0.0)
    with pytest.raises(ValueError, match="grid_s"):
        routing.route_breakpoints(np.array([0.0, np.nan]), pairs, {0}, 100.0)


def test_link_state_from_pairs_is_half_open_and_reads_segment_rates():
    pairs = [
        _pair(
            1,
            4,
            [
                Pass(10.0, 30.0, 20.0, 40.0, False, False),
                Pass(50.0, 50.0, 50.0, 10.0, False, False),
            ],
            [RateSegment(10.0, 15.0, None, 0.0), RateSegment(15.0, 30.0, "QPSK 1/4", 490243.0)],
        )
    ]
    calls = []

    def delay(station, satellite, t):
        calls.append((station, satellite, t))
        return 0.004 + t * 1e-6

    state = routing.link_state_from_pairs(pairs, delay)
    assert state(1, 4, 9.999) is None
    assert state(1, 4, 10.0) == (0.0, 0.004 + 10.0 * 1e-6)
    assert state(1, 4, 15.0) == (490243.0, 0.004 + 15.0 * 1e-6)
    assert state(1, 4, 30.0) is None  # LOS is excluded
    assert state(1, 4, 50.0) is None  # a zero-duration pass holds no time
    assert state(0, 4, 20.0) is None  # an unknown pair is never visible
    assert calls == [(1, 4, 10.0), (1, 4, 15.0)]


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"network": _network(pairs=(("Source", "Nowhere"),))}, "Nowhere"),
        ({"network": _network(fixed_bps=None)}, "fixed_capacity_bps"),
        ({"breakpoints_s": np.array([0.0])}, "breakpoints"),
        ({"breakpoints_s": np.array([0.0, 10.0, 10.0])}, "increasing"),
        ({"breakpoints_s": np.array([0.0, np.inf])}, "finite"),
        ({"satellite_ecef": lambda t: np.zeros((len(t), 3, 3))}, "shape"),
        ({"satellite_ecef": lambda t: np.zeros((len(t), 2, 3))}, "ellipsoid"),
        ({"satellite_ecef": lambda t: np.full((len(t), 2, 3), np.nan)}, "finite"),
        ({"link_state": lambda s, k, t: (-1.0, 0.01)}, "rate_bps"),
        ({"link_state": lambda s, k, t: (1.0, 0.0)}, "delay_s"),
        ({"satellite_count": 0}, "satellite_count"),
        ({"network": _network(models=("minimum_hops",))}, "minimum_hops"),
    ],
)
def test_evaluate_routes_rejects_bad_inputs(kwargs, message):
    arguments = {
        "network": _network(),
        "station_names": NAMES,
        "satellite_ecef": _still,
        "link_state": lambda station, satellite, t: (1.0, 0.01),
        "breakpoints_s": np.array([0.0, 10.0]),
        "satellite_count": 2,
        **kwargs,
    }
    with pytest.raises(ValueError, match=message):
        routing.evaluate_routes(**arguments)


def test_route_metrics_rejects_bad_intervals():
    good = routing.RouteInterval(0, "minimum_delay", 0.0, 10.0, (2, 0, 3), 0.01, 5.0)
    with pytest.raises(ValueError, match="window_s"):
        routing.route_metrics([good], -1.0)
    with pytest.raises(ValueError, match="overlap"):
        routing.route_metrics([good, replace(good, start_s=5.0, end_s=12.0)], 20.0)
    with pytest.raises(ValueError, match="window"):
        routing.route_metrics([replace(good, end_s=30.0)], 20.0)
    with pytest.raises(ValueError, match="delay_s"):
        routing.route_metrics([replace(good, delay_s=None)], 20.0)
    assert routing.route_metrics([], 20.0) == {}


# --- events off: v1 equivalence ---------------------------------------------------------------


@functools.cache
def _v1_fixture(name: str) -> dict:
    return json.loads(gzip.decompress((V1_FIXTURES / f"{name}.json.gz").read_bytes()))


@functools.cache
def _grid_routes(name: str):
    """Routes of the v2 twin of a v1 example with events disabled, on the grid as breakpoints."""
    from openleo.engine.acm import acm_on_grid
    from openleo.engine.catalogs import load_catalogs
    from openleo.engine.evaluator import LinkEvaluator
    from openleo.engine.events import passes_on_grid
    from openleo.engine.geometry import Geometry
    from openleo.engine.timegrid import build_grid, builtin_timescale
    from openleo.scenario import load_scenario

    scenario = load_scenario(_example_path(name))
    scenario = replace(
        scenario, time=replace(scenario.time, events=replace(scenario.time.events, enabled=False))
    )
    timescale = builtin_timescale()
    orbits = load_catalogs(scenario.space_segment, timescale)
    grid = build_grid(scenario.time)
    offsets = grid.offsets_s
    stations = scenario.ground_segment.stations
    names = tuple(station.name for station in stations)
    geometry = Geometry(orbits, stations, timescale, grid.start_utc)
    evaluator = LinkEvaluator(scenario, geometry)
    endpoints = sorted(
        {
            names.index(name)
            for pair in scenario.network.pairs
            for name in (pair.source, pair.target)
        }
    )
    pairs, delays = [], {}
    for satellite_index in range(len(orbits)):
        for station_index in endpoints:
            station = stations[station_index]
            mask_deg = (
                station.minimum_elevation_deg
                if station.minimum_elevation_deg is not None
                else scenario.ground_segment.default_minimum_elevation_deg
            )
            elevation_deg = geometry.elevation_deg(station_index, satellite_index, offsets)
            passes = passes_on_grid(elevation_deg, offsets, mask_deg)
            visible = np.flatnonzero(elevation_deg >= mask_deg)
            available_db = np.full(offsets.size, np.nan)
            delay_s = np.full(offsets.size, np.nan)
            if visible.size:
                link = evaluator.evaluate(station_index, satellite_index, offsets[visible])
                available_db[visible] = (
                    link["esn0_db"] - scenario.adaptation.implementation_margin_db
                )
                delay_s[visible] = link["delay_s"]
            segments, events = acm_on_grid(available_db, offsets, passes, scenario.adaptation)
            pairs.append(
                PairResult(station_index, satellite_index, passes, segments, events, 0.0, 0.0, 0.0)
            )
            delays[(station_index, satellite_index)] = delay_s

    def grid_delay(station_index, satellite_index, t):
        k = int(np.searchsorted(offsets, t))
        assert offsets[k] == t
        return float(delays[(station_index, satellite_index)][k])

    def satellite_ecef(times):
        return np.stack(
            [geometry.satellite_ecef(index, times)[0] for index in range(len(orbits))], axis=1
        )

    window_s = float(offsets[-1])
    breakpoints = routing.route_breakpoints(offsets, pairs, set(endpoints), window_s)
    intervals = routing.evaluate_routes(
        scenario.network,
        names,
        satellite_ecef,
        routing.link_state_from_pairs(pairs, grid_delay),
        breakpoints,
        len(orbits),
    )
    return offsets, breakpoints, intervals


@pytest.mark.parametrize("name", CONSTELLATION_FIXTURES)
def test_grid_mode_matches_v1_routes(name: str) -> None:
    """Every v1 frame but the last holds its route for one step; the last holds no time."""
    offsets, breakpoints, intervals = _grid_routes(name)
    assert breakpoints.tolist() == offsets.tolist()  # events off: boundaries are grid samples
    v1 = _v1_fixture(name)
    frames = {(r["model"], r["time_index"]): r for r in v1["routes"]}
    for model in ROUTE_MODELS:
        rows = [i for i in intervals if i.model == model]
        assert {i.pair_index for i in rows} == {0}
        starts = np.array([i.start_s for i in rows])
        assert rows[-1].end_s == offsets[-1]
        for k, t in enumerate(offsets[:-1].tolist()):
            interval = rows[int(np.searchsorted(starts, t, side="right")) - 1]
            assert interval.start_s <= t < interval.end_s
            record = frames[(model, k)]
            if record["nodes"] is None:
                assert interval.nodes is None, (name, model, k)
                continue
            assert interval.nodes == tuple(record["nodes"]), (name, model, k)
            assert abs(interval.delay_s - record["delay_s"]) <= 1e-13, (name, model, k)
            assert interval.bottleneck_bps == record["bottleneck_bps"], (name, model, k)
        metrics = routing.route_metrics(intervals, float(offsets[-1]))[(0, model)]
        summary = v1["network_summary"][model]
        expected_bits = summary["integrated_bottleneck_bits"]
        assert abs(metrics["integrated_bottleneck_bits"] - expected_bits) <= 1e-9 * expected_bits
        # Route changes match v1's rule on the frames that hold time (v1 also compared the last
        # sample, which holds none).
        held = [frames[(model, k)]["nodes"] for k in range(offsets.size - 1)]
        assert metrics["route_changes"] == sum(
            1 for a, b in pairwise(held) if a is not None and b is not None and a != b
        )


def test_the_route_models_have_one_definition():
    from openleo import sweep
    from openleo.replay import source
    from openleo.scenario import upgrade

    assert tuple(routing._ALGORITHMS) == ROUTE_MODELS
    assert sweep.ROUTE_MODELS is source.ROUTE_MODELS is upgrade.ROUTE_MODELS is ROUTE_MODELS
