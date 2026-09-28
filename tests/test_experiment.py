import json
import os
from dataclasses import replace
from datetime import timedelta
from hashlib import sha256

import pytest
import test_app

scenario = test_app.scenario


def _with_atmosphere(scenario, *, hydrometeors=False):
    from openleo.constellation import parse_constellation, scenario_document

    raw = scenario_document(scenario)
    names = [station["name"] for station in raw["stations"]]
    propagation = {
        "model": "itu_reference",
        "station_heights_amsl_m": {name: 0.0 for name in names},
        "refinement": 1,
    }
    if hydrometeors:
        propagation = {
            **propagation,
            "hydrometeors": {
                "model": "declared_uniform_layers",
                "stations": {
                    name: {
                        "liquid_water_kg_m2": 0.5,
                        "rain_rate_mm_h": 10.0,
                        "rain_top_height_amsl_m": 4000.0,
                        "polarization_tilt_deg": 45.0,
                    }
                    for name in names
                },
            },
        }
    raw = {**raw, "propagation": propagation}
    return parse_constellation(
        raw, scenario.source_path, sha256(json.dumps(raw).encode()).hexdigest()
    )


def test_model_only_experiment_is_portable_hashed_and_time_aligned(scenario, tmp_path):
    from openleo.constellation import load_constellation
    from openleo.experiment import ExperimentConfig, run_experiment
    from openleo.workbench import load_experiment

    source = _with_atmosphere(scenario, hydrometeors=True)
    original = source.source_path.read_bytes()
    output = tmp_path / "study"
    summary = run_experiment(source, ExperimentConfig(start_offset_s=5, duration_s=10), output)
    assert summary["kind"] == "openleo.experiment"
    assert [case["id"] for case in summary["cases"]] == ["free_space", "reference", "hydrometeors"]
    documents = [load_experiment(output / case["bundle_path"]) for case in summary["cases"]]
    assert [document["schema_version"] for document in documents] == ["1", "2", "3"]
    assert all(case["duration_s"] == 10 for case in summary["cases"])
    assert all(not case["packets"] for case in summary["cases"])
    assert all(
        document["timestamps_utc"] == documents[0]["timestamps_utc"] for document in documents
    )
    assert documents[0]["timestamps_utc"] == ["2026-08-30T06:17:05Z", "2026-08-30T06:17:15Z"]
    assert documents[1]["satellites"] == documents[2]["satellites"]
    assert documents[1]["links"][0][0]["cn0_db_hz"] > documents[2]["links"][0][0]["cn0_db_hz"]
    for case in summary["cases"]:
        restored = load_constellation(output / case["bundle_path"] / "scenario.json")
        assert restored.orbit.path.is_file()
        assert restored.time_window.stop_utc - restored.time_window.start_utc == timedelta(
            seconds=10
        )
    manifest = json.loads((output / "manifest.json").read_text())
    for name, checksum in manifest["files"].items():
        assert sha256((output / name).read_bytes()).hexdigest() == checksum
    assert "inputs/orbit.csv" in manifest["files"]
    assert summary["comparisons"]
    assert source.source_path.read_bytes() == original


def test_free_space_source_does_not_invent_amsl_or_weather(scenario, tmp_path):
    from openleo.experiment import ExperimentConfig, run_experiment

    summary = run_experiment(scenario, ExperimentConfig(duration_s=20), tmp_path / "study")
    assert [case["id"] for case in summary["cases"]] == ["free_space"]
    assert "propagation" not in summary["scenario"]


def test_summary_resolves_archived_external_catalog_without_local_paths(scenario, tmp_path):
    from openleo.constellation import load_constellation, parse_constellation
    from openleo.experiment import ExperimentConfig, run_experiment

    external = tmp_path / "private-catalog" / "orbit.csv"
    external.parent.mkdir()
    external.write_bytes(scenario.orbit.path.read_bytes())
    source = tmp_path / "private-source" / "scenario.json"
    source.parent.mkdir()
    raw = json.loads(scenario.source_path.read_text())
    source.write_text(json.dumps({**raw, "orbit": {**raw["orbit"], "path": str(external)}}))
    scenario = load_constellation(source)
    output = tmp_path / "study"
    summary = run_experiment(scenario, ExperimentConfig(duration_s=20), output)
    relocated = tmp_path / "relocated-study"
    output.rename(relocated)

    serialized = (relocated / "experiment-study.json").read_text()
    assert json.loads(serialized) == summary
    assert summary["scenario"]["orbit"]["path"] == "inputs/orbit.csv"
    restored = parse_constellation(
        summary["scenario"], relocated / "experiment-study.json", scenario.source_sha256
    )
    assert restored.orbit.path.read_bytes() == external.read_bytes()
    assert external.parent.name not in serialized
    assert source.parent.name not in serialized
    assert str(tmp_path) not in serialized
    assert (
        summary["provenance"]["source_scenario_sha256"] == sha256(source.read_bytes()).hexdigest()
    )
    assert summary["provenance"]["orbit_sha256"] == sha256(external.read_bytes()).hexdigest()
    assert summary["scenario"]["orbit"]["source_url"] == raw["orbit"]["source_url"]
    assert summary["scenario"]["orbit"]["terms_url"] == raw["orbit"]["terms_url"]


