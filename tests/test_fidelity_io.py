import json
from datetime import timedelta
from hashlib import sha256
from pathlib import Path
from shutil import copytree

import pytest


def _completed_study(directory, *, stop="2026-09-10T12:01:00Z", steps=(30, 60)):
    from openleo.fidelity_study import load_fidelity_study, run_fidelity_study

    directory.mkdir(parents=True, exist_ok=True)
    original = Path("examples/constellations/iridium_global_reference.json")
    source = json.loads(original.read_text())
    scenario = {
        **source,
        "orbit": {
            **source["orbit"],
            "path": str((original.parent / source["orbit"]["path"]).resolve()),
        },
        "time_window": {**source["time_window"], "stop_utc": stop},
    }
    (directory / "scenario.json").write_text(json.dumps(scenario))
    config = directory / "study.json"
    config.write_text(
        json.dumps(
            {
                "schema_version": "1",
                "name": "Reference sampling check",
                "cases": [{"id": "reference", "scenario": "scenario.json"}],
                "sampling_steps_s": list(steps),
            }
        )
    )
    output = directory / "completed"
    expected = run_fidelity_study(load_fidelity_study(config), output)
    return output, expected


@pytest.fixture(scope="module")
def completed_study(tmp_path_factory):
    return _completed_study(tmp_path_factory.mktemp("fidelity-input"))


def _rewrite_summary(directory, change):
    path = directory / "fidelity-study.json"
    summary = json.loads(path.read_text())
    change(summary)
    path.write_text(json.dumps(summary))
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["files"][path.name] = sha256(path.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest))


def _write_child(directory, run, document):
    from openleo.workbench import _json

    child = directory / run["bundle_path"]
    experiment = child / "experiment.json"
    experiment.write_text(_json(document) + "\n")
    manifest_path = child / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["files"]["experiment.json"] = sha256(experiment.read_bytes()).hexdigest()
    manifest_path.write_text(_json(manifest) + "\n")
    run["manifest_sha256"] = sha256(manifest_path.read_bytes()).hexdigest()


def _write_top(directory, summary, *, metrics=False):
    from openleo.fidelity_study import metric_csv
    from openleo.workbench import _json

    contents = {"fidelity-study.json": _json(summary) + "\n"}
    if metrics:
        contents.update(
            {
                "station-metrics.csv": metric_csv(summary["station_metrics"]),
                "route-metrics.csv": metric_csv(summary["route_metrics"]),
            }
        )
    for name, content in contents.items():
        (directory / name).write_text(content)
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    for name in contents:
        manifest["files"][name] = sha256((directory / name).read_bytes()).hexdigest()
    manifest_path.write_text(_json(manifest) + "\n")


def test_loader_roundtrips_the_actual_study(completed_study):
    from openleo.fidelity_io import load_fidelity_result

    directory, expected = completed_study
    assert load_fidelity_result(directory) == expected


def test_loader_rejects_missing_or_invalid_manifest(tmp_path):
    from openleo.fidelity_io import load_fidelity_result

    with pytest.raises(ValueError):
        load_fidelity_result(tmp_path)
    (tmp_path / "manifest.json").write_text('{"kind":"unrelated"}')
    with pytest.raises(ValueError):
        load_fidelity_result(tmp_path)


@pytest.mark.parametrize("filename", ["index.html", "station-metrics.csv", "route-metrics.csv"])
def test_loader_checks_every_top_level_file_hash(completed_study, tmp_path, filename):
    from openleo.fidelity_io import load_fidelity_result

    output = tmp_path / "study"
    copytree(completed_study[0], output)
    path = output / filename
    path.write_bytes(path.read_bytes() + b"x")
    with pytest.raises(ValueError, match="SHA-256"):
        load_fidelity_result(output)


@pytest.mark.parametrize(
    "change",
    [
        lambda d: d["runs"][0].update(bundle_path="../outside"),
        lambda d: d["runs"][0].update(sample_count=99),
        lambda d: d["runs"][0].update(scenario_sha256="0" * 64),
        lambda d: d["station_metrics"][0].update(usable_duration_s=1_000_000),
        lambda d: d["station_metrics"][0].update(delta_bits_to_finest=123456),
        lambda d: d["route_metrics"].pop(),
        lambda d: d["refinement_comparisons"][0].update(link_count=123456),
        lambda d: d["runs"].append(d["runs"][0]),
    ],
)
def test_loader_rejects_rehashed_inconsistent_records(completed_study, tmp_path, change):
    from openleo.fidelity_io import load_fidelity_result

    output = tmp_path / "study"
    copytree(completed_study[0], output)
    _rewrite_summary(output, change)
    with pytest.raises(ValueError):
        load_fidelity_result(output)


def test_loader_rejects_symlinked_artifacts(completed_study, tmp_path):
    from openleo.fidelity_io import load_fidelity_result

    output = tmp_path / "study"
    copytree(completed_study[0], output)
    path = output / "station-metrics.csv"
    backup = tmp_path / "original.csv"
    path.replace(backup)
    try:
        path.symlink_to(backup)
    except OSError:
        pytest.skip("platform does not allow symlink creation")
    with pytest.raises(ValueError, match="symlink"):
        load_fidelity_result(output)


