"""Bounded, reproducible multi-case constellation fidelity studies."""

from __future__ import annotations

import csv
import json
import shutil
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import timedelta
from hashlib import sha256
from importlib.metadata import version
from io import StringIO
from itertools import pairwise
from math import fsum, isclose
from pathlib import Path
from re import fullmatch
from tempfile import mkdtemp

from skyfield.api import load

from openleo.catalog import load_catalog
from openleo.constellation import (
    MAX_LINK_SAMPLES,
    MAX_SAMPLES,
    ConstellationScenario,
    _bounded_grid,
    load_constellation,
    parse_constellation,
)
from openleo.constellation import _statistics as _station_statistics
from openleo.input import _object, _positive_number, _string, _utc
from openleo.network import ROUTE_MODELS
from openleo.network import _summary as _route_summary
from openleo.propagation_study import CASE_NAMES, run_propagation_study
from openleo.workbench import _json, _validate_document, write_workbench

MAX_STUDY_BYTES = 1_000_000
MAX_TOTAL_LINK_SAMPLES = 2_000_000
_STUDY_KEYS = frozenset(("schema_version", "name", "cases", "sampling_steps_s"))
_CASE_KEYS = frozenset(("id", "scenario"))
_WINDOWS_RESERVED_CASE_IDS = frozenset(
    (
        "con",
        "prn",
        "aux",
        "nul",
        *(f"com{i}" for i in range(1, 10)),
        *(f"lpt{i}" for i in range(1, 10)),
    )
)


@dataclass(frozen=True, slots=True)
class _FidelityCase:
    id: str
    scenario: ConstellationScenario


@dataclass(frozen=True, slots=True)
class FidelityStudy:
    """Validated immutable study configuration."""

    name: str
    path: Path
    cases: tuple[_FidelityCase, ...]
    sampling_steps_s: tuple[float, ...]
    source_sha256: str


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _read(path: Path) -> bytes:
    try:
        with path.open("rb") as file:
            source = file.read(MAX_STUDY_BYTES + 1)
    except OSError as exc:
        raise ValueError(f"{path}: could not load study: {exc}") from exc
    if len(source) > MAX_STUDY_BYTES:
        raise ValueError(f"{path}: study JSON exceeds {MAX_STUDY_BYTES} bytes")
    return source


def _sampling_steps(raw) -> tuple[float, ...]:
    if not isinstance(raw, list) or not 2 <= len(raw) <= 6:
        raise ValueError("sampling_steps_s must be a list with 2..6 entries")
    steps = tuple(_positive_number(value, "sampling_steps_s") for value in raw)
    for value in steps:
        represented = timedelta(seconds=value).total_seconds()
        if represented <= 0.0 or not isclose(represented, value, rel_tol=0.0, abs_tol=1e-12):
            raise ValueError(
                f"sampling_steps_s value {value!r} must be exactly representable at "
                "microsecond resolution"
            )
    if len(set(steps)) != len(steps):
        raise ValueError("sampling_steps_s values must be distinct")
    return tuple(sorted(steps))


