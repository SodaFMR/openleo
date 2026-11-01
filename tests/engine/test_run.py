"""Run orchestration: tables, metadata, convergence gate, workers and archival (spec 6.4-6.8, 7)."""

import functools
import gzip
import hashlib
import json
import math
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from math import log2
from pathlib import Path

import numpy as np
import pytest

import openleo
from openleo.engine import run as run_module
from openleo.engine.run import estimate_memory_bytes, run, save_run
from openleo.results import encode_table, load_results
from openleo.scenario import load_scenario, scenario_document
from openleo.scenario.model import IslConfig, NetworkConfig, RoutePair, Station

ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = ROOT / "examples" / "v2"
V1_FIXTURES = ROOT / "tests" / "fixtures" / "v1"
SPEC_TABLES = {
    "satellites",
    "stations",
    "positions",
    "samples",
    "events",
    "passes",
    "pair_metrics",
    "station_metrics",
    "rate_segments",
    "routes",
    "route_metrics",
    "convergence",
}
RESULTS_KEYS = {
    "kind",
    "schema_version",
    "software",
    "scenario",
    "provenance",
    "models",
    "events",
    "convergence",
    "warnings",
    "limitations",
    "summary",
}
WORKERS = min(8, os.cpu_count() or 1)


def _example(name: str):
    return load_scenario(EXAMPLES / f"{name}.json")


def _time(scenario, **changes):
    return replace(scenario, time=replace(scenario.time, **changes))


def _events(scenario, enabled: bool):
    return _time(scenario, events=replace(scenario.time.events, enabled=enabled))


def _gate(scenario, mode: str):
    return _time(scenario, convergence=replace(scenario.time.convergence, mode=mode))


def _window(scenario, start: datetime, minutes: float, step_s: float | None = None):
    return _time(
        scenario,
        start_utc=start,
        stop_utc=start + timedelta(minutes=minutes),
        step_s=scenario.time.step_s if step_s is None else step_s,
    )


def _small(scenario, count: int):
    """The scenario restricted to the first ``count`` satellites of its first catalog."""
    rows = scenario.space_segment.catalogs[0].path.read_text(encoding="utf-8").splitlines()[1:]
    ids = tuple(int(row.split(",")[11]) for row in rows[:count])
    return replace(scenario, space_segment=replace(scenario.space_segment, select_norad_ids=ids))


START = datetime(2026, 9, 10, 12, tzinfo=UTC)


@functools.cache
def _global_run():
    return run(_example("iridium_global"), workers=WORKERS)


@functools.cache
def _hydrometeor_run():
    return run(_gate(_example("iridium_global_hydrometeors"), "error"), workers=WORKERS)


# --- tables and metadata ---------------------------------------------------------------------


def test_run_produces_all_spec_tables():
    results = _global_run()
    assert set(results.tables) == SPEC_TABLES
    for table in results.tables.values():
        for column in table.columns:
            assert column.description, (table.name, column.name)
            if column.dtype != "str":
                assert column.unit, (table.name, column.name)
    assert set(results.metadata) == RESULTS_KEYS
    assert results.metadata["kind"] == "openleo.results"
    assert results.metadata["schema_version"] == "2"
    assert results.metadata["events"]["integration"] == "event_segments"
    satellites = results.tables["satellites"]
    assert satellites["index"].tolist() == list(range(satellites.rows))
    stations = results.tables["stations"]
    assert stations["name"] == ("Madrid", "Tromso", "Singapore", "Quito")
    assert results.tables["positions"].rows == satellites.rows * 121
    # Every exported pair metric is covered by the gate.
    convergence = results.tables["convergence"]
    assert any(m.startswith("route/Madrid->Tromso/minimum_delay/") for m in convergence["metric"])
    assert any(m.endswith("/integrated_shannon_upper_bound_bits") for m in convergence["metric"])


def test_run_without_network_omits_route_tables():
    results = run(_example("iss_cartagena"))
    assert set(results.tables) == SPEC_TABLES - {"routes", "route_metrics"}
    passes = results.tables["passes"]
    assert passes.rows == 1
    # ISS pass over Cartagena: AOS and LOS root-found inside the window.
    assert 0.0 < passes["aos_offset_s"][0] < passes["los_offset_s"][0] < 600.0
    pair = results.tables["pair_metrics"]
    assert pair["pass_count"].tolist() == [1]
    assert pair["integrated_rate_bits"][0] > 0.0


