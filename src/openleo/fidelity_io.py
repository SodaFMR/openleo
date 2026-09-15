"""Verify a completed fidelity study without rerunning orbit or propagation models."""

from __future__ import annotations

import json
import re
from hashlib import sha256
from pathlib import Path, PurePosixPath

from openleo.constellation import _bounded_grid, parse_constellation
from openleo.input import _positive_number, _sha256, _string, _utc
from openleo.propagation_study import CASE_NAMES, _comparison
from openleo.simulation import _timestamps
from openleo.workbench import (
    ARTIFACT_NAMES,
    MAX_ARTIFACT_BYTES,
    _json,
    _read,
    _unique_object,
    load_experiment,
)

_FILES = {"fidelity-study.json", "station-metrics.csv", "route-metrics.csv", "index.html"}
_SUMMARY_FIELDS = {
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
_RUN_FIELDS = {
    "case_id",
    "step_s",
    "propagation_model",
    "bundle_path",
    "manifest_sha256",
    "scenario_sha256",
    "sample_count",
    "duration_s",
}


def _file(directory: Path, relative: str) -> Path:
    path = PurePosixPath(relative)
    if path.is_absolute() or any(part in {"..", "."} for part in path.parts) or "\\" in relative:
        raise ValueError("unsafe study artifact path")
    current = directory
    for part in path.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("symlinked study artifacts are not supported")
    return current


def _load_top(directory):
    manifest = json.loads(
        _read(_file(directory, "manifest.json"), 1_000_000), object_pairs_hook=_unique_object
    )
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema_version") != "1"
        or manifest.get("kind") != "openleo.fidelity-manifest"
        or not isinstance(manifest.get("files"), dict)
        or set(manifest["files"]) != _FILES
    ):
        raise ValueError("invalid fidelity study manifest")
    contents = {}
    for name in sorted(_FILES):
        raw = _read(_file(directory, name), MAX_ARTIFACT_BYTES)
        if sha256(raw).hexdigest() != manifest["files"][name]:
            raise ValueError(f"{name} SHA-256 mismatch")
        contents[name] = raw
    summary = json.loads(contents["fidelity-study.json"], object_pairs_hook=_unique_object)
    _json(summary)  # Reject non-finite JSON numbers at every depth.
    return summary, contents


def _validate_header(summary):
    if not isinstance(summary, dict) or set(summary) != _SUMMARY_FIELDS:
        raise ValueError("invalid fidelity study summary fields")
    if (summary["kind"], summary["schema_version"]) != ("openleo.fidelity-study", "1"):
        raise ValueError("unsupported fidelity study summary")
    _string(summary["name"], "study name")
    _sha256(summary["provenance"]["study_sha256"], "study SHA-256")
    steps = summary["sampling_steps_s"]
    if not isinstance(steps, list) or not 2 <= len(steps) <= 6:
        raise ValueError("invalid sampling steps")
    for step in steps:
        _positive_number(step, "sampling step")
    if steps != sorted(set(steps)) or summary["numerical_reference_step_s"] != steps[0]:
        raise ValueError("invalid numerical reference sampling step")
    runs = summary["runs"]
    if not isinstance(runs, list) or not 6 <= len(runs) <= 144:
        raise ValueError("invalid fidelity run count")
    if not isinstance(summary["limitations"], list) or not summary["limitations"]:
        raise ValueError("study limitations are required")
    for value in summary["limitations"]:
        _string(value, "limitation")
    return steps


def _load_run(directory, run, steps):
    if not isinstance(run, dict) or set(run) != _RUN_FIELDS:
        raise ValueError("invalid fidelity run fields")
    case_id = run["case_id"]
    if not isinstance(case_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,47}", case_id):
        raise ValueError("invalid study case id")
    step = _positive_number(run["step_s"], "run step")
    model = run["propagation_model"]
    if step not in steps or model not in CASE_NAMES:
        raise ValueError("invalid study run model or step")
    expected_path = f"cases/{case_id}/step-{steps.index(step):02d}/{model}"
    if run["bundle_path"] != expected_path:
        raise ValueError("invalid study bundle path")
    child = _file(directory, expected_path)
    manifest = _read(_file(child, "manifest.json"), 1_000_000)
    if sha256(manifest).hexdigest() != run["manifest_sha256"]:
        raise ValueError("child manifest SHA-256 mismatch")
    for name in ARTIFACT_NAMES:
        _file(child, name)
    document = load_experiment(child)
    times = tuple(_utc(value, "timestamp") for value in document["timestamps_utc"])
    scenario = parse_constellation(
        document["scenario"], child / "experiment.json", document["provenance"]["scenario_sha256"]
    )
    grid_step, grid_count = _bounded_grid(scenario)
    if times != tuple(_timestamps(scenario, grid_step, grid_count)):
        raise ValueError("child timestamps do not match the declared time grid")
    duration = (times[-1] - times[0]).total_seconds()
    if (
        not isinstance(run["sample_count"], int)
        or isinstance(run["sample_count"], bool)
        or run["sample_count"] != len(times)
        or run["duration_s"] != duration
        or duration <= 0
        or scenario.time_window.step_s != step
        or document["provenance"]["scenario_sha256"] != run["scenario_sha256"]
        or (model == "free_space") != (document["schema_version"] == "1")
    ):
        raise ValueError("run identity does not match its experiment")
    return document