def load_fidelity_study(path: str | Path) -> FidelityStudy:
    """Load all local scenarios and return one immutable validated study."""
    study_path = Path(path).resolve()
    source = _read(study_path)
    try:
        raw = json.loads(source.decode("utf-8"), object_pairs_hook=_unique_object)
        data = _object(raw, _STUDY_KEYS, "study")
        if data["schema_version"] != "1":
            raise ValueError("schema_version must be '1'")
        cases_raw = data["cases"]
        if not isinstance(cases_raw, list) or not 1 <= len(cases_raw) <= 8:
            raise ValueError("cases must be a list with 1..8 entries")
        cases = []
        identifiers = set()
        for index, raw_case in enumerate(cases_raw):
            case = _object(raw_case, _CASE_KEYS, f"cases[{index}]")
            identifier = _string(case["id"], f"cases[{index}].id")
            if fullmatch(r"[a-z0-9][a-z0-9_-]{0,47}", identifier) is None:
                raise ValueError(f"cases[{index}].id must match [a-z0-9][a-z0-9_-]{{0,47}}")
            if identifier in _WINDOWS_RESERVED_CASE_IDS:
                raise ValueError(f"cases[{index}].id {identifier!r} is a reserved filename")
            if identifier in identifiers:
                raise ValueError("case ids must be unique")
            identifiers.add(identifier)
            scenario_name = _string(case["scenario"], f"cases[{index}].scenario")
            if Path(scenario_name).is_absolute():
                raise ValueError(f"cases[{index}].scenario must be a relative local filename")
            scenario = load_constellation((study_path.parent / scenario_name).resolve())
            if scenario.propagation is None or scenario.propagation.model != "itu_reference":
                raise ValueError(f"case {identifier!r} requires itu_reference propagation")
            if scenario.propagation.refinement not in (1, 2):
                raise ValueError(f"case {identifier!r} propagation refinement must be 1 or 2")
            cases.append(_FidelityCase(identifier, scenario))
        return FidelityStudy(
            name=_string(data["name"], "name"),
            path=study_path,
            cases=tuple(cases),
            sampling_steps_s=_sampling_steps(data["sampling_steps_s"]),
            source_sha256=sha256(source).hexdigest(),
        )
    except UnicodeDecodeError as exc:
        raise ValueError(f"{study_path}: study JSON must be UTF-8") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"{study_path}: invalid JSON: {exc.msg}") from exc
    except (KeyError, TypeError, OverflowError) as exc:
        raise ValueError(f"{study_path}: invalid fidelity study: {exc}") from exc


def derive_metrics(document: Mapping) -> tuple[tuple[dict, ...], tuple[dict, ...]]:
    """Derive sample and left-held duration metrics from a verified experiment."""
    _validate_document(document)
    timestamps = tuple(_utc(value, "timestamps_utc") for value in document["timestamps_utc"])
    intervals = tuple((end - start).total_seconds() for start, end in pairwise(timestamps))
    duration_s = fsum(intervals)
    if duration_s <= 0.0:
        raise ValueError("metrics require a positive-duration time window")
    frames = document["links"]
    scenario = parse_constellation(
        document["scenario"], Path("experiment.json"), document["provenance"]["scenario_sha256"]
    )
    station_statistics = _station_statistics(scenario, timestamps, frames)
    if station_statistics != document["statistics"]:
        raise ValueError("station statistics do not match the link frames")
    route_statistics = _route_summary(document["network"]["frames"], timestamps)
    if route_statistics != document["network"]["summary"]:
        raise ValueError("network summary does not match the route frames")
    station_rows = []
    for station_index, statistics in enumerate(station_statistics):
        visible = tuple(
            any(link["station_index"] == station_index for link in frame) for frame in frames
        )
        usable = tuple(
            any(link["station_index"] == station_index and link["rate_bps"] > 0.0 for link in frame)
            for frame in frames
        )
        visible_duration = fsum(seconds for state, seconds in zip(visible, intervals) if state)
        usable_duration = fsum(seconds for state, seconds in zip(usable, intervals) if state)
        station_rows.append(
            {
                **statistics,
                "visible_duration_s": visible_duration,
                "usable_duration_s": usable_duration,
                "rf_outage_duration_s": visible_duration - usable_duration,
                "out_of_view_duration_s": duration_s - visible_duration,
                "visible_time_fraction": visible_duration / duration_s,
                "usable_time_fraction": usable_duration / duration_s,
                "mean_best_rate_bps": statistics["best_link_integrated_bits"] / duration_s,
            }
        )
    route_rows = []
    for model in ROUTE_MODELS:
        routes = tuple(frame[model] for frame in document["network"]["frames"])
        connected = tuple(route is not None for route in routes)
        bits = route_statistics[model]["integrated_bottleneck_bits"]
        connected_duration = fsum(seconds for state, seconds in zip(connected, intervals) if state)
        route_rows.append(
            {
                "routing_model": model,
                **route_statistics[model],
                "connected_duration_s": connected_duration,
                "disconnected_duration_s": duration_s - connected_duration,
                "connected_time_fraction": connected_duration / duration_s,
                "mean_bottleneck_bps": bits / duration_s,
            }
        )
    return tuple(station_rows), tuple(route_rows)