def test_scenario_and_provenance_blocks():
    scenario = _example("iridium_global")
    metadata = _global_run().metadata
    block = metadata["scenario"]
    document = scenario_document(scenario)
    assert block["source_sha256"] == scenario.source_sha256
    assert {k: v for k, v in block.items() if k not in {"source_sha256", "normalised_sha256"}} == (
        document
    )
    canonical = json.dumps(
        document, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )
    assert block["normalised_sha256"] == hashlib.sha256(canonical.encode()).hexdigest()
    catalogs = metadata["provenance"]["catalogs"]
    assert catalogs[0]["record_count"] == 80
    assert catalogs[0]["sha256"] == scenario.space_segment.catalogs[0].sha256
    assert len(metadata["provenance"]["leap_second_table_sha256"]) == 64
    assert len(metadata["provenance"]["elements"]) == 80
    assert metadata["software"]["openleo"] == "2.0.0.dev0"
    models = metadata["models"]
    assert models["adaptation"]["standard"].startswith("ETSI EN 302 307-1")
    assert models["free_space_path_loss"]["standard"] == "ITU-R P.525"
    assert all("standard" in entry and "version" in entry for entry in models.values())
    assert metadata["limitations"][0].startswith("Archived GP elements")
    waiver = [line for line in metadata["limitations"] if "regardless of the RF threshold" in line]
    assert len(waiver) == 1 and "fixed_capacity_bps" in waiver[0]
    assert (
        "Route-metric grid convergence is reported but not gated in S1."
        in (metadata["limitations"])
    )
    baseline = models["network"]["baseline"]
    assert "regardless of the RF threshold (MODCOD lock)" in baseline
    assert "geometrically visible ground link" in baseline


def test_reference_models_list_every_itu_kernel():
    scenario = _window(_gate(_small(_example("iridium_global_hydrometeors"), 3), "off"), START, 5)
    models = run(scenario).metadata["models"]
    standards = {entry["standard"] for entry in models.values()}
    assert {"ITU-R P.676", "ITU-R P.835", "ITU-R P.453", "ITU-R P.838", "ITU-R P.840"} <= standards


def test_public_api():
    assert openleo.__version__ == "2.0.0.dev0"
    assert openleo.run is run
    assert openleo.load_results is load_results
    assert openleo.load_scenario is load_scenario
    assert openleo.Results.__module__ == "openleo.results.model"
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'version = "2.0.0.dev0"' in pyproject


def test_progress_reports_every_phase_in_order():
    phases = []
    run(_window(_small(_example("iridium_global"), 6), START, 10), progress=phases.append)
    assert phases == [
        "catalog",
        "geometry",
        "events",
        "links",
        "routes",
        "convergence",
        "tables",
    ]


# --- v1 equivalence with events disabled -------------------------------------------------------

ANGLE_DEG = 1e-8
RANGE_M = 1e-4
DB = 1e-9
DELAY_S = RANGE_M / 299_792_458.0 + 1e-13
TOLERANCES = {
    "elevation_deg": ANGLE_DEG,
    "azimuth_deg": ANGLE_DEG,
    "range_m": RANGE_M,
    "range_rate_mps": 1e-6,
    "doppler_hz": 1e-3,
    "delay_s": DELAY_S,
    "cn0_db_hz": DB,
    "esn0_db": DB,
    "snr_db": DB,
    "gaseous_dry_attenuation_db": DB,
    "gaseous_water_attenuation_db": DB,
    "gaseous_attenuation_db": DB,
    "free_space_cn0_db_hz": DB,
    "geometric_delay_s": RANGE_M / 299_792_458.0,
    "atmospheric_excess_delay_s": 1e-13,
    "apparent_elevation_deg": 1e-9,
    "cloud_attenuation_db": DB,
    "rain_specific_attenuation_db_per_km": DB,
    "rain_path_length_m": RANGE_M,
    "rain_attenuation_db": DB,
    "hydrometeor_attenuation_db": DB,
    "total_atmospheric_attenuation_db": DB,
    "margin_db": DB,
    "remaining_contact_s": 0.0,
}


