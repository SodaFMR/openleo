"""The v2 bridges against v1's frozen replay preparation, and their events-on behaviour."""

from collections import defaultdict
from dataclasses import fields
from math import floor

import pytest

from openleo.replay import (
    NetworkReplayConfig,
    ReplayConfig,
    ReplaySource,
    prepare_network_replay,
    prepare_packet_replay,
)
from openleo.results import load_results

STEP_NS = 60_000_000_000


@pytest.fixture(scope="module")
def off_source(hydrometeors_off):
    return ReplaySource.from_results(hydrometeors_off)


@pytest.fixture(scope="module")
def on_source(global_on):
    return ReplaySource.from_results(global_on)


def test_bridge_trace_matches_v1_fixture(off_source, v1_hydrometeors):
    expected = v1_hydrometeors["packet_replay_trace"]
    config = ReplayConfig(**expected["metadata"]["configuration"])
    prepared = prepare_packet_replay(off_source, config)
    assert prepared["trace_rows"] == expected["trace_rows"]
    assert prepared["metadata"] == expected["metadata"]


def _v1_rows(fixture, station, satellite, first, last):
    """v1's ``prepare_packet_replay`` rows, rebuilt from the fixture's full-precision links."""
    links = {
        link["time_index"]: link
        for link in fixture["links"]
        if link["station_index"] == station and link["satellite_index"] == satellite
    }
    rows = []
    for index in range(first, last + 1):
        link = links.get(index)
        if link is None:
            rows.append({"time_ns": 0, "rate_bps": 0, "delay_ns": 0, "available": 0})
            continue
        rate = floor(link["rate_bps"])
        rows.append(
            {
                "time_ns": 0,
                "rate_bps": rate,
                "delay_ns": floor(link["delay_s"] * 1_000_000_000 + 0.5),
                "available": int(rate > 0),
            }
        )
    for k, row in enumerate(rows):
        row["time_ns"] = k * STEP_NS
    return rows


def test_bridge_trace_matches_v1_links_on_visible_pairs(off_source, v1_hydrometeors):
    by_pair = defaultdict(list)
    for link in v1_hydrometeors["links"]:
        by_pair[(link["station_index"], link["satellite_index"])].append(link["time_index"])
    compared = 0
    for (station, satellite), indices in sorted(by_pair.items()):
        norad = v1_hydrometeors["satellites"][satellite]["norad_id"]
        low, high = max(0, min(indices) - 2), min(120, max(indices) + 2)
        for first in range(low, high, 50):  # windows of at most 50 minutes (3600 s cap)
            last = min(high, first + 50)
            config = ReplayConfig(
                station_name=off_source.station_names[station],
                norad_id=norad,
                start_offset_s=first * 60,
                duration_s=(last - first) * 60,
                offered_load_bps=10_000,
                packet_size_bytes=1200,
            )
            prepared = prepare_packet_replay(off_source, config)
            assert prepared["trace_rows"] == _v1_rows(
                v1_hydrometeors, station, satellite, first, last
            ), (station, satellite, first)
            compared += sum(row["available"] for row in prepared["trace_rows"])
    assert compared > 50


@pytest.mark.parametrize("model", ["minimum_delay", "maximum_rate", "fixed_capacity"])
def test_network_prepared_matches_v1_fixture(off_source, v1_hydrometeors, model):
    expected = v1_hydrometeors["network_replay_prepared"][model]
    config = NetworkReplayConfig(**expected["metadata"]["configuration"])
    assert config.pair_index == 0
    prepared = prepare_network_replay(off_source, config)
    assert prepared["edges"] == expected["edges"]
    assert prepared["routes"] == expected["routes"]
    metadata, reference = prepared["metadata"], expected["metadata"]
    assert metadata["traffic"] == reference["traffic"]
    assert metadata["window"] == reference["window"]
    assert metadata["selection"] == {**reference["selection"], "pair_index": 0}
    assert metadata["configuration"] == {**reference["configuration"], "pair_index": 0}
    quantization = metadata["quantization"]
    assert quantization["max_rate_floor_error_bps"] == 0.0
    assert quantization["max_delay_round_error_ns"] == pytest.approx(
        reference["quantization"]["max_delay_round_error_ns"], abs=1e-3
    )