def attach_deltas(rows: Iterable[Mapping], bits_field: str, identity_field: str) -> list[dict]:
    """Return independent rows with signed deltas to finest-grid and free-space peers."""
    scalar_rows = [dict(row) for row in rows]
    group_steps = {}
    free_space = {}
    for row in scalar_rows:
        group = (row["case_id"], row["propagation_model"], row[identity_field])
        group_steps[group] = min(row["step_s"], group_steps.get(group, row["step_s"]))
        if row["propagation_model"] == "free_space":
            free_space[(row["case_id"], row["step_s"], row[identity_field])] = row[bits_field]
    finest = {
        (row["case_id"], row["propagation_model"], row[identity_field]): row[bits_field]
        for row in scalar_rows
        if row["step_s"]
        == group_steps[(row["case_id"], row["propagation_model"], row[identity_field])]
    }
    result = []
    for row in scalar_rows:
        group = (row["case_id"], row["propagation_model"], row[identity_field])
        baseline = finest[group]
        value = row[bits_field]
        delta = value - baseline
        free_value = free_space[(row["case_id"], row["step_s"], row[identity_field])]
        result.append(
            {
                **row,
                "delta_bits_to_finest": delta,
                "relative_delta_percent_to_finest": None
                if baseline == 0.0
                else 100.0 * delta / baseline,
                "delta_bits_to_free_space": value - free_value,
            }
        )
    return result


def _csv_value(value):
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return format(value, ".15g")
    if (
        isinstance(value, str)
        and value
        and (
            value[0] in "=+-@"
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
        )
    ):
        return "'" + value
    return value


def metric_csv(rows: Iterable[Mapping]) -> str:
    """Serialize metric rows with deterministic columns and spreadsheet-safe text."""
    records = tuple(dict(row) for row in rows)
    if not records:
        raise ValueError("metric CSV requires at least one row")
    fields = tuple(sorted({field for row in records for field in row}))
    output = StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    writer.writerows({field: _csv_value(row.get(field)) for field in fields} for row in records)
    return output.getvalue()


def _configured_scenario(case: _FidelityCase, step_s: float) -> ConstellationScenario:
    return replace(case.scenario, time_window=replace(case.scenario.time_window, step_s=step_s))


def _check_output(directory: Path) -> None:
    if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
        raise ValueError("output path must be a directory, not a file or symbolic link")
    if directory.exists() and any(directory.iterdir()):
        raise ValueError("refusing to replace an existing nonempty output directory")


def _validate_loaded_study(study: FidelityStudy) -> None:
    if not isinstance(study, FidelityStudy):
        raise TypeError("study must be loaded with load_fidelity_study")
    try:
        reloaded = load_fidelity_study(study.path)
    except TypeError as exc:
        raise ValueError("study path must identify its original configuration") from exc
    if reloaded != study:
        raise ValueError("study must match its validated configuration and scenarios")
    for case in study.cases:
        for step_s in study.sampling_steps_s:
            _bounded_grid(_configured_scenario(case, step_s))


def _preflight(study: FidelityStudy) -> None:
    timescale = load.timescale(builtin=True)
    total = 0
    for case in study.cases:
        satellite_count = len(load_catalog(case.scenario.orbit, timescale))
        for step_s in study.sampling_steps_s:
            scenario = _configured_scenario(case, step_s)
            _, sample_count = _bounded_grid(scenario)
            samples = satellite_count * len(scenario.stations) * sample_count
            if sample_count > MAX_SAMPLES or samples > MAX_LINK_SAMPLES:
                raise ValueError(
                    f"case {case.id!r} step_s={step_s!r} produces {samples} "
                    f"satellite-station-samples; limit is {MAX_LINK_SAMPLES}"
                )
            total += samples * len(CASE_NAMES)
            if total > MAX_TOTAL_LINK_SAMPLES:
                raise ValueError(
                    f"study produces {total} link samples across propagation variants; "
                    f"limit is {MAX_TOTAL_LINK_SAMPLES}"
                )


