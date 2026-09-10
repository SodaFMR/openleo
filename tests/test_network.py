"""Hand-derived routing and ellipsoid regressions, independent of orbit propagation."""

from copy import deepcopy
from dataclasses import FrozenInstanceError, asdict
from math import inf, nan

import pytest

from openleo import network


def _config(**overrides):
    return {
        "source_station": "Source",
        "target_station": "Target",
        "isl_max_range_m": 200_000.0,
        "isl_capacity_bps": 100.0,
        "fixed_capacity_bps": 20.0,
        **overrides,
    }


def _graph(size, edges):
    return tuple(
        tuple((b if a == node else a, delay, rate) for a, b, delay, rate in edges if node in (a, b))
        for node in range(size)
    )


def _link(station, satellite, rate=40.0, delay=0.01):
    return {
        "station_index": station,
        "satellite_index": satellite,
        "delay_s": delay,
        "rate_bps": rate,
    }


def _document():
    return {
        "schema_version": "1",
        "kind": "openleo.constellation",
        "scenario": {"network": _config()},
        "timestamps_utc": [
            "2026-09-10T12:00:00Z",
            "2026-09-10T12:00:10Z",
            "2026-09-10T12:00:30Z",
        ],
        "stations": [{"name": "Source"}, {"name": "Target"}],
        "satellites": [
            {"positions_ecef_m": [[7_000_000.0, 0.0, 0.0]] * 3},
            {"positions_ecef_m": [[7_000_000.0, 100_000.0, 0.0]] * 3},
        ],
        "links": [
            [_link(0, 0), _link(1, 1)],
            [_link(0, 0, rate=0.0), _link(1, 1)],
            [_link(0, 1, rate=80.0), _link(1, 1, rate=80.0)],
        ],
    }


def test_network_config_is_frozen_and_zero_range_disables_isls():
    config = network.parse_network(_config(isl_max_range_m=0.0), ["Source", "Target"])
    assert asdict(config) == _config(isl_max_range_m=0.0)
    with pytest.raises(FrozenInstanceError):
        config.isl_capacity_bps = 0.0


@pytest.mark.parametrize(
    "config",
    [
        {key: value for key, value in _config().items() if key != "isl_capacity_bps"},
        _config(extra=True),
        _config(source_station="Missing"),
        _config(target_station="Source"),
        _config(source_station=""),
        _config(isl_max_range_m=-1),
        _config(isl_capacity_bps=0),
        _config(fixed_capacity_bps=-1),
        _config(fixed_capacity_bps=inf),
        _config(isl_capacity_bps=nan),
        _config(isl_max_range_m=True),
        _config(isl_max_range_m="1000"),
        [],
    ],
)
def test_rejects_malformed_network_config(config):
    with pytest.raises(ValueError):
        network.parse_network(config, ["Source", "Target"])