@pytest.mark.parametrize(
    "name", ["iridium_global", "iridium_global_reference", "iridium_global_hydrometeors"]
)
def test_events_off_matches_v1_samples(name):
    results = run(_gate(_events(_example(name), False), "off"))
    assert results.metadata["events"]["integration"] == "grid_left_hold"
    samples = results.tables["samples"]
    fixture = json.loads(gzip.decompress((V1_FIXTURES / f"{name}.json.gz").read_bytes()))
    rows = {
        key: index
        for index, key in enumerate(
            zip(
                samples["station_index"].tolist(),
                samples["satellite_index"].tolist(),
                samples["time_index"].tolist(),
                strict=True,
            )
        )
        if key[0] <= 2 and key[1] <= 9
    }
    links = fixture["links"]
    assert set(rows) == {(r["station_index"], r["satellite_index"], r["time_index"]) for r in links}
    bandwidth = results.metadata["scenario"]["radio"]["channel_bandwidth_hz"]
    tolerances = {**TOLERANCES, "shannon_upper_bound_bps": bandwidth * log2(10.0) / 10.0 * DB}
    names = {c.name for c in samples.columns}
    checked = set()
    for link in links:
        row = rows[(link["station_index"], link["satellite_index"], link["time_index"])]
        assert samples["modcod"][row] == (link["modcod"] or "")
        assert samples["rate_bps"][row] == link["rate_bps"]
        assert bool(samples["contact_truncated"][row]) == link["contact_truncated"]
        for field, tolerance in tolerances.items():
            if field not in link:
                continue
            assert field in names
            actual, expected = float(samples[field][row]), link[field]
            error = abs(actual - expected)
            if field == "azimuth_deg":
                error = min(error % 360.0, 360.0 - error % 360.0)
            assert error <= tolerance, (field, link["time_index"], actual, expected)
            checked.add(field)
    assert {"elevation_deg", "esn0_db", "margin_db", "remaining_contact_s"} <= checked


# --- convergence gate -------------------------------------------------------------------------


def test_convergence_passes_on_hydrometeor_example():
    """Spec section 11.5 acceptance, in mode error: the run publishes and every gated row passes.

    Route metrics are reported but not gated in S1 (Task 13 ruling). One of them fails: the
    fixed_capacity route changes 53 times on the 60 s grid and 54 times on the 15 s grid, where a
    6 s intermediate route (4110-4116 s) appears between two route breakpoints, the spec 6.5
    limitation. It is flagged in the table, the convergence block and the warnings.
    """
    results = _hydrometeor_run()
    convergence = results.tables["convergence"]
    gated = convergence["gated"].astype(bool)
    passed = convergence["passed"].astype(bool)
    assert gated.sum() > 3000
    assert passed[gated].all()
    assert set(np.array(convergence["class"])[gated]) == {"link", "station"}
    route_failures = [m for m, ok in zip(convergence["metric"], passed, strict=True) if not ok]
    assert route_failures == ["route/Madrid->Tromso/fixed_capacity/route_changes"]
    warned = [w for w in results.metadata["warnings"] if w.startswith("convergence:")]
    assert len(warned) == 1
    assert "fixed_capacity/route_changes is 53.0" in warned[0]
    assert "route metrics are not gated" in warned[0]
    block = results.metadata["convergence"]
    assert block["summary"]["fatal"] == 0
    assert [(f["metric"], f["class"], f["fatal"]) for f in block["failures"]] == [
        ("route/Madrid->Tromso/fixed_capacity/route_changes", "route", False)
    ]


def test_hydrometeor_convergence_fails_only_on_route_changes_between_breakpoints():
    results = _hydrometeor_run()
    convergence = results.tables["convergence"]
    rows = {
        metric: (primary, refined, bool(ok))
        for metric, primary, refined, ok in zip(
            convergence["metric"],
            convergence["primary"],
            convergence["refined"],
            convergence["passed"],
            strict=True,
        )
    }
    assert rows["route/Madrid->Tromso/fixed_capacity/route_changes"] == (53.0, 54.0, False)
    block = results.metadata["convergence"]
    assert block["skipped"] is False
    assert block["summary"]["failed"] == 1
    assert block["summary"]["classes"]["route"] == {"gated": False, "metrics": 12, "failed": 1}
    assert block["summary"]["max_relative_difference"] < 0.02
    # With events on only quadrature nodes and route breakpoints depend on the grid: pair and
    # station metrics agree to 1e-9 relative (the Shannon integrals), or exactly.
    link_metrics = [
        abs(r - p) / abs(r)
        for metric, (p, r, _) in rows.items()
        if not metric.startswith("route/") and r and math.isfinite(r)
    ]
    assert max(link_metrics) < 1e-9
    # Mean route delays are held to 1 us absolute, so the 1 % relative tolerance decides them.
    delay = rows["route/Madrid->Tromso/fixed_capacity/mean_delay_connected_s"]
    assert delay[2] and abs(delay[1] - delay[0]) > 1e-4