def _check_scenarios(documents, previous):
    free, reference, refined = (documents[name] for name in CASE_NAMES)
    scenario = free["scenario"]
    for document in (reference, refined):
        if {k: v for k, v in document["scenario"].items() if k != "propagation"} != scenario:
            raise ValueError("propagation cases must differ only in propagation")
        if document["satellites"] != free["satellites"] or document["stations"] != free["stations"]:
            raise ValueError("propagation cases must preserve geometry")
        if document["timestamps_utc"] != free["timestamps_utc"]:
            raise ValueError("propagation cases must preserve the time grid")
    first = reference["scenario"]["propagation"]
    second = refined["scenario"]["propagation"]
    if first["refinement"] not in (1, 2) or second != {
        **first,
        "refinement": first["refinement"] * 2,
    }:
        raise ValueError("inconsistent layer refinement")
    signature = {
        **reference["scenario"],
        "time_window": {k: v for k, v in scenario["time_window"].items() if k != "step_s"},
    }
    if previous is not None and signature != previous:
        raise ValueError("sampling variants must preserve all non-sampling inputs")
    return signature


def load_fidelity_result(run_directory: str | Path) -> dict:
    """Verify manifest, child experiments, comparisons and time-weighted summaries."""
    from openleo.fidelity_study import attach_deltas, derive_metrics, metric_csv

    directory = Path(run_directory)
    try:
        if directory.is_symlink():
            raise ValueError("symlinked study directory is not supported")
        summary, contents = _load_top(directory)
        steps = _validate_header(summary)
        runs = summary["runs"]
        identities = [(r["case_id"], r["step_s"], r["propagation_model"]) for r in runs]
        case_ids = tuple(dict.fromkeys(key[0] for key in identities))
        if not 1 <= len(case_ids) <= 8 or len(set(identities)) != len(identities):
            raise ValueError("invalid or duplicate study runs")
        expected = [
            (case, step, model) for case in case_ids for step in steps for model in CASE_NAMES
        ]
        if identities != expected:
            raise ValueError("study runs must form the complete ordered sampling/model grid")
        station_rows, route_rows, comparisons = [], [], []
        total_samples = 0
        for case_id in case_ids:
            previous = None
            for step in steps:
                documents = {}
                for model in CASE_NAMES:
                    run = runs[identities.index((case_id, step, model))]
                    document = _load_run(directory, run, steps)
                    if document["provenance"]["software"] != summary["provenance"]["software"]:
                        raise ValueError("inconsistent study software provenance")
                    total_samples += (
                        len(document["satellites"])
                        * len(document["stations"])
                        * len(document["timestamps_utc"])
                    )
                    if total_samples > 2_000_000:
                        raise ValueError("study exceeds total link-sample budget")
                    documents[model] = document
                    stations, routes = derive_metrics(document)
                    identity = {"case_id": case_id, "step_s": step, "propagation_model": model}
                    station_rows.extend({**identity, **row} for row in stations)
                    route_rows.extend({**identity, **row} for row in routes)
                previous = _check_scenarios(documents, previous)
                comparisons.append(
                    {
                        "case_id": case_id,
                        "step_s": step,
                        **_comparison(documents["reference"], documents["refined"]),
                    }
                )
        station_rows = attach_deltas(station_rows, "best_link_integrated_bits", "station_index")
        route_rows = attach_deltas(route_rows, "integrated_bottleneck_bits", "routing_model")
        for key, expected_rows in (
            ("station_metrics", station_rows),
            ("route_metrics", route_rows),
            ("refinement_comparisons", comparisons),
        ):
            if _json(summary[key]) != _json(expected_rows):
                raise ValueError(f"{key} does not match the child experiments")
        for name, rows in (
            ("station-metrics.csv", station_rows),
            ("route-metrics.csv", route_rows),
        ):
            if contents[name] != metric_csv(rows).encode("utf-8"):
                raise ValueError(f"{name} does not match study metrics")
        return summary
    except (OSError, KeyError, TypeError, OverflowError, UnicodeError, RecursionError) as exc:
        raise ValueError("invalid fidelity study bundle") from exc
