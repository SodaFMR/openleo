import json
import os
from dataclasses import FrozenInstanceError
from hashlib import sha256

import pytest
import test_app
from test_experiment import _with_atmosphere

scenario = test_app.scenario


def _config(scenario, tmp_path, **updates):
    from openleo.constellation import scenario_document

    source = _with_atmosphere(scenario, hydrometeors=True)
    source.source_path.write_text(json.dumps(scenario_document(source)))
    raw = {
        "schema_version": "1",
        "name": "<script>benchmark</script>",
        "scenario_path": source.source_path.name,
        "frequencies_hz": [12e9],
        "sampling_steps_s": [10, 20],
        "refinements": [1, 2],
        "traffic_loads_bps": [10000, 300000],
        "start_offset_s": 0,
        "duration_s": 20,
        "packet_size_bytes": 1200,
        "queue_packets": 8,
        "flows_per_direction": 2,
        "acquisition_delay_s": 0.1,
        "seed": 1,
        **updates,
    }
    path = tmp_path / "benchmark-input.json"
    path.write_text(json.dumps(raw))
    return path


def test_model_only_has_real_metrics_aligned_effects_and_portable_manifest(scenario, tmp_path):
    from openleo.benchmark import load_benchmark_config, run_benchmark

    path = _config(scenario, tmp_path)
    config = load_benchmark_config(path)
    with pytest.raises(FrozenInstanceError):
        config.name = "changed"
    target = tmp_path / "result"
    result = run_benchmark(path, target)
    assert result["kind"] == "openleo.benchmark"
    assert len(result["runs"]) == 4
    assert result["packet_evidence"] == "missing"
    assert all(run["offered_load_bps"] is None for run in result["runs"])
    assert all(run["summary"]["configuration"]["packet_mode"] == "none" for run in result["runs"])
    assert all(run["elapsed_s"] >= 0 for run in result["runs"])
    assert {row["kind"] for row in result["metrics"]} == {"station", "route"}
    assert all(row["unit"] for row in result["metrics"])
    rates = [row for row in result["metrics"] if row["metric"] == "mean_best_rate_bps"]
    assert any(row["value"] > 0 for row in rates)
    axes = {row["axis"] for row in result["comparisons"]}
    assert {"step_s", "refinement", "propagation"} <= axes
    assert any(row["absolute_effect"] == 0 for row in result["comparisons"])
    assert len(result["uncertainty_inventory"]) >= 7
    html = (target / "index.html").read_text()
    assert "<script>" not in html
    assert "&lt;script&gt;" in html and "<svg" in html
    assert "not measured" in html.lower() and "missing" in html.lower()
    for run in result["runs"]:
        assert (target / run["bundle_path"] / "index.html").is_file()
        assert run["bundle_path"] + "/index.html" in html
    manifest = json.loads((target / "manifest.json").read_text())
    assert {"benchmark.json", "metrics.csv", "index.html"} <= manifest["files"].keys()
    for filename, checksum in manifest["files"].items():
        assert sha256((target / filename).read_bytes()).hexdigest() == checksum
    assert str(tmp_path) not in (target / "benchmark.json").read_text()
    repeated = run_benchmark(path, tmp_path / "repeat")
    assert repeated["metrics"] == result["metrics"]
    assert repeated["comparisons"] == result["comparisons"]
    assert repeated["numerical_metrics_sha256"] == result["numerical_metrics_sha256"]


def test_effects_align_all_other_parameters_and_keep_zero_denominator():
    from openleo.benchmark import _comparisons

    baseline = {
        "run_id": "a",
        "frequency_hz": 12e9,
        "step_s": 30,
        "refinement": 1,
        "offered_load_bps": 10000,
        "propagation": "free_space",
        "kind": "station",
        "name": "A",
        "routing_model": None,
        "direction": None,
        "flow": None,
        "metric": "mean_best_rate_bps",
        "unit": "bit/s",
        "value": 0,
    }
    rows = [
        baseline,
        {**baseline, "run_id": "b", "step_s": 60, "value": 5},
        {**baseline, "run_id": "c", "refinement": 2, "value": 0},
        {**baseline, "run_id": "d", "step_s": 60, "offered_load_bps": 300000, "value": 100},
    ]
    effects = _comparisons(rows)
    step = [row for row in effects if row["axis"] == "step_s"]
    assert len(step) == 1
    assert step[0]["absolute_effect"] == 5
    assert step[0]["relative_effect"] is None
    assert step[0]["case_count"] == 2
    refinement = [row for row in effects if row["axis"] == "refinement"]
    assert len(refinement) == 1 and refinement[0]["absolute_effect"] == 0
    rows = [{**baseline, "value": 10}, {**baseline, "step_s": 60, "value": 15}]
    assert _comparisons(rows)[0]["relative_effect"] == 0.5