def _coarse(mode: str):
    # A step of 1/9 of the shortest catalog period (5837.66 s / 9 = 648.6 s), with routes. With
    # events on the example still converges (route breakpoints follow the passes), so the
    # deliberately coarse case samples v1-style (events off), where every metric is grid-held.
    scenario = _events(_example("iridium_global"), False)
    return _gate(_time(scenario, step_s=648.0), mode)


def test_convergence_error_and_warn_modes():
    with pytest.raises(ValueError, match=r"convergence gate failed.*suggested /time/step_s"):
        run(_coarse("error"))
    results = run(_coarse("warn"))
    block = results.metadata["convergence"]
    assert block["summary"]["failed"] > 0
    assert len(block["failures"]) == block["summary"]["failed"]
    convergence_warnings = [w for w in results.metadata["warnings"] if w.startswith("convergence:")]
    assert len(convergence_warnings) == block["summary"]["failed"]
    table = results.tables["convergence"]
    assert int(np.sum(table["passed"] == 0)) == block["summary"]["failed"]


def test_convergence_off_records_the_skip():
    scenario = _window(_gate(_small(_example("iridium_global"), 4), "off"), START, 10)
    results = run(scenario)
    assert results.metadata["convergence"]["skipped"] is True
    assert results.tables["convergence"].rows == 0


def test_nothing_visible_publishes_zeros(tmp_path):
    base = _gate(_example("iss_cartagena"), "error")
    # The ISS (51.6 deg inclination) never rises above the horizon of either pole.
    stations = tuple(
        Station(name, latitude, 0.0, 0.0, None, "default", 10.0)
        for name, latitude in (("North", 90.0), ("South", -90.0))
    )
    scenario = replace(
        base,
        ground_segment=replace(base.ground_segment, stations=stations),
        network=NetworkConfig(
            (RoutePair("North", "South"),),
            IslConfig("all_pairs_within_range", 5.0e6, 1.0e9),
            1.0e6,
            ("minimum_delay", "maximum_rate", "fixed_capacity"),
        ),
    )
    results = run(scenario)
    for name in ("samples", "events", "passes", "rate_segments"):
        assert results.tables[name].rows == 0, name
    pairs = results.tables["pair_metrics"]
    assert pairs.rows == 2
    assert pairs["pass_count"].tolist() == [0, 0]
    assert pairs["integrated_rate_bits"].tolist() == [0.0, 0.0]
    assert pairs["integrated_shannon_upper_bound_bits"].tolist() == [0.0, 0.0]
    assert np.isnan(pairs["mean_rate_visible_bps"]).all()
    stations_table = results.tables["station_metrics"]
    assert stations_table["visible_fraction"].tolist() == [0.0, 0.0]
    assert stations_table["best_link_integrated_bits"].tolist() == [0.0, 0.0]
    routes = results.tables["route_metrics"]
    assert routes["connected_fraction"].tolist() == [0.0, 0.0, 0.0]
    assert routes["route_changes"].tolist() == [0, 0, 0]
    assert results.tables["convergence"]["passed"].all()
    assert results.metadata["convergence"]["summary"]["failed"] == 0
    assert results.metadata["summary"]["pass_count"] == 0
    # Publishable as is: header-only tables round-trip.
    saved = save_run(scenario, results, tmp_path / "run")
    loaded = load_results(saved)
    assert loaded.tables["samples"].rows == 0
    assert loaded.tables["samples"].columns == results.tables["samples"].columns


# --- guards ------------------------------------------------------------------------------------


