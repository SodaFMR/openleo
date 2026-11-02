"""Static reports of a results directory or a sweep, and results export."""

import csv
import json
from pathlib import Path

import pytest

from openleo import sweep
from openleo.engine.run import run, save_run
from openleo.export import export_results
from openleo.report import write_report
from openleo.results import load_results, manifest_digest, read_parquet, verify_manifest
from openleo.scenario import load_scenario, parse_scenario, scenario_document
from openleo.scenario.parse import _relative_posix

ROOT = Path(__file__).resolve().parents[1]
GLOBAL = ROOT / "examples" / "constellations" / "iridium_global.json"
ISS = ROOT / "examples" / "scenarios" / "iss_cartagena.json"


@pytest.fixture(scope="module")
def results_dir(tmp_path_factory):
    """A small networked run with the convergence gate on (warn), so it has every section."""
    scenario = load_scenario(GLOBAL)
    document = scenario_document(scenario)
    document["time"]["stop_utc"] = "2026-09-10T12:05:00Z"
    document["space_segment"]["select_norad_ids"] = [42960, 42961, 43570, 43572]
    parsed = parse_scenario(document, GLOBAL, scenario.source_sha256)
    return save_run(parsed, run(parsed), tmp_path_factory.mktemp("report") / "run")


@pytest.fixture(scope="module")
def sweep_dir(tmp_path_factory):
    directory = tmp_path_factory.mktemp("sweep")
    spec = {
        "schema": "openleo.sweep/2",
        "name": "report sweep",
        "base_scenario": _relative_posix(ISS, directory),
        "mode": "oat",
        "parameters": [{"path": "/radio/eirp_dbw", "values": [7.0, 10.0], "nominal": 10.0}],
    }
    path = directory / "sweep.json"
    path.write_text(json.dumps(spec), encoding="utf-8")
    output = directory / "out"
    sweep.run_sweep(path, output)
    return output


def _rows(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def test_report_from_results_and_sweep(results_dir, sweep_dir, tmp_path):
    results = load_results(results_dir)
    metadata = results.metadata
    output = write_report(results_dir, tmp_path / "results-report")
    assert output == tmp_path / "results-report"
    text = (output / "report.md").read_text(encoding="utf-8")
    assert sorted(path.name for path in output.iterdir()) == ["report.md", "summary.csv"]
    for line in metadata["limitations"] + metadata["warnings"]:
        assert line in text
    assert "Shannon" in text and "upper bound" in text
    assert "reported, not gated" in text
    assert metadata["scenario"]["normalised_sha256"] in text
    assert metadata["scenario"]["source_sha256"] in text
    assert metadata["provenance"]["catalogs"][0]["sha256"] in text
    assert manifest_digest(results_dir) in text
    convergence = metadata["convergence"]["summary"]
    assert f"{convergence['metrics']} metrics" in text
    rows = _rows(output / "summary.csv")
    assert list(rows[0]) == ["scope", "name", "metric", "value", "unit"]
    station = {(r["name"], r["metric"]): r["value"] for r in rows if r["scope"] == "station"}
    table = results.tables["station_metrics"]
    assert float(station[("Madrid", "visible_fraction")]) == float(
        table["visible_fraction"][list(table["name"]).index("Madrid")]
    )
    assert any(r["scope"] == "route" for r in rows)
    shannon = [r for r in rows if r["metric"] == "integrated_shannon_upper_bound_bits"]
    assert shannon and shannon[0]["unit"] == "bit (upper bound)"

    document = json.loads((sweep_dir / "sweep.json").read_text(encoding="utf-8"))
    output = write_report(sweep_dir, tmp_path / "sweep-report")
    text = (output / "report.md").read_text(encoding="utf-8")
    for line in document["limitations"]:
        assert line in text
    assert document["comparison_label"] in text
    assert document["baseline_run_id"] in text
    for entry in document["runs"]:
        assert entry["id"] in text
        assert entry["scenario_sha256"] in text
    assert manifest_digest(sweep_dir) in text
    rows = _rows(output / "summary.csv")
    assert list(rows[0]) == ["run_id", "metric", "value", "baseline_value", "delta", "ratio"]
    assert {r["run_id"] for r in rows} == {entry["id"] for entry in document["runs"]}
    assert all(r["metric"].startswith("station/") for r in rows)


def test_report_figures_are_optional(results_dir, sweep_dir, tmp_path):
    pytest.importorskip("matplotlib")
    output = write_report(results_dir, tmp_path / "figures", figures=True)
    names = sorted(path.name for path in output.iterdir())
    assert "report.md" in names and "summary.csv" in names
    assert {name for name in names if name.endswith((".png", ".svg"))}
    text = (output / "report.md").read_text(encoding="utf-8")
    for name in names:
        if name.endswith(".png"):
            assert f"]({name})" in text
    output = write_report(sweep_dir, tmp_path / "sweep-figures", figures=True)
    assert any(path.suffix == ".svg" for path in output.iterdir())


def test_report_rejects_other_directories(tmp_path):
    with pytest.raises(ValueError, match="results or sweep"):
        write_report(tmp_path, tmp_path / "out")


def test_report_refuses_a_non_empty_output(results_dir, tmp_path):
    output = tmp_path / "out"
    output.mkdir()
    (output / "keep").write_text("keep", encoding="utf-8")
    with pytest.raises(ValueError, match="non-empty"):
        write_report(results_dir, output)
    assert (output / "keep").read_text(encoding="utf-8") == "keep"


def test_export_round_trip_csv_parquet(results_dir, tmp_path):
    original = load_results(results_dir)
    exported = export_results(results_dir, "csv", tmp_path / "csv")
    verify_manifest(exported)
    assert not list((exported / "tables").glob("*.olc"))
    copy = load_results(exported)
    assert copy.metadata == original.metadata
    assert copy.tables == original.tables
    assert (exported / "inputs" / "scenario.json").read_bytes() == (
        results_dir / "inputs" / "scenario.json"
    ).read_bytes()

    pytest.importorskip("pyarrow")
    exported = export_results(results_dir, "parquet", tmp_path / "parquet")
    verify_manifest(exported)
    for name, table in original.tables.items():
        assert read_parquet(exported / "tables" / f"{name}.parquet") == table
    assert load_results(exported).tables == original.tables


def test_export_rejects_unknown_formats_and_existing_output(results_dir, tmp_path):
    with pytest.raises(ValueError, match="csv or parquet"):
        export_results(results_dir, "olc", tmp_path / "x")
    (tmp_path / "exists").mkdir()
    (tmp_path / "exists" / "f").write_text("x")
    with pytest.raises((ValueError, FileExistsError)):
        export_results(results_dir, "csv", tmp_path / "exists")


def test_report_failure_leaves_the_output_directory_untouched(results_dir, tmp_path, monkeypatch):
    from openleo.report import figures

    def broken(context, stage):
        (stage / "partial.png").write_bytes(b"partial")
        raise ValueError("figure failed")

    monkeypatch.setattr(figures, "render_figures", broken)
    output = tmp_path / "out"
    output.mkdir()
    with pytest.raises(ValueError, match="figure failed"):
        write_report(results_dir, output, figures=True)
    assert output.is_dir() and list(output.iterdir()) == []
    assert sorted(p.name for p in tmp_path.iterdir()) == ["out"]
    with pytest.raises(ValueError, match="figure failed"):
        write_report(results_dir, tmp_path / "absent", figures=True)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["out"]
    assert write_report(results_dir, output) == output
    assert sorted(p.name for p in output.iterdir()) == ["report.md", "summary.csv"]
