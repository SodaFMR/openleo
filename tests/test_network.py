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