def _identified(rows, case_id: str, step_s: float, propagation_model: str):
    return [
        {
            "case_id": case_id,
            "step_s": step_s,
            "propagation_model": propagation_model,
            **row,
        }
        for row in rows
    ]


def _build_study(study: FidelityStudy, directory: Path) -> dict:
    runs = []
    station_rows = []
    route_rows = []
    comparisons = []
    for case in study.cases:
        for step_index, step_s in enumerate(study.sampling_steps_s):
            result = run_propagation_study(_configured_scenario(case, step_s))
            comparisons.append({"case_id": case.id, "step_s": step_s, **result["comparison"]})
            for propagation_model in CASE_NAMES:
                document = result["cases"][propagation_model]
                relative = Path("cases", case.id, f"step-{step_index:02d}", propagation_model)
                child = directory / relative
                write_workbench(document, child)
                stations, routes = derive_metrics(document)
                station_rows.extend(_identified(stations, case.id, step_s, propagation_model))
                route_rows.extend(_identified(routes, case.id, step_s, propagation_model))
                runs.append(
                    {
                        "case_id": case.id,
                        "step_s": step_s,
                        "propagation_model": propagation_model,
                        "bundle_path": relative.as_posix(),
                        "manifest_sha256": sha256(
                            (child / "manifest.json").read_bytes()
                        ).hexdigest(),
                        "scenario_sha256": document["provenance"]["scenario_sha256"],
                        "sample_count": len(document["timestamps_utc"]),
                        "duration_s": (
                            _utc(document["timestamps_utc"][-1], "timestamps_utc")
                            - _utc(document["timestamps_utc"][0], "timestamps_utc")
                        ).total_seconds(),
                    }
                )
            del result
    station_metrics = attach_deltas(station_rows, "best_link_integrated_bits", "station_index")
    route_metrics = attach_deltas(route_rows, "integrated_bottleneck_bits", "routing_model")
    summary = {
        "schema_version": "1",
        "kind": "openleo.fidelity-study",
        "name": study.name,
        "provenance": {
            "study_sha256": study.source_sha256,
            "software": {
                name: version(name) for name in ("openleo-link", "skyfield", "sgp4", "numpy")
            },
        },
        "sampling_steps_s": list(study.sampling_steps_s),
        "numerical_reference_step_s": study.sampling_steps_s[0],
        "runs": runs,
        "station_metrics": station_metrics,
        "route_metrics": route_metrics,
        "refinement_comparisons": comparisons,
        "limitations": [
            "The finest declared sampling step is a numerical reference, not truth or proof of convergence.",
            "Durations and integrated bits left-hold sampled states; events between samples can be missed.",
            "The ITU reference atmosphere is an idealized global profile, not measured local weather or uncertainty.",
            "Rates and routes omit scheduling, queues, packet loss and protocol overhead; they are not measured throughput.",
        ],
    }
    from openleo.fidelity_report import render_fidelity_report

    contents = {
        "fidelity-study.json": _json(summary) + "\n",
        "station-metrics.csv": metric_csv(station_metrics),
        "route-metrics.csv": metric_csv(route_metrics),
        "index.html": render_fidelity_report(summary),
    }
    for name, content in contents.items():
        (directory / name).write_text(content, encoding="utf-8", newline="\n")
    manifest = {
        "schema_version": "1",
        "kind": "openleo.fidelity-manifest",
        "files": {
            name: sha256(content.encode("utf-8")).hexdigest() for name, content in contents.items()
        },
    }
    (directory / "manifest.json").write_text(_json(manifest) + "\n", encoding="utf-8", newline="\n")
    return summary


def run_fidelity_study(study: FidelityStudy, output_dir: str | Path) -> dict:
    """Run a validated study and publish its complete bundle only on success."""
    _validate_loaded_study(study)
    directory = Path(output_dir)
    _check_output(directory)
    _preflight(study)
    directory.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(mkdtemp(prefix=f".{directory.name}-", dir=directory.parent))
    try:
        summary = _build_study(study, temporary)
        _check_output(directory)
        if directory.exists():
            directory.rmdir()
        temporary.replace(directory)
        return summary
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