def test_report_chart_aliases_have_matrix_lookup_and_preserve_full_station_names():
    from html.parser import HTMLParser
    from re import DOTALL, findall
    from xml.etree.ElementTree import fromstring

    from openleo.benchmark import _report, _uncertainty_inventory

    class ReportReader(HTMLParser):
        def __init__(self):
            super().__init__()
            self.tables = []
            self.classes = []
            self.cell = None

        def handle_starttag(self, tag, attributes):
            self.classes.extend(value for key, value in attributes if key == "class")
            if tag == "table":
                self.tables.append([])
            if tag == "tr":
                self.tables[-1].append([])
            if tag in ("th", "td"):
                self.cell = ""

        def handle_data(self, data):
            if self.cell is not None:
                self.cell += data

        def handle_endtag(self, tag):
            if tag in ("th", "td"):
                self.tables[-1][-1].append(self.cell)
                self.cell = None

    runs = [
        {
            "id": "case-" + digit * 16,
            "frequency_hz": 12e9,
            "step_s": 30,
            "refinement": 1,
            "offered_load_bps": None,
            "elapsed_s": 0,
            "bundle_path": "cases/case-" + digit * 16,
        }
        for digit in ("a", "b")
    ]
    stations = ("Madrid", "Tromso", "Singapore", "Quito")
    rows = [
        {
            "run_id": run["id"],
            "propagation": "hydrometeors",
            "kind": "station",
            "name": name,
            "routing_model": None,
            "direction": None,
            "flow": None,
            "metric": "mean_best_rate_bps",
            "value": 1000,
            "unit": "bit/s",
        }
        for run in runs
        for name in stations
    ]
    html = _report(
        {
            "runs": runs,
            "name": "Chart fixture",
            "packet_evidence": "missing",
            "metrics": rows,
            "comparisons": [],
            "limitations": [],
            "uncertainty_inventory": _uncertainty_inventory(),
        }
    )
    report = ReportReader()
    report.feed(html)
    assert "charts" in report.classes
    assert report.tables[0][0][:2] == ["Alias", "Run"]
    assert [row[:2] for row in report.tables[0][1:]] == [
        ["R1", "case-aaaaaaaaaaaaaaaa"],
        ["R2", "case-bbbbbbbbbbbbbbbb"],
    ]
    charts = [fromstring(svg) for svg in findall(r"<svg\b.*?</svg>", html, DOTALL)]
    assert len(charts) == 4
    assert all(
        [node.text for node in svg.findall("text") if node.get("x") == "8"] == ["R1", "R2"]
        for svg in charts
    )
    assert all(
        any(name in cell for table in report.tables for row in table for cell in row)
        for name in stations
    )


@pytest.mark.parametrize(
    "updates",
    [
        {"schema_version": 1},
        {"unknown": 1},
        {"name": " "},
        {"scenario_path": "/absolute/path"},
        {"frequencies_hz": []},
        {"frequencies_hz": [12e9, 12e9]},
        {"frequencies_hz": [201e9]},
        {"frequencies_hz": [True]},
        {"sampling_steps_s": [0]},
        {"sampling_steps_s": [0.0000001]},
        {"sampling_steps_s": [1, 2, 3, 4]},
        {"refinements": [True]},
        {"refinements": [3]},
        {"traffic_loads_bps": [0]},
        {"traffic_loads_bps": [float("nan")]},
        {"duration_s": 21},
        {"duration_s": True},
        {"start_offset_s": -1},
        {"start_offset_s": 1e100},
        {"flows_per_direction": 5},
        {"queue_packets": 0},
        {"seed": 0},
        {"traffic_loads_bps": [1e9]},
        {"acquisition_delay_s": 61},
    ],
)
def test_invalid_configuration_never_creates_outputs(scenario, tmp_path, updates):
    from openleo.benchmark import run_benchmark

    path = _config(scenario, tmp_path, **updates)
    target = tmp_path / "result"
    with pytest.raises(ValueError):
        run_benchmark(path, target)
    assert not target.exists()