@pytest.mark.parametrize(
    "settings",
    [
        {"packet_mode": "invented"},
        {"duration_s": True},
        {"duration_s": float("nan")},
        {"duration_s": 3601},
        {"offered_load_bps": 1e-320},
        {"flows_per_direction": 5},
        {"acquisition_delay_s": 61},
        {"start_offset_s": -1},
        {"packet_mode": "link"},
        {"backend": "/arbitrary/executable"},
        {"output": "/arbitrary/location"},
    ],
)
def test_invalid_experiment_settings_are_rejected(settings):
    from openleo.experiment import parse_experiment_config

    with pytest.raises(ValueError):
        parse_experiment_config(settings)


def test_unavailable_backend_and_invalid_window_do_not_create_output(scenario, tmp_path):
    from openleo.experiment import ExperimentConfig, run_experiment

    target = tmp_path / "study"
    with pytest.raises(ValueError, match="backend"):
        run_experiment(scenario, ExperimentConfig(packet_mode="network", duration_s=10), target)
    assert not target.exists()
    with pytest.raises(ValueError, match="window"):
        run_experiment(scenario, ExperimentConfig(start_offset_s=1e50, duration_s=1), target)
    assert not target.exists()
    with pytest.raises(ValueError, match="window"):
        run_experiment(scenario, ExperimentConfig(duration_s=30), target)
    assert not target.exists()


def test_existing_output_is_preserved(scenario, tmp_path):
    from openleo.experiment import ExperimentConfig, run_experiment

    target = tmp_path / "study"
    target.mkdir()
    (target / "keep").write_text("user data")
    with pytest.raises(ValueError, match="nonempty"):
        run_experiment(scenario, ExperimentConfig(duration_s=20), target)
    assert (target / "keep").read_text() == "user data"


def test_report_has_real_metrics_and_escapes_scenario_labels(scenario, tmp_path):
    from openleo.experiment import ExperimentConfig, run_experiment

    target = tmp_path / "study"
    summary = run_experiment(
        replace(scenario, name='<script>alert("x")</script>'),
        ExperimentConfig(duration_s=20),
        target,
    )
    html = (target / "index.html").read_text()
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "cases/free_space/explorer.html" in html
    assert "reference rate" in html.lower()
    assert "not measured" in html.lower()
    assert "<svg" in html
    assert summary["configuration"]["packet_mode"] == "none"


@pytest.mark.parametrize(
    "mode,variable", [("link", "OPENLEO_NS3_REPLAY"), ("network", "OPENLEO_NS3_NETWORK")]
)
def test_integrated_packets_use_actual_native_backend(scenario, tmp_path, mode, variable):
    from openleo.experiment import ExperimentConfig, run_experiment

    backend = os.environ.get(variable)
    if not backend:
        pytest.skip(f"set {variable} to the actual native backend")
    options = {"packet_backend" if mode == "link" else "network_backend": backend}
    scenario = replace(scenario, time_window=replace(scenario.time_window, step_s=0.1))
    result = run_experiment(
        scenario,
        ExperimentConfig(
            packet_mode=mode, station_name="Cartagena", norad_id=25544, duration_s=0.1
        ),
        tmp_path / "study",
        **options,
    )
    packets = result["cases"][0]["packets"]
    assert len(packets) == (1 if mode == "link" else 3)
    html = (tmp_path / "study" / "index.html").read_text()
    for record in packets:
        summary = record["summary"]
        rows = summary.get("flow_metrics", summary.get("directions"))
        assert sum(row["offered_packets"] for row in rows) == 6
        assert sum(row["received_in_window_packets"] for row in rows) > 0
        assert record["bundle_path"] + "/packets.csv" in html


def test_experiment_cli_reads_portable_settings(scenario, tmp_path, capsys):
    from openleo.cli import main

    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"duration_s": 10}))
    assert (
        main(
            [
                "experiment",
                str(scenario.source_path),
                "--settings",
                str(settings),
                "--output",
                str(tmp_path / "study"),
            ]
        )
        == 0
    )
    assert "report:" in capsys.readouterr().out
    assert (tmp_path / "study" / "index.html").is_file()


def test_network_cli_missing_bundle_has_controlled_error(tmp_path, capsys):
    from openleo.cli import main

    assert (
        main(
            [
                "network-replay",
                str(tmp_path / "missing"),
                "--backend",
                "missing",
                "--output",
                str(tmp_path / "result"),
            ]
        )
        == 2
    )
    assert "error:" in capsys.readouterr().err