def test_loader_rejects_rehashed_forged_child_statistics(completed_study, tmp_path):
    from openleo.fidelity_io import load_fidelity_result
    from openleo.fidelity_study import attach_deltas

    output = tmp_path / "study"
    copytree(completed_study[0], output)
    summary = json.loads((output / "fidelity-study.json").read_text())
    run = summary["runs"][0]
    child = output / run["bundle_path"] / "experiment.json"
    document = json.loads(child.read_text())
    document["statistics"][0]["best_link_integrated_bits"] = 123_456_789.0
    document["network"]["summary"]["minimum_delay"]["integrated_bottleneck_bits"] = 987_654_321.0
    _write_child(output, run, document)

    delta_fields = {
        "delta_bits_to_finest",
        "relative_delta_percent_to_finest",
        "delta_bits_to_free_space",
    }
    station_rows = [
        {key: value for key, value in row.items() if key not in delta_fields}
        for row in summary["station_metrics"]
    ]
    station = next(
        row
        for row in station_rows
        if (row["case_id"], row["step_s"], row["propagation_model"], row["station_index"])
        == (run["case_id"], run["step_s"], run["propagation_model"], 0)
    )
    station["best_link_integrated_bits"] = 123_456_789.0
    station["mean_best_rate_bps"] = 123_456_789.0 / run["duration_s"]
    summary["station_metrics"] = attach_deltas(
        station_rows, "best_link_integrated_bits", "station_index"
    )

    route_rows = [
        {key: value for key, value in row.items() if key not in delta_fields}
        for row in summary["route_metrics"]
    ]
    route = next(
        row
        for row in route_rows
        if (row["case_id"], row["step_s"], row["propagation_model"], row["routing_model"])
        == (run["case_id"], run["step_s"], run["propagation_model"], "minimum_delay")
    )
    route["integrated_bottleneck_bits"] = 987_654_321.0
    summary["route_metrics"] = attach_deltas(
        route_rows, "integrated_bottleneck_bits", "routing_model"
    )
    _write_top(output, summary, metrics=True)

    with pytest.raises(ValueError, match="statistics|summary"):
        load_fidelity_result(output)


def test_loader_rejects_rehashed_uniform_timestamp_shift(completed_study, tmp_path):
    from openleo.fidelity_io import load_fidelity_result
    from openleo.input import _utc

    output = tmp_path / "study"
    copytree(completed_study[0], output)
    summary = json.loads((output / "fidelity-study.json").read_text())
    run = summary["runs"][0]
    child = output / run["bundle_path"] / "experiment.json"
    document = json.loads(child.read_text())
    document["timestamps_utc"] = [
        (_utc(value, "timestamp") + timedelta(days=1)).isoformat().replace("+00:00", "Z")
        for value in document["timestamps_utc"]
    ]
    _write_child(output, run, document)
    _write_top(output, summary)

    with pytest.raises(ValueError, match="time grid"):
        load_fidelity_result(output)


def test_run_loader_rejects_rehashed_interior_time_on_short_final_grid(tmp_path):
    from openleo.fidelity_io import _load_run
    from openleo.input import _utc

    output, summary = _completed_study(
        tmp_path / "input", stop="2026-09-10T12:01:05Z", steps=(30, 60)
    )
    run = summary["runs"][0]
    child = output / run["bundle_path"] / "experiment.json"
    document = json.loads(child.read_text())
    assert document["timestamps_utc"][-2:] == [
        "2026-09-10T12:01:00Z",
        "2026-09-10T12:01:05Z",
    ]
    document["timestamps_utc"][1] = (
        (_utc(document["timestamps_utc"][1], "timestamp") - timedelta(seconds=1))
        .isoformat()
        .replace("+00:00", "Z")
    )
    _write_child(output, run, document)

    with pytest.raises(ValueError, match="time grid"):
        _load_run(output, run, summary["sampling_steps_s"])


def test_fidelity_cli_rejects_missing_input_without_traceback(tmp_path, capsys):
    from openleo.cli import main

    assert (
        main(["fidelity-study", str(tmp_path / "missing.json"), "--output", str(tmp_path / "run")])
        == 2
    )
    error = capsys.readouterr().err
    assert "error:" in error
    assert "Traceback" not in error
    assert not (tmp_path / "run").exists()


def test_fidelity_cli_writes_a_verified_visual_study(completed_study, tmp_path, capsys):
    from openleo.cli import main
    from openleo.fidelity_io import load_fidelity_result

    source = completed_study[0].parent / "study.json"
    output = tmp_path / "cli"
    assert main(["fidelity-study", str(source), "--output", str(output)]) == 0
    summary = load_fidelity_result(output)
    assert len(summary["runs"]) == 6
    assert (output / "index.html").is_file()
    assert "report:" in capsys.readouterr().out