def test_diamond_routes_match_hand_calculation_and_are_reciprocal():
    graph = _graph(4, [(0, 1, 1.0, 20.0), (1, 3, 1.0, 20.0), (0, 2, 2.0, 100.0), (2, 3, 2.0, 80.0)])
    assert network.shortest_path(graph, 0, 3) == {
        "nodes": [0, 1, 3],
        "delay_s": 2.0,
        "bottleneck_bps": 20.0,
    }
    assert network.widest_path(graph, 0, 3) == {
        "nodes": [0, 2, 3],
        "delay_s": 4.0,
        "bottleneck_bps": 80.0,
    }
    assert network.widest_path(graph, 3, 0)["nodes"] == [3, 2, 0]


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
    assert network.widest_path(graph, 0, 4) == {
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
        for route in (network.shortest_path(graph, 0, 3), network.widest_path(graph, 0, 3)):
            assert route["nodes"] == [0, 1, 3]
            assert route["delay_s"] == 2.0


def test_disconnected_graph_has_no_route():
    graph = _graph(4, [(0, 1, 1.0, 10.0), (2, 3, 1.0, 10.0)])
    assert network.shortest_path(graph, 0, 3) is None
    assert network.widest_path(graph, 0, 3) is None


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
    assert network.has_line_of_sight(first, second) is visible


def test_snapshot_routes_keep_rf_outage_separate_from_fixed_baseline_without_mutation():
    document = _document()
    before = deepcopy(document)
    result = network.add_network(document)
    assert document == before
    assert result is not document
    assert set(result) == set(document) | {"network"}
    output = result["network"]
    assert output["source_station_index"] == 0
    assert output["target_station_index"] == 1
    first, outage, recovered = output["frames"]
    assert first["isl_edges"][0][:2] == [0, 1]
    assert first["isl_edges"][0][2] == pytest.approx(0.00033356409519815205)
    assert first["minimum_delay"] == {
        "nodes": [2, 0, 1, 3],
        "delay_s": pytest.approx(0.020333564095198153),
        "bottleneck_bps": 40.0,
    }
    assert outage["minimum_delay"] is None
    assert outage["maximum_rate"] is None
    assert outage["fixed_capacity"]["bottleneck_bps"] == 20.0
    assert recovered["maximum_rate"]["nodes"] == [2, 1, 3]
    for model in ("minimum_delay", "maximum_rate"):
        assert output["summary"][model] == {
            "connected_sample_fraction": pytest.approx(2 / 3),
            "integrated_bottleneck_bits": 400.0,
            "route_changes": 0,
        }
    assert output["summary"]["fixed_capacity"] == {
        "connected_sample_fraction": 1.0,
        "integrated_bottleneck_bits": 600.0,
        "route_changes": 1,
    }
    assert result == network.add_network(document)


def test_isls_obey_declared_range_and_earth_occlusion():
    document = _document()
    short = {**document, "scenario": {"network": _config(isl_max_range_m=99_999)}}
    assert network.add_network(short)["network"]["frames"][0]["minimum_delay"] is None
    assert network.add_network(short)["network"]["frames"][0]["isl_edges"] == []
    opposite = {
        **document,
        "scenario": {"network": _config(isl_max_range_m=20_000_000)},
        "satellites": [
            document["satellites"][0],
            {"positions_ecef_m": [[-7_000_000.0, 0.0, 0.0]] * 3},
        ],
    }
    assert network.add_network(opposite)["network"]["frames"][0]["isl_edges"] == []


def test_fixed_baseline_retains_the_same_isl_capacity_as_adaptive_models():
    document = _document()
    result = network.add_network(
        {
            **document,
            "scenario": {"network": _config(isl_capacity_bps=5.0)},
        }
    )
    for model in ("minimum_delay", "maximum_rate", "fixed_capacity"):
        assert result["network"]["frames"][0][model]["bottleneck_bps"] == 5.0


def test_other_ground_stations_cannot_supply_implicit_satellite_relays():
    document = _document()
    result = network.add_network(
        {
            **document,
            "scenario": {"network": _config(isl_max_range_m=0.0)},
            "stations": [*document["stations"], {"name": "Other"}],
            "links": [[_link(0, 0), _link(1, 1), _link(2, 0), _link(2, 1)]] * 3,
        }
    )
    for frame in result["network"]["frames"]:
        for model in ("minimum_delay", "maximum_rate", "fixed_capacity"):
            assert frame[model] is None


@pytest.mark.parametrize("field", ["delay_s", "rate_bps"])
def test_rejects_overflow_in_derived_route_metrics(field):
    document = _document()
    links = [[{**_link(0, 1), field: 1e308}, {**_link(1, 1), field: 1e308}]] * 3
    with pytest.raises(ValueError, match="finite"):
        network.add_network({**document, "links": links})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("kind", "other"),
        ("timestamps_utc", []),
        ("timestamps_utc", ["2026-09-10T12:00:00Z"] * 3),
        ("timestamps_utc", ["2026-09-10T12:00:00"] * 3),
        ("satellites", []),
        ("satellites", [{"positions_ecef_m": [[7_000_000, 0, 0]]}]),
        ("satellites", [{"positions_ecef_m": [[0, 0, 0]] * 3}]),
        ("satellites", [{"positions_ecef_m": [[nan, 0, 0]] * 3}]),
        ("stations", [{"name": "Source"}, {"name": "Source"}]),
        ("links", []),
        ("links", [[_link(0, 9)]] * 3),
        ("links", [[_link(True, 0)]] * 3),
        ("links", [[_link(0, 0, rate=-1)]] * 3),
        ("links", [[_link(0, 0, delay=0)]] * 3),
        ("links", [[_link(0, 0), _link(0, 0)]] * 3),
    ],
)
def test_rejects_malformed_snapshot_inputs(field, value):
    with pytest.raises(ValueError):
        network.add_network({**_document(), field: value})
