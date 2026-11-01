"""Snapshot routing (spec section 6.5): the v1 algorithms and their evaluation on breakpoints."""

import pytest

from openleo.engine import routing


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