def test_duplicate_keys_size_missing_atmosphere_and_existing_outputs(scenario, tmp_path):
    from openleo.benchmark import run_benchmark

    path = _config(scenario, tmp_path, sampling_steps_s=[20], refinements=[1])
    valid = path.read_text()
    target = tmp_path / "result"
    path.write_text(valid.replace('"seed": 1', '"seed": 1, "seed": 2'))
    with pytest.raises(ValueError, match="duplicate"):
        run_benchmark(path, target)
    path.write_text(" " * 1000001)
    with pytest.raises(ValueError, match="bytes"):
        run_benchmark(path, target)
    path.write_text(valid)
    raw = json.loads(scenario.source_path.read_text())
    scenario.source_path.write_text(
        json.dumps({key: value for key, value in raw.items() if key != "propagation"})
    )
    with pytest.raises(ValueError, match="hydrometeor"):
        run_benchmark(path, target)
    assert not target.exists()
    path = _config(scenario, tmp_path, sampling_steps_s=[20], refinements=[1])
    target.mkdir()
    (target / "keep.txt").write_text("keep")
    with pytest.raises(ValueError, match="nonempty"):
        run_benchmark(path, target)
    assert (target / "keep.txt").read_text() == "keep"
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match="symbolic"):
        run_benchmark(path, link)


def test_runtime_failure_preserves_empty_output_and_removes_staging(
    scenario, tmp_path, monkeypatch
):
    from openleo import benchmark

    path = _config(scenario, tmp_path, sampling_steps_s=[20], refinements=[1])
    target = tmp_path / "result"
    target.mkdir()

    def fail(*args, **kwargs):
        raise RuntimeError("failed simulation")

    monkeypatch.setattr(benchmark, "run_experiment", fail)
    with pytest.raises(RuntimeError, match="failed simulation"):
        benchmark.run_benchmark(path, target)
    assert list(target.iterdir()) == []
    assert not list(tmp_path.glob(".result-*"))


def test_spreadsheet_station_names_are_escaped(scenario, tmp_path):
    from dataclasses import replace

    from openleo.benchmark import run_benchmark

    source = replace(
        scenario,
        stations=(replace(scenario.stations[0], name="=formula"), scenario.stations[1]),
        network=replace(scenario.network, source_station="=formula"),
    )
    path = _config(source, tmp_path, sampling_steps_s=[20], refinements=[1])
    target = tmp_path / "csv-safe"
    summary = run_benchmark(path, target)
    assert any(row["name"] == "=formula" for row in summary["metrics"])
    assert "'=formula" in (target / "metrics.csv").read_text()


@pytest.mark.skipif(not os.environ.get("OPENLEO_NS3_NETWORK"), reason="optional ns-3 backend")
def test_native_packet_metrics_and_routing_comparison(scenario, tmp_path):
    from openleo.benchmark import run_benchmark

    path = _config(
        scenario, tmp_path, sampling_steps_s=[20], refinements=[1], traffic_loads_bps=[10000]
    )
    summary = run_benchmark(
        path, tmp_path / "native", network_backend=os.environ["OPENLEO_NS3_NETWORK"]
    )
    assert summary["packet_evidence"] == "actual_ns3"
    assert len(summary["runs"]) == 1
    rows = [row for row in summary["metrics"] if row["kind"] == "packet"]
    assert rows and any(row["metric"] == "goodput_bps" for row in rows)
    assert {row["routing_model"] for row in rows} == {
        "fixed_capacity",
        "minimum_delay",
        "maximum_rate",
    }
    assert any(row["axis"] == "routing_model" for row in summary["comparisons"])
    html = (tmp_path / "native" / "index.html").read_text()
    assert "<td>0</td><td>0</td>" in html
