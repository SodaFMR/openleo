"""scripts/compare_results.py: the cross-platform numeric comparison CI runs (spec section 7.5)."""

import copy
import importlib.util
import json
from pathlib import Path

import pytest

from openleo.engine._assembly import scenario_block
from openleo.engine.run import run, save_run
from openleo.results import Table, load_results, write_results
from openleo.scenario import load_scenario

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "compare_results.py"
ISS = ROOT / "examples" / "scenarios" / "iss_cartagena.json"
COUPLED = ROOT / "examples" / "constellations" / "iridium_global_hydrometeors.json"
CI_DIGEST = ROOT / "tests" / "fixtures" / "ci" / "coupled-ubuntu.json"


def _module():
    spec = importlib.util.spec_from_file_location("compare_results", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


compare = _module()


@pytest.fixture(scope="module")
def iss_run(tmp_path_factory) -> Path:
    directory = tmp_path_factory.mktemp("compare") / "iss"
    scenario = load_scenario(ISS)
    save_run(scenario, run(scenario), directory)
    return directory


def test_a_run_agrees_with_itself_and_with_its_digest(iss_run, tmp_path, capsys):
    assert compare.main([str(iss_run), str(iss_run)]) == 0
    digest = tmp_path / "digest.json"
    assert compare.main([str(iss_run), "--write-digest", str(digest)]) == 0
    assert compare.main([str(iss_run), str(digest)]) == 0
    assert "within the spec section 11 tolerances" in capsys.readouterr().out
    text = digest.read_text(encoding="utf-8")
    assert text.endswith("}\n") and "\r" not in text
    document = json.loads(text)
    assert document["kind"] == "openleo.results-digest"
    assert document["tables"]["samples"]["rows"] > 0


def test_digest_bounds_follow_the_element_tolerance(iss_run):
    reference = compare.digest(iss_run)
    tau = reference["event_tolerance_s"]
    assert tau == 0.001  # the example has events on at the default tolerance
    column = reference["tables"]["samples"]["columns"]["range_m"]
    atol, _ = compare.tolerance("range_m", "m", tau)

    within = copy.deepcopy(reference)
    within["tables"]["samples"]["columns"]["range_m"]["max"] += 0.5 * atol
    within["tables"]["samples"]["columns"]["range_m"]["sum"] += 0.5 * atol * column["finite"]
    assert compare.compare_digests(reference, within) == []

    beyond = copy.deepcopy(reference)
    beyond["tables"]["samples"]["columns"]["range_m"]["max"] += 2.0 * atol
    (failure,) = compare.compare_digests(reference, beyond)
    assert failure.startswith("samples.range_m: max ")

    shifted = copy.deepcopy(reference)
    shifted["tables"]["samples"]["columns"]["range_m"]["sum"] += 2.0 * atol * column["finite"]
    (failure,) = compare.compare_digests(reference, shifted)
    assert failure.startswith("samples.range_m: sum ")


def test_digest_structure_differences_are_reported(iss_run):
    reference = compare.digest(iss_run)
    other = copy.deepcopy(reference)
    other["scenario_sha256"] = "0" * 64
    other["tables"]["samples"]["rows"] += 1
    other["tables"]["passes"]["column_order"].reverse()
    other["tables"]["satellites"]["columns"]["name"]["sha256"] = "0" * 64
    other["tables"]["events"]["columns"]["elevation_deg"]["nan"] += 1
    other["tables"]["events"]["columns"]["t_offset_s"]["unit"] = "h"
    del other["tables"]["stations"]
    failures = compare.compare_digests(reference, other)
    assert failures[0].startswith("scenario:")
    assert any(line.startswith("tables:") for line in failures)
    assert any(line.startswith("samples: ") and "rows" in line for line in failures)
    assert any(line.startswith("passes: columns") for line in failures)
    assert "satellites.name: text values differ" in failures
    assert any(line.startswith("events.elevation_deg:") and "NaN" in line for line in failures)
    assert any(line.startswith("events.t_offset_s: dtype/unit") for line in failures)


def test_directories_are_compared_element_by_element(iss_run, tmp_path):
    # Shift one range by 1 mm (ten times the tolerance) and publish the run again.
    other = tmp_path / "other"
    results = load_results(iss_run)
    samples = results.tables["samples"]
    data = {column.name: samples[column.name] for column in samples.columns}
    data["range_m"] = data["range_m"].copy()
    data["range_m"][3] += 1e-3
    tables = dict(results.tables)
    tables["samples"] = Table("samples", samples.columns, data)
    write_results(type(results)(results.metadata, tables), other, formats=("csv",))
    (failure,) = compare.compare_directories(other, iss_run)
    assert failure.startswith("samples.range_m: row 3: ")


def test_tolerances_follow_spec_section_11():
    tau = 0.001
    assert compare.tolerance("elevation_deg", "deg", tau) == (1e-8, 0.0)
    assert compare.tolerance("apparent_elevation_deg", "deg", tau) == (1e-9, 0.0)
    assert compare.tolerance("doppler_hz", "Hz", tau) == (1e-3, 0.0)
    assert compare.tolerance("cn0_db_hz", "dB-Hz", tau) == (1e-9, 0.0)
    assert compare.tolerance("atmospheric_excess_delay_s", "s", tau) == (1e-13, 0.0)
    assert compare.tolerance("delay_s", "s", tau) == (1e-12, 0.0)
    assert compare.tolerance("aos_offset_s", "s", tau) == (2e-3, 0.0)
    assert compare.tolerance("rate_bps", "bit/s", tau) == (0.0, 0.0)
    assert compare.tolerance("integrated_rate_bits", "bit", tau) == (1.0, 1e-6)
    assert compare.tolerance("primary", "metric unit", tau) == (1.0, 1e-6)
    assert compare.tolerance("rain_rate_mm_h", "mm/h", tau) == (0.0, 1e-12)


def test_usage_errors_exit_2(iss_run, tmp_path, capsys):
    with pytest.raises(SystemExit) as raised:
        compare.main([str(iss_run)])
    assert raised.value.code == 2
    bad = tmp_path / "bad.json"
    bad.write_text('{"kind": "something else"}', encoding="utf-8")
    assert compare.main([str(iss_run), str(bad)]) == 2
    assert "not an openleo.results-digest" in capsys.readouterr().err


def test_the_ci_digest_is_for_the_shipped_coupled_example():
    digest = json.loads(CI_DIGEST.read_text(encoding="utf-8"))
    scenario = load_scenario(COUPLED)
    assert digest["scenario_sha256"] == scenario_block(scenario)["normalised_sha256"]
    assert digest["scenario_name"] == scenario.name
    assert set(digest["tables"]) >= {"samples", "events", "passes", "routes", "convergence"}
