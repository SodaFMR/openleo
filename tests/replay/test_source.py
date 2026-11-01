import json
import math
import shutil
from datetime import UTC, datetime

import numpy as np
import pytest

from openleo.engine.routing import _isl_edges
from openleo.replay import ReplaySource
from openleo.results import load_results, write_manifest


@pytest.fixture(scope="module")
def off_source(hydrometeors_off):
    return ReplaySource.from_results(hydrometeors_off)


@pytest.fixture(scope="module")
def on_source(global_on):
    return ReplaySource.from_results(global_on)


def test_source_exposes_the_run_identity(off_source, hydrometeors_off):
    results = load_results(hydrometeors_off)
    assert off_source.start_utc == datetime(2026, 9, 10, 12, tzinfo=UTC)
    assert off_source.window_s == 7200.0
    assert off_source.station_names == ("Madrid", "Tromso", "Singapore", "Quito")
    assert off_source.norad_ids == tuple(results.tables["satellites"]["norad_id"].tolist())
    assert off_source.satellite_count == 80
    assert off_source.events_enabled is False
    assert off_source.route_pairs == ((0, 1),)
    assert off_source.route_models == ("minimum_delay", "maximum_rate", "fixed_capacity")


def test_events_off_link_state_is_the_sample_state(off_source, hydrometeors_off):
    samples = load_results(hydrometeors_off).tables["samples"]
    station = samples["station_index"].tolist()
    satellite = samples["satellite_index"].tolist()
    times = samples["t_offset_s"].tolist()
    rates = samples["rate_bps"].tolist()
    delays = samples["delay_s"].tolist()
    checked = 0
    for k in range(0, len(times), 7):
        if times[k] == off_source.window_s:
            continue
        rate, delay = off_source.link_state(station[k], satellite[k], times[k])
        assert rate == rates[k]
        assert delay == pytest.approx(delays[k], abs=1e-15)
        # Visibility and rate are held between samples, as v1 held them; the delay is
        # evaluated where the link is above the mask, else held from the sample.
        later = min(times[k] + 59.5, off_source.window_s - 0.5)
        held_rate, held_delay = off_source.link_state(station[k], satellite[k], later)
        assert held_rate == rate
        elevation = off_source.geometry.elevation_deg(station[k], satellite[k], [later])[0]
        if elevation >= 10.0:
            expected = off_source.evaluator.evaluate(station[k], satellite[k], [later])["delay_s"]
            assert held_delay == expected[0]
        else:
            assert held_delay == delay
        checked += 1
    assert checked > 100
    visible = set(zip(station, satellite, times, strict=True))
    assert (0, 3, 0.0) not in visible
    assert off_source.link_state(0, 3, 0.0) is None


def test_routes_come_from_the_routes_table(on_source, global_on):
    routes = load_results(global_on).tables["routes"]
    for index in range(routes.rows):
        model = routes["model"][index]
        start = float(routes["start_offset_s"][index])
        end = float(routes["end_offset_s"][index])
        nodes = routes["nodes"][index]
        expected = tuple(int(n) for n in nodes.split()) if nodes else None
        for t in (start, (start + end) / 2):
            assert on_source.route(0, model, t) == expected


def test_isl_edges_follow_the_engine_rule(on_source):
    t = 123.456
    count = on_source.satellite_count
    positions = np.array(
        [on_source.geometry.satellite_ecef(s, [t])[0][0] for s in range(count)], dtype=np.float64
    )
    engine = {(a, b): delay for a, b, delay in _isl_edges(positions, 5_000_000.0)}
    assert engine
    for a in range(0, count, 9):
        for b in range(a + 1, count, 7):
            state = on_source.edge_state(a, b, t, False)
            if (a, b) in engine:
                assert state == (1_000_000_000.0, engine[(a, b)])
            else:
                assert state is None


