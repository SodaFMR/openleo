import copy
import csv
import json
from dataclasses import FrozenInstanceError, replace
from hashlib import sha256
from io import StringIO
from pathlib import Path

import pytest

from openleo.cli import main
from openleo.constellation import parse_constellation, simulate_constellation
from openleo.fidelity_study import (
    attach_deltas,
    derive_metrics,
    load_fidelity_study,
    metric_csv,
    run_fidelity_study,
)
from openleo.network import add_network
from openleo.workbench import load_experiment


def _scenario(tmp_path, *, stop="2026-09-10T12:00:10Z"):
    source = Path("examples/constellations/iridium_global_reference.json")
    raw = json.loads(source.read_text(encoding="utf-8"))
    raw["orbit"]["path"] = str((source.parent / raw["orbit"]["path"]).resolve())
    raw["time_window"]["stop_utc"] = stop
    raw["time_window"]["step_s"] = 5
    target = tmp_path / "scenario.json"
    target.write_text(json.dumps(raw), encoding="utf-8")
    return target


def _study(tmp_path, *, steps=(5, 10), cases=None, scenario_stop="2026-09-10T12:00:10Z"):
    _scenario(tmp_path, stop=scenario_stop)
    raw = {
        "schema_version": "1",
        "name": "Tiny fidelity study",
        "cases": cases or [{"id": "tiny", "scenario": "scenario.json"}],
        "sampling_steps_s": list(steps),
    }
    target = tmp_path / "study.json"
    target.write_text(json.dumps(raw), encoding="utf-8")
    return target


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda raw: raw.pop("name"), "name"),
        (lambda raw: raw.update(extra=True), "extra"),
        (lambda raw: raw.update(schema_version="2"), "schema_version"),
        (lambda raw: raw.update(cases=[]), "cases"),
        (lambda raw: raw.update(cases=[{"id": "Bad id", "scenario": "scenario.json"}]), "id"),
        (lambda raw: raw.update(sampling_steps_s=[5]), "sampling_steps_s"),
        (lambda raw: raw.update(sampling_steps_s=[5, True]), "sampling_steps_s"),
        (lambda raw: raw.update(sampling_steps_s=[5, 5]), "distinct"),
        (lambda raw: raw.update(sampling_steps_s=[5, 5.0000001]), "microsecond"),
    ],
)
def test_load_rejects_malformed_studies(tmp_path, change, message):
    path = _study(tmp_path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    change(raw)
    path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        load_fidelity_study(path)


def test_load_rejects_duplicate_json_keys_and_case_ids(tmp_path):
    path = _study(tmp_path)
    path.write_text(
        '{"schema_version":"1","name":"first","name":"second",'
        '"cases":[],"sampling_steps_s":[5,10]}',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="duplicate"):
        load_fidelity_study(path)

    path = _study(
        tmp_path,
        cases=[
            {"id": "same", "scenario": "scenario.json"},
            {"id": "same", "scenario": "scenario.json"},
        ],
    )
    with pytest.raises(ValueError, match="unique"):
        load_fidelity_study(path)


@pytest.mark.parametrize(
    "case_id",
    (
        "con",
        "prn",
        "aux",
        "nul",
        *(f"com{i}" for i in range(1, 10)),
        *(f"lpt{i}" for i in range(1, 10)),
    ),
)
def test_load_and_cli_reject_windows_reserved_case_ids_before_output(tmp_path, capsys, case_id):
    path = _study(tmp_path, cases=[{"id": case_id, "scenario": "scenario.json"}])
    output = tmp_path / "result"

    with pytest.raises(ValueError, match="reserved"):
        load_fidelity_study(path)
    assert main(["fidelity-study", str(path), "--output", str(output)]) == 2
    assert "reserved" in capsys.readouterr().err
    assert not output.exists()


def test_loaded_study_is_frozen_and_sorts_steps_without_mutating_json(tmp_path):
    path = _study(tmp_path, steps=(10, 5))
    before = path.read_bytes()

    study = load_fidelity_study(path)

    assert study.path == path.resolve()
    assert study.sampling_steps_s == (5.0, 10.0)
    assert study.cases[0].id == "tiny"
    assert path.read_bytes() == before
    with pytest.raises(FrozenInstanceError):
        study.name = "changed"


def _analytic_document():
    source = Path("examples/scenarios/iss_cartagena.json")
    original = json.loads(source.read_text(encoding="utf-8"))
    raw = {
        "name": "Duration metric fixture",
        "orbit": original["orbit"],
        "stations": [
            original["ground_station"],
            {**original["ground_station"], "name": "Neighbor", "longitude_deg": -0.95},
        ],
        "time_window": {
            **original["time_window"],
            "start_utc": "2026-08-30T06:16:00Z",
            "stop_utc": "2026-08-30T06:16:10Z",
            "step_s": 4,
        },
        "radio_link": {**original["radio_link"], "eirp_dbw": -100.0},
        "adaptation": {
            "symbol_rate_baud": 500_000.0,
            "rolloff": 0.2,
            "implementation_margin_db": 1.0,
            "hysteresis_db": 0.5,
        },
        "network": {
            "source_station": "Cartagena",
            "target_station": "Neighbor",
            "isl_max_range_m": 5_000_000.0,
            "isl_capacity_bps": 10_000_000.0,
            "fixed_capacity_bps": 1_000_000.0,
        },
    }
    scenario = parse_constellation(raw, source, sha256(json.dumps(raw).encode()).hexdigest())
    document = simulate_constellation(scenario)
    del document["timestamps_utc"][2]
    del document["links"][2]
    for satellite in document["satellites"]:
        del satellite["positions_ecef_m"][2]
    first = next(link for link in document["links"][0] if link["station_index"] == 0)
    first.update(modcod="QPSK 1/4", rate_bps=100.0, margin_db=0.0)
    document["links"][2] = [link for link in document["links"][2] if link["station_index"] != 0]
    document["statistics"][0].update(
        visible_sample_fraction=2 / 3,
        usable_sample_fraction=1 / 3,
        best_link_integrated_bits=400.0,
        fixed_baseline_integrated_bits=10_000_000.0,
        handover_count=0,
    )
    return add_network(document)


def test_metrics_use_left_held_actual_intervals_and_do_not_mutate_input():
    document = _analytic_document()
    before = copy.deepcopy(document)

    stations, routes = derive_metrics(document)

    row = stations[0]
    assert row["visible_sample_fraction"] == pytest.approx(2 / 3)
    assert row["usable_sample_fraction"] == pytest.approx(1 / 3)
    assert row["best_link_integrated_bits"] == 400.0
    assert row["visible_duration_s"] == 10.0
    assert row["usable_duration_s"] == 4.0
    assert row["rf_outage_duration_s"] == 6.0
    assert row["out_of_view_duration_s"] == 0.0
    assert row["visible_time_fraction"] == 1.0
    assert row["usable_time_fraction"] == 0.4
    assert row["mean_best_rate_bps"] == 40.0
    assert len(routes) == 3
    assert document == before


def test_delta_attachment_is_signed_and_does_not_mutate_scalar_rows():
    rows = [
        {
            "case_id": "a",
            "step_s": 5.0,
            "propagation_model": "free_space",
            "station_index": 0,
            "bits": 150.0,
        },
        {
            "case_id": "a",
            "step_s": 10.0,
            "propagation_model": "free_space",
            "station_index": 0,
            "bits": 160.0,
        },
        {
            "case_id": "a",
            "step_s": 5.0,
            "propagation_model": "reference",
            "station_index": 0,
            "bits": 100.0,
        },
        {
            "case_id": "a",
            "step_s": 10.0,
            "propagation_model": "reference",
            "station_index": 0,
            "bits": 120.0,
        },
    ]
    before = copy.deepcopy(rows)

    result = attach_deltas(rows, "bits", "station_index")

    assert result[2]["delta_bits_to_finest"] == 0.0
    assert result[3]["delta_bits_to_finest"] == 20.0
    assert result[3]["relative_delta_percent_to_finest"] == 20.0
    assert result[2]["delta_bits_to_free_space"] == -50.0
    assert result[0]["delta_bits_to_free_space"] == 0.0
    assert rows == before


def test_metric_csv_is_deterministic_and_neutralizes_spreadsheet_formulas():
    rows = [{"name": "=cmd", "note": "safe", "value": -2.5, "missing": None}]
    text = metric_csv(rows)
    parsed = next(csv.DictReader(StringIO(text)))

    assert text.splitlines()[0] == "missing,name,note,value"
    assert parsed == {"missing": "", "name": "'=cmd", "note": "safe", "value": "-2.5"}


def test_total_budget_is_checked_before_output_creation(tmp_path, monkeypatch):
    study = load_fidelity_study(
        _study(
            tmp_path,
            steps=(5, 10),
            scenario_stop="2026-09-10T14:00:00Z",
        )
    )
    monkeypatch.setattr("openleo.fidelity_study.load_catalog", lambda *args: (None,) * 86)
    output = tmp_path / "result"

    with pytest.raises(ValueError, match="2000000"):
        run_fidelity_study(study, output)

    assert not output.exists()


def test_nonempty_output_is_preserved_and_rejected_before_running(tmp_path, monkeypatch):
    study = load_fidelity_study(_study(tmp_path))
    output = tmp_path / "result"
    output.mkdir()
    sentinel = output / "keep.txt"
    sentinel.write_text("mine", encoding="utf-8")
    monkeypatch.setattr(
        "openleo.fidelity_study.run_propagation_study",
        lambda *args: pytest.fail("output collision reached simulation"),
    )

    with pytest.raises(ValueError, match="nonempty"):
        run_fidelity_study(study, output)

    assert sentinel.read_text(encoding="utf-8") == "mine"


def test_run_revalidates_replaced_study_before_catalog_or_output(tmp_path, monkeypatch):
    study = load_fidelity_study(_study(tmp_path))
    case = study.cases[0]
    invalid = (
        replace(study, cases=(replace(case, id="../../escaped"),)),
        replace(study, cases=(case, case)),
        replace(study, sampling_steps_s=()),
        replace(study, sampling_steps_s=(10.0, 5.0)),
        replace(
            study,
            cases=(
                replace(
                    case,
                    scenario=replace(
                        case.scenario,
                        time_window=replace(
                            case.scenario.time_window,
                            stop_utc=case.scenario.time_window.start_utc,
                        ),
                    ),
                ),
            ),
        ),
    )

    def forbidden(*args):
        pytest.fail("invalid replaced study reached catalog loading")

    monkeypatch.setattr("openleo.fidelity_study.load_catalog", forbidden)
    for index, candidate in enumerate(invalid):
        output = tmp_path / f"invalid-{index}"
        with pytest.raises(ValueError):
            run_fidelity_study(candidate, output)
        assert not output.exists()
    assert not (tmp_path / "escaped").exists()


def test_tiny_real_study_writes_verified_atomic_bundle(tmp_path):
    study = load_fidelity_study(_study(tmp_path))
    before = copy.deepcopy(study)
    output = tmp_path / "result"

    summary = run_fidelity_study(study, output)

    assert study == before
    assert set(summary) == {
        "schema_version",
        "kind",
        "name",
        "provenance",
        "sampling_steps_s",
        "numerical_reference_step_s",
        "runs",
        "station_metrics",
        "route_metrics",
        "refinement_comparisons",
        "limitations",
    }
    assert summary["sampling_steps_s"] == [5.0, 10.0]
    assert summary["numerical_reference_step_s"] == 5.0
    assert len(summary["runs"]) == 6
    assert len(summary["station_metrics"]) == 24
    assert len(summary["route_metrics"]) == 18
    assert json.loads((output / "fidelity-study.json").read_text()) == summary
    manifest = json.loads((output / "manifest.json").read_text())
    assert set(manifest["files"]) == {
        "fidelity-study.json",
        "station-metrics.csv",
        "route-metrics.csv",
        "index.html",
    }
    for name, checksum in manifest["files"].items():
        assert sha256((output / name).read_bytes()).hexdigest() == checksum
    for run in summary["runs"]:
        child = output / run["bundle_path"]
        document = load_experiment(child)
        assert len(document["timestamps_utc"]) == run["sample_count"]
        assert sha256((child / "manifest.json").read_bytes()).hexdigest() == run["manifest_sha256"]
        assert not Path(run["bundle_path"]).is_absolute()