def test_memory_budget_refusal_before_propagation(monkeypatch):
    from skyfield.sgp4lib import EarthSatellite

    def forbidden(*args, **kwargs):
        raise AssertionError("propagation started")

    monkeypatch.setattr(EarthSatellite, "at", forbidden)
    with pytest.raises(ValueError, match=r"memory estimate .* exceeds the budget of 1 bytes"):
        run(_example("iridium_global"), memory_budget_bytes=1)


def test_memory_estimate_formula():
    assert estimate_memory_bytes(80, 4, 121) == 80 * 121 * 6 * 8 + 4 * 80 * 121 * 4 * 8
    with_network = estimate_memory_bytes(80, 4, 121, network=True)
    assert with_network > estimate_memory_bytes(80, 4, 121)
    with pytest.raises(ValueError):
        estimate_memory_bytes(-1, 4, 121)


def test_step_above_period_bound_rejected():
    scenario = _time(_example("iridium_global"), step_s=800.0)
    with pytest.raises(ValueError, match=r"^/time/step_s") as caught:
        run(scenario)
    assert "729.70" in str(caught.value)  # P_min / 8 of the Iridium NEXT catalog, in seconds
    # Events off: v1's sampled semantics accept any grid.
    run(_gate(_events(_window(scenario, START, 40), False), "off"))


def test_event_errors_name_the_pair(monkeypatch):
    def failing(*args, **kwargs):
        raise ValueError("non-monotone elevation between 1.000000 s and 2.000000 s")

    monkeypatch.setattr(run_module, "find_passes", failing)
    with pytest.raises(ValueError) as caught:
        run(_window(_example("iridium_global"), START, 10))
    message = str(caught.value)
    assert "'Madrid'" in message
    assert "NORAD 41917" in message
    assert "non-monotone elevation between 1.000000 s and 2.000000 s" in message


def test_run_argument_checks():
    scenario = _example("iss_cartagena")
    with pytest.raises(ValueError, match="workers"):
        run(scenario, workers=0)
    with pytest.raises(ValueError, match="memory_budget_bytes"):
        run(scenario, memory_budget_bytes=0)


def test_inputs_not_mutated():
    scenario = _window(_small(_example("iridium_global"), 4), START, 10)
    document = json.dumps(scenario_document(scenario), sort_keys=True)
    catalog = scenario.space_segment.catalogs[0].path
    before = catalog.read_bytes()
    copy = replace(scenario)
    run(scenario)
    assert scenario == copy
    assert json.dumps(scenario_document(scenario), sort_keys=True) == document
    assert catalog.read_bytes() == before


# --- workers ------------------------------------------------------------------------------------


def test_workers_identical_tables():
    scenario = _window(_small(_example("iridium_global"), 16), START, 30)
    one = run(scenario, workers=1)
    two = run(scenario, workers=3)  # uneven satellite chunks
    assert json.dumps(one.metadata, sort_keys=True) == json.dumps(two.metadata, sort_keys=True)
    assert set(one.tables) == set(two.tables)
    for name, table in one.tables.items():
        assert encode_table(table) == encode_table(two.tables[name]), name


# --- warnings and archival ----------------------------------------------------------------------


def test_warnings_element_age_and_synthetic_rf(tmp_path):
    scenario = _window(_small(_example("iridium_global"), 5), datetime(2026, 10, 1, tzinfo=UTC), 10)
    results = run(scenario)
    saved = save_run(scenario, results, tmp_path / "aged")
    warnings = json.loads((saved / "results.json").read_text(encoding="utf-8"))["warnings"]
    aged = [w for w in warnings if "more than 14 days from the element epoch" in w]
    assert len(aged) == 5
    assert aged[0].startswith("NORAD ")
    assert any("synthetic_rf" in w and "not the named operator" in w for w in warnings)
    fresh = run(_window(scenario, START, 10))
    assert not any("14 days" in w for w in fresh.metadata["warnings"])
    iss = run(_example("iss_cartagena"))
    assert not any("synthetic_rf" in w for w in iss.metadata["warnings"])