def test_ground_edges_use_the_link_state(on_source):
    count = on_source.satellite_count
    t = 30.0
    seen = 0
    for satellite in range(count):
        state = on_source.link_state(0, satellite, t)
        assert on_source.edge_state(satellite, count, t, False) == state
        fixed = on_source.edge_state(satellite, count, t, True)
        if state is None:
            assert fixed is None
        else:
            seen += 1
            assert fixed == (2_228_124.0, state[1])
    assert seen


def test_events_on_link_state_switches_at_segment_boundaries(on_source, global_on):
    segments = load_results(global_on).tables["rate_segments"]
    for index in range(segments.rows):
        station = int(segments["station_index"][index])
        satellite = int(segments["satellite_index"][index])
        start = float(segments["start_offset_s"][index])
        end = float(segments["end_offset_s"][index])
        rate = float(segments["rate_bps"][index])
        state = on_source.link_state(station, satellite, start)
        assert state is not None and state[0] == rate
        if end - start > 1e-3:
            assert on_source.link_state(station, satellite, end - 1e-4)[0] == rate


def test_breakpoints_hold_grid_passes_and_switches(on_source, global_on):
    segments = load_results(global_on).tables["rate_segments"]
    mine = [
        k
        for k in range(segments.rows)
        if segments["station_index"][k] == 0 and segments["satellite_index"][k] == 25
    ]
    times = on_source.breakpoints(0.0, 600.0, station_index=0, satellite_index=25)
    assert set(np.arange(0.0, 601.0, 60.0)) <= set(times.tolist())
    for k in mine:
        assert float(segments["start_offset_s"][k]) in times
    inside = on_source.breakpoints(100.0, 200.0, station_index=0, satellite_index=25)
    assert inside.min() >= 100.0 and inside.max() <= 200.0
    route = on_source.breakpoints(0.0, 600.0, pair_index=0, model="minimum_delay")
    assert set(times.tolist()) <= set(route.tolist())
    assert np.all(np.diff(route) > 0)


@pytest.mark.parametrize(
    ("call", "match"),
    [
        (lambda s: s.link_state(0, 0, -1.0), "window"),
        (lambda s: s.link_state(0, 0, math.nan), "window"),
        (lambda s: s.link_state(9, 0, 1.0), "station_index"),
        (lambda s: s.link_state(0, 999, 1.0), "satellite_index"),
        (lambda s: s.route(0, "widest", 1.0), "model"),
        (lambda s: s.route(3, "minimum_delay", 1.0), "pair_index"),
        (lambda s: s.edge_state(81, 80, 1.0, False), "edge"),
        (lambda s: s.edge_state(80, 81, 1.0, False), "satellite"),
        (lambda s: s.breakpoints(5.0, 1.0), "start"),
        (lambda s: s.breakpoints(0.0, 1.0, station_index=0), "satellite_index"),
        (lambda s: s.breakpoints(0.0, 1.0, model="minimum_delay"), "pair_index"),
    ],
)
def test_invalid_queries_are_value_errors(on_source, call, match):
    with pytest.raises(ValueError, match=match):
        call(on_source)


def _copy(directory, tmp_path):
    copy = tmp_path / "run"
    shutil.copytree(directory, copy)
    return copy