def test_network_config_gains_a_pair_index():
    assert [f.name for f in fields(NetworkReplayConfig)][-1] == "pair_index"
    with pytest.raises(ValueError, match="pair_index"):
        prepare_network_replay(object(), NetworkReplayConfig(pair_index=-1))


def _switches(results, station, satellite, start, stop):
    segments = results.tables["rate_segments"]
    passes = results.tables["passes"]
    times = set()
    for k in range(segments.rows):
        if segments["station_index"][k] == station and segments["satellite_index"][k] == satellite:
            times |= {float(segments["start_offset_s"][k]), float(segments["end_offset_s"][k])}
    for k in range(passes.rows):
        if passes["station_index"][k] == station and passes["satellite_index"][k] == satellite:
            times |= {float(passes["aos_offset_s"][k]), float(passes["los_offset_s"][k])}
    return sorted(t for t in times if start < t < stop)


def test_events_on_trace_has_exact_switch_times(on_source, global_on):
    results = load_results(global_on)
    station, satellite = 0, 25
    start, duration = 30, 300
    switches = _switches(results, station, satellite, start, start + duration)
    off_grid = [t for t in switches if t % 60]
    assert len(off_grid) >= 2
    config = ReplayConfig(
        station_name="Madrid",
        norad_id=on_source.norad_ids[satellite],
        start_offset_s=start,
        duration_s=duration,
    )
    prepared = prepare_packet_replay(on_source, config)
    rows = {row["time_ns"]: row for row in prepared["trace_rows"]}
    for t in switches:
        time_ns = floor((t - start) * 1e9 + 0.5)
        assert time_ns in rows, t
        state = on_source.link_state(station, satellite, t)
        row = rows[time_ns]
        if state is None:
            assert (row["rate_bps"], row["available"], row["delay_ns"]) == (0, 0, 0)
        else:
            assert row["rate_bps"] == floor(state[0])
            assert row["delay_ns"] == floor(state[1] * 1e9 + 0.5)
    grid = {k * STEP_NS - start * 1_000_000_000 for k in range(1, 6)}
    assert grid <= set(rows)
    times = [row["time_ns"] for row in prepared["trace_rows"]]
    assert times == sorted(set(times))
    assert times[0] == 0 and times[-1] == duration * 1_000_000_000
    # The rates change inside the window, at the switch times and nowhere else.
    changes = {
        b["time_ns"]
        for a, b in zip(prepared["trace_rows"], prepared["trace_rows"][1:], strict=False)
        if (a["rate_bps"], a["available"]) != (b["rate_bps"], b["available"])
    }
    assert changes <= {floor((t - start) * 1e9 + 0.5) for t in switches}
    assert changes


def test_events_on_network_snapshots_follow_route_breakpoints(on_source, global_on):
    routes = load_results(global_on).tables["routes"]
    starts = sorted(
        {
            float(routes["start_offset_s"][k])
            for k in range(routes.rows)
            if routes["model"][k] == "minimum_delay" and 0 < routes["start_offset_s"][k] < 120
        }
    )
    prepared = prepare_network_replay(
        on_source, NetworkReplayConfig(duration_s=120, offered_load_bps=10_000)
    )
    times = [row["time_ns"] for row in prepared["routes"]]
    assert times == sorted(set(times))
    for t in starts:
        assert floor(t * 1e9 + 0.5) in times
    ready = [row for row in prepared["routes"] if row["state"] == "ready"]
    assert ready and all(row["path"].startswith("80;") for row in ready)
    paths = {row["path"] for row in ready}
    expected = {
        routes["nodes"][k].replace(" ", ";")
        for k in range(routes.rows)
        if routes["model"][k] == "minimum_delay"
        and routes["connected"][k]
        and routes["start_offset_s"][k] < 120
    }
    assert paths == expected