def test_save_run_archives_inputs(tmp_path):
    scenario = _window(_small(_example("iridium_global"), 4), START, 10)
    results = run(scenario)
    saved = save_run(scenario, results, tmp_path / "run")
    assert saved == tmp_path / "run"
    catalog = scenario.space_segment.catalogs[0].path
    archived_catalog = saved / "inputs" / "catalogs" / catalog.name
    assert archived_catalog.read_bytes() == catalog.read_bytes()
    archived = load_scenario(saved / "inputs" / "scenario.json")
    assert archived.space_segment.catalogs[0].path == archived_catalog.resolve()
    assert scenario_document(archived)["time"] == scenario_document(scenario)["time"]
    document = json.loads((saved / "inputs" / "scenario.json").read_text(encoding="utf-8"))
    assert document["space_segment"]["catalogs"][0]["path"] == f"catalogs/{catalog.name}"
    provenance = results.metadata["provenance"]["catalogs"][0]
    assert provenance["archived_path"] == f"catalogs/{catalog.name}"
    assert (saved / "inputs" / provenance["archived_path"]).read_bytes() == catalog.read_bytes()
    on_disk = json.loads((saved / "results.json").read_text(encoding="utf-8"))
    assert set(on_disk) == RESULTS_KEYS | {"tables"}
    assert {path.suffix for path in (saved / "tables").iterdir()} == {".csv", ".olc"}
    header = (saved / "tables" / "events.csv").read_text(encoding="utf-8").splitlines()[0]
    assert "t_utc" in header.split(",")
    loaded = load_results(saved)
    for name, table in results.tables.items():
        assert loaded.tables[name] == table, name
    csv_only = save_run(scenario, results, tmp_path / "csv", formats=("csv",))
    assert {path.suffix for path in (csv_only / "tables").iterdir()} == {".csv"}


def test_save_run_refuses_a_changed_catalog(tmp_path):
    source = _example("iss_cartagena")
    catalog = tmp_path / "data" / "iss.csv"
    catalog.parent.mkdir()
    catalog.write_bytes(source.space_segment.catalogs[0].path.read_bytes())
    scenario = replace(
        source,
        space_segment=replace(
            source.space_segment,
            catalogs=(replace(source.space_segment.catalogs[0], path=catalog),),
        ),
    )
    results = run(_gate(scenario, "off"))
    catalog.write_bytes(catalog.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="SHA-256"):
        save_run(scenario, results, tmp_path / "run")
    assert not (tmp_path / "run").exists()


def test_metadata_is_json_safe():
    text = json.dumps(_global_run().metadata, allow_nan=False)
    assert "NaN" not in text
    summary = _global_run().metadata["summary"]
    assert summary["satellite_count"] == 80
    assert summary["station_count"] == 4
    assert math.isfinite(summary["integrated_rate_bits"])


# --- internals -----------------------------------------------------------------------------------


def test_route_chunks_merge_to_the_unchunked_routes(monkeypatch):
    scenario = _gate(_window(_small(_example("iridium_global"), 12), START, 40), "off")
    whole = run(scenario)
    # Three breakpoints' positions per chunk: routes are evaluated chunk by chunk and merged.
    monkeypatch.setattr(run_module, "ROUTE_CHUNK_BYTES", 3 * 24 * 12)
    chunked = run(scenario)
    assert whole.tables["routes"].rows > 3
    assert chunked.tables["routes"] == whole.tables["routes"]
    assert chunked.tables["route_metrics"] == whole.tables["route_metrics"]


def test_link_errors_name_the_pair(monkeypatch):
    def failing(*args, **kwargs):
        raise ValueError("adaptive Gauss-Legendre did not converge")

    monkeypatch.setattr(run_module, "integrate_smooth", failing)
    with pytest.raises(ValueError, match=r"station 'Cartagena', NORAD 25544: adaptive Gauss"):
        run(_gate(_example("iss_cartagena"), "off"))


def test_refined_shannon_errors_name_the_pair(monkeypatch):
    calls = []
    original = run_module.integrate_smooth

    def failing_when_refined(f, breakpoints, *args, **kwargs):
        calls.append(len(breakpoints))
        if len(calls) > 1:  # the primary pass integrates; the refined one fails
            raise ValueError("refined quadrature failed")
        return original(f, breakpoints, *args, **kwargs)

    monkeypatch.setattr(run_module, "integrate_smooth", failing_when_refined)
    with pytest.raises(ValueError, match=r"station 'Cartagena', NORAD 25544: refined quadrature"):
        run(_example("iss_cartagena"))