def test_source_rejects_tampered_inputs(hydrometeors_off, tmp_path):
    # Without a fresh manifest, any change is a manifest failure.
    plain = _copy(hydrometeors_off, tmp_path / "a")
    scenario = plain / "inputs" / "scenario.json"
    document = json.loads(scenario.read_text(encoding="utf-8"))
    document["radio"]["eirp_dbw"] = 99.0
    scenario.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError, match="manifest"):
        ReplaySource.from_results(plain)

    # A rewritten manifest does not hide a scenario that differs from results.json.
    write_manifest(plain)
    with pytest.raises(ValueError, match="scenario"):
        ReplaySource.from_results(plain)

    # Nor a catalog that differs from the recorded provenance.
    catalog_copy = _copy(hydrometeors_off, tmp_path / "b")
    catalog = catalog_copy / "inputs" / "catalogs" / "iridium_next_2026-09-10.csv"
    catalog.write_bytes(catalog.read_bytes() + b"\n")
    write_manifest(catalog_copy)
    with pytest.raises(ValueError, match="sha256|SHA-256"):
        ReplaySource.from_results(catalog_copy)

    # Nor a missing archived scenario.
    missing = _copy(hydrometeors_off, tmp_path / "c")
    (missing / "inputs" / "scenario.json").unlink()
    write_manifest(missing)
    with pytest.raises(ValueError, match="inputs/scenario.json"):
        ReplaySource.from_results(missing)

    # Nor a results.json whose scenario was edited to match the tampered input.
    both = _copy(hydrometeors_off, tmp_path / "d")
    results_path = both / "results.json"
    results = json.loads(results_path.read_text(encoding="utf-8"))
    results["scenario"]["radio"]["eirp_dbw"] = 99.0
    results_path.write_text(json.dumps(results), encoding="utf-8")
    scenario = both / "inputs" / "scenario.json"
    document = json.loads(scenario.read_text(encoding="utf-8"))
    document["radio"]["eirp_dbw"] = 99.0
    scenario.write_text(json.dumps(document), encoding="utf-8")
    write_manifest(both)
    with pytest.raises(ValueError, match="normalised_sha256|scenario"):
        ReplaySource.from_results(both)


def test_source_detects_a_changed_run_before_publishing(hydrometeors_off, tmp_path):
    copy = _copy(hydrometeors_off, tmp_path)
    source = ReplaySource.from_results(copy)
    provenance = source.provenance()
    assert provenance["results"]["manifest_sha256"] == source.manifest_sha256
    assert provenance["scenario"]["normalised_sha256"]
    assert provenance["catalogs"][0]["archived_path"] == "catalogs/iridium_next_2026-09-10.csv"
    (copy / "results.json").write_text("{}", encoding="utf-8")
    write_manifest(copy)
    with pytest.raises(ValueError, match="changed"):
        source.provenance()


@pytest.fixture(scope="module")
def delayed_off(tmp_path_factory):
    """Events off with a feedback delay: MODCOD switches, so route breakpoints, fall off-grid."""
    from pathlib import Path

    from openleo.engine.run import run, save_run
    from openleo.scenario import load_scenario, parse_scenario, scenario_document

    path = Path("examples/constellations/iridium_global.json")
    document = scenario_document(load_scenario(path))
    document["time"]["stop_utc"] = "2026-09-10T12:30:00Z"
    document["time"]["events"]["enabled"] = False
    document["time"]["convergence"]["mode"] = "off"
    document["adaptation"]["feedback_delay_s"] = 7.5
    scenario = parse_scenario(document, path, "0" * 64)
    return save_run(scenario, run(scenario), tmp_path_factory.mktemp("delayed") / "run")


def test_events_off_edges_reproduce_the_engine_route_delays(delayed_off):
    from itertools import pairwise

    from openleo.replay import NetworkReplayConfig, prepare_network_replay

    source = ReplaySource.from_results(delayed_off)
    routes = load_results(delayed_off).tables["routes"]
    off_grid = 0
    for k in range(routes.rows):
        if not routes["connected"][k]:
            continue
        start = float(routes["start_offset_s"][k])
        model = routes["model"][k]
        nodes = [int(n) for n in routes["nodes"][k].split()]
        total = 0.0
        for a, b in pairwise(nodes):
            state = source.edge_state(min(a, b), max(a, b), start, model == "fixed_capacity")
            assert state is not None and state[0] > 0, (start, model, a, b)
            total += state[1]
        assert total == pytest.approx(float(routes["delay_s"][k]), rel=0, abs=1e-12)
        off_grid += start % 60 != 0
    assert off_grid > 10
    for model in source.route_models:
        prepared = prepare_network_replay(
            source, NetworkReplayConfig(routing_model=model, duration_s=1800)
        )
        assert prepared["routes"][-1]["state"] == "stop"