def test_feedback_delay_shifts_modcod_effects():
    base = _gate(_example("iss_cartagena"), "off")
    delayed = replace(base, adaptation=replace(base.adaptation, feedback_delay_s=2.0))
    events = run(delayed).tables["events"]
    decisions = [k for k, kind in enumerate(events["type"]) if kind == "modcod_decision"]
    effects = [k for k, kind in enumerate(events["type"]) if kind == "modcod_effective"]
    assert decisions
    los = float(events["t_offset_s"][events["type"].index("los")])
    for k in decisions:
        decided = float(events["t_offset_s"][k])
        matching = [j for j in effects if events["decision_offset_s"][j] == decided]
        if decided + 2.0 < los:
            assert [float(events["t_offset_s"][j]) for j in matching] == [decided + 2.0]
        else:  # dropped at the pass end: it never takes effect
            assert matching == []


def test_pass_helpers_cover_degenerate_passes():
    from openleo.engine.acm import ModcodEvent
    from openleo.engine.events import Pass

    scenario = _gate(_example("iss_cartagena"), "off")
    ctx = run_module._Context(scenario)
    point = Pass(120.0, 120.0, 120.0, 10.0, False, False)
    pair = run_module._PairEvents((point,), (), (), None)
    assert run_module._shannon_on_events(ctx, 0, 0, pair, ctx.grid.offsets_s) == (0.0,)
    # A decided mode whose requirement the available Es/N0 misses has lost its frames.
    end = Pass(100.0, 600.0, 300.0, 40.0, False, True)
    events = (ModcodEvent(100.0, 100.0, None, "8PSK 3/4"),)
    assert run_module._state_at_pass_end(ctx, end, events, 0.0) == (None, 0.0)
    rate = run_module._state_at_pass_end(ctx, end, events, 9.0)
    assert rate[0] == "8PSK 3/4"
    assert rate[1] == scenario.adaptation.symbol_rate_baud * 2.228124


def test_save_run_reports_a_missing_catalog(tmp_path):
    source = _example("iss_cartagena")
    catalog = tmp_path / "iss.csv"
    catalog.write_bytes(source.space_segment.catalogs[0].path.read_bytes())
    scenario = replace(
        source,
        space_segment=replace(
            source.space_segment,
            catalogs=(replace(source.space_segment.catalogs[0], path=catalog),),
        ),
    )
    results = run(_gate(scenario, "off"))
    catalog.unlink()
    with pytest.raises(ValueError, match="could not read"):
        save_run(scenario, results, tmp_path / "run")


def test_archive_names_disambiguate_shared_basenames(tmp_path):
    source = _example("iss_cartagena").space_segment.catalogs[0]
    first = replace(source, path=tmp_path / "a" / "gp.csv")
    second = replace(source, path=tmp_path / "b" / "gp.csv")
    third = replace(source, path=tmp_path / "c.csv")
    assert run_module._archive_names((first, second, third)) == ["0-gp.csv", "1-gp.csv", "c.csv"]


def test_a_decision_effective_at_the_pass_end_is_dropped_everywhere():
    from openleo.engine.acm import ModcodEvent
    from openleo.engine.events import Pass

    base = _gate(_example("iss_cartagena"), "off")
    for delay in (0.0, 2.0):
        scenario = replace(base, adaptation=replace(base.adaptation, feedback_delay_s=delay))
        ctx = run_module._Context(scenario)
        end = Pass(100.0, 600.0, 300.0, 40.0, False, True)
        kept = ModcodEvent(100.0, 100.0 + delay, None, "QPSK 1/2")
        dropped = ModcodEvent(600.0 - delay, 600.0, "QPSK 1/2", "8PSK 3/4")
        # The state in effect at the pass end ignores the decision effective exactly there ...
        state = run_module._state_at_pass_end(ctx, end, (kept, dropped), 20.0)
        assert state[0] == "QPSK 1/2"
        # ... and the events table records that decision without an effect row, as acm does.
        pair = run_module._PairEvents((end,), (), (kept, dropped), None)
        rows = run_module._event_rows(ctx, 0, 0, pair)
        kinds = [(row[3], row[2]) for row in rows if row[3].startswith("modcod")]
        assert kinds == [
            ("modcod_decision", 100.0),
            ("modcod_effective", 100.0 + delay),
            ("modcod_decision", 600.0 - delay),
        ]
