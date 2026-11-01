"""Static Markdown and CSV reports of a results directory or a sweep (design spec section 8).

``write_report(path, output_dir, figures=False)`` reads a results v2 directory (``results.json``)
or a sweep directory (``sweep.json``), after verifying its manifest, and writes ``report.md``
and ``summary.csv`` (plus PNG and SVG figures with ``figures=True``, which needs matplotlib).

The report only restates what the directory records: every number is read from its tables or
metadata, the limitations and warnings are copied verbatim, the Shannon-Hartley bound is labelled
a theoretical upper bound wherever it appears, the convergence gate is summarised per metric
class (route metrics are reported, not gated, in S1), and the provenance hashes are listed.

``summary.csv`` of a results directory has the columns ``scope,name,metric,value,unit``: the run
summary (scope ``run``), every station metric (``station``, name = station), every route metric
(``route``, name = ``source->target/model``) and the convergence counts (``convergence``). For a
sweep it holds the station rows of ``comparisons.csv`` (same columns).
"""

from __future__ import annotations

import csv
import io
import json
import math
import os
import shutil
import tempfile
from collections.abc import Callable, Sequence
from hashlib import sha256
from pathlib import Path
from typing import Any

from openleo.results import load_results, manifest_digest, verify_manifest

SUMMARY_COLUMNS = ("scope", "name", "metric", "value", "unit")
SWEEP_SUMMARY_COLUMNS = ("run_id", "metric", "value", "baseline_value", "delta", "ratio")
UPPER_BOUND = "theoretical upper bound, not throughput or achieved goodput"
NOT_GATED = "reported, not gated"
_RUN_SUMMARY_UNITS = {
    "satellite_count": "1",
    "station_count": "1",
    "grid_sample_count": "1",
    "step_s": "s",
    "window_s": "s",
    "pass_count": "1",
    "visible_pair_duration_s": "s",
    "integrated_rate_bits": "bit",
    "integrated_shannon_upper_bound_bits": "bit",
}


def _number(value: Any) -> str:
    """A number as report text: empty for NaN or missing, an integer when it is one."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    value = float(value)
    if math.isnan(value):
        return ""
    if math.isfinite(value) and value == int(value) and abs(value) < 1e15:
        return str(int(value))
    return repr(value)


def _unit(metric: str, unit: str) -> str:
    return f"{unit} (upper bound)" if "shannon" in metric else unit


def _cell(text: Any) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def _table(header: Sequence[str], rows: Sequence[Sequence[Any]]) -> list[str]:
    lines = ["| " + " | ".join(_cell(h) for h in header) + " |"]
    lines.append("|" + "|".join(" --- " for _ in header) + "|")
    lines += ["| " + " | ".join(_cell(value) for value in row) + " |" for row in rows]
    return lines


def _bullets(lines: Sequence[str], empty: str = "None recorded.") -> list[str]:
    return [f"- {line}" for line in lines] if lines else [empty]


def _csv_bytes(header: Sequence[str], rows: Sequence[Sequence[Any]]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")


# --- results ---------------------------------------------------------------------------------------


def _table_rows(table, keys: Sequence[str]) -> tuple[list[str], list[list[Any]]]:
    metrics = [c for c in table.columns if c.name not in keys]
    rows = []
    for k in range(table.rows):
        rows.append([table[c.name][k] for c in metrics])
    return [c.name for c in metrics], rows


def _convergence_lines(convergence: dict) -> tuple[list[str], list[list[str]]]:
    settings = convergence.get("settings", {})
    mode = settings.get("mode", "off")
    lines = [f"Mode: `{mode}`."]
    summary_rows: list[list[str]] = []
    if convergence.get("skipped"):
        lines.append("The grid-convergence gate was not run (mode off); no metric was compared.")
        return lines, summary_rows
    summary = convergence["summary"]
    lines.append(
        f"{summary['metrics']} metrics compared under {settings.get('refinement_factor')}x grid "
        f"refinement (refined step {_number(summary['refined_step_s'])} s, relative tolerance "
        f"{_number(settings.get('relative_tolerance'))}): {summary['passed']} passed, "
        f"{summary['failed']} failed, {summary['fatal']} of them gated. Largest relative "
        f"difference: {_number(summary['max_relative_difference']) or 'unbounded'}."
    )
    lines.append("")
    class_rows = [
        [
            name,
            values["metrics"],
            values["failed"],
            "gated" if values["gated"] else NOT_GATED,
        ]
        for name, values in summary["classes"].items()
    ]
    lines += _table(("Class", "Metrics", "Failed", "Status"), class_rows)
    failures = convergence.get("failures", [])
    if failures:
        lines += ["", "Failures:", ""]
        lines += _table(
            ("Metric", "Class", "Primary", "Refined", "Relative difference", "Gated"),
            [
                [
                    f.get("metric"),
                    f.get("class"),
                    _number(f.get("primary")),
                    _number(f.get("refined")),
                    _number(f.get("relative_difference")),
                    "yes" if f.get("fatal") else NOT_GATED,
                ]
                for f in failures
            ],
        )
    for key in ("metrics", "passed", "failed", "fatal"):
        summary_rows.append(["convergence", "", key, _number(summary[key]), "1"])
    return lines, summary_rows


def _results_report(directory: Path) -> tuple[str, bytes, dict]:
    results = load_results(directory)
    metadata = results.metadata
    scenario = metadata["scenario"]
    summary = metadata["summary"]
    time = scenario["time"]
    events = metadata["events"]
    tables = results.tables
    lines = [
        f"# OpenLEO run report: {scenario['name']}",
        "",
        (
            f"Results directory `{directory.name}` (results schema "
            f"{metadata['schema_version']}). Every value below is read from the results; "
            "nothing is recomputed."
        ),
        "",
        "## Run",
        "",
    ]
    software = ", ".join(f"{k} {v}" for k, v in sorted(metadata["software"].items()))
    lines += _table(
        ("Item", "Value"),
        [
            ("Window", f"{time['start_utc']} to {time['stop_utc']} ({summary['window_s']} s)"),
            ("Grid", f"step {time['step_s']} s, {summary['grid_sample_count']} samples"),
            ("Satellites, stations", f"{summary['satellite_count']}, {summary['station_count']}"),
            (
                "Events",
                f"enabled, {events['integration']}, tolerance {events['tolerance_s']} s"
                if events["enabled"]
                else f"disabled, {events['integration']} (v1 sampled semantics)",
            ),
            ("Propagation", scenario["propagation"]["model"]),
            ("Software", software),
        ],
    )
    lines += ["", "## Headline metrics", ""]
    lines += _table(
        ("Metric", "Value", "Unit"),
        [
            ("Passes", _number(summary["pass_count"]), "1"),
            ("Visible pair time", _number(summary["visible_pair_duration_s"]), "s"),
            ("Integrated adaptive (MODCOD) rate", _number(summary["integrated_rate_bits"]), "bit"),
            (
                f"Integrated Shannon-Hartley bound ({UPPER_BOUND})",
                _number(summary["integrated_shannon_upper_bound_bits"]),
                "bit (upper bound)",
            ),
        ],
    )
    rows: list[list[Any]] = [
        ["run", "", key, _number(summary[key]), _unit(key, unit)]
        for key, unit in _RUN_SUMMARY_UNITS.items()
        if key in summary
    ]
    station = tables["station_metrics"]
    names, values = _table_rows(station, ("station_index", "name"))
    units = {c.name: c.unit for c in station.columns}
    lines += ["", "## Stations", ""]
    lines += _table(
        ("Station", *names),
        [[n, *map(_number, v)] for n, v in zip(station["name"], values, strict=True)],
    )
    for name, row in zip(station["name"], values, strict=True):
        rows += [
            ["station", name, metric, _number(value), _unit(metric, units[metric])]
            for metric, value in zip(names, row, strict=True)
        ]
    if "route_metrics" in tables:
        routes = tables["route_metrics"]
        keys = ("pair_index", "model_index", "source", "target", "model")
        names, values = _table_rows(routes, keys)
        units = {c.name: c.unit for c in routes.columns}
        labels = [
            f"{s}->{t}/{m}"
            for s, t, m in zip(routes["source"], routes["target"], routes["model"], strict=True)
        ]
        lines += ["", "## Routes", ""]
        lines += _table(
            ("Pair/model", *names),
            [[label, *map(_number, v)] for label, v in zip(labels, values, strict=True)],
        )
        lines += [
            "",
            (
                "Route choices are snapshots held between route breakpoints; their metrics are "
                f"{NOT_GATED} by the convergence gate in this release."
            ),
        ]
        for label, row in zip(labels, values, strict=True):
            rows += [
                ["route", label, metric, _number(value), units[metric]]
                for metric, value in zip(names, row, strict=True)
            ]
    convergence_lines, convergence_rows = _convergence_lines(metadata["convergence"])
    lines += ["", "## Convergence", "", *convergence_lines]
    rows += convergence_rows
    lines += ["", "## Warnings", "", *_bullets(metadata["warnings"])]
    lines += ["", "## Limitations", "", *_bullets(metadata["limitations"])]
    provenance = metadata["provenance"]
    lines += ["", "## Provenance", ""]
    lines += _table(
        ("Item", "SHA-256"),
        [
            ("manifest.json (run integrity anchor)", manifest_digest(directory)),
            ("results.json", sha256((directory / "results.json").read_bytes()).hexdigest()),
            ("scenario source file", scenario["source_sha256"]),
            ("normalised scenario", scenario["normalised_sha256"]),
            ("leap-second table", provenance["leap_second_table_sha256"]),
            *[
                (f"catalog {c['path']} (archived as {c['archived_path']})", c["sha256"])
                for c in provenance["catalogs"]
            ],
        ],
    )
    context = {"kind": "results", "results": results}
    return "\n".join(lines) + "\n", _csv_bytes(SUMMARY_COLUMNS, rows), context


# --- sweeps ----------------------------------------------------------------------------------------


def _comparisons(directory: Path) -> list[dict[str, str]]:
    with (directory / "comparisons.csv").open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _sweep_report(directory: Path) -> tuple[str, bytes, dict]:
    verify_manifest(directory)
    document = json.loads((directory / "sweep.json").read_text(encoding="utf-8"))
    comparisons = _comparisons(directory)
    stations = [row for row in comparisons if row["metric"].startswith("station/")]
    lines = [
        f"# OpenLEO sweep report: {document['name']}",
        "",
        (
            f"Sweep directory `{directory.name}`, mode `{document['mode']}`, "
            f"{len(document['runs'])} runs. Baseline: `{document['baseline_run_id']}` "
            f"({document['baseline_rule']})."
        ),
        "",
        f"Every comparison is a {document['comparison_label']}.",
        "",
        "## Runs",
        "",
    ]
    lines += _table(
        ("Run", "Label", "Scenario SHA-256", "Metrics", "Replay evidence"),
        [
            (
                f"`{entry['id']}`",
                entry["label"],
                entry["scenario_sha256"],
                entry["metric_count"],
                (entry["replay"] or {}).get("evidence", "none requested"),
            )
            for entry in document["runs"]
        ],
    )
    lines += ["", "## Station metrics against the baseline", ""]
    lines += _table(
        ("Run", "Metric", "Value", "Baseline", "Delta", "Ratio"),
        [[row[c] for c in SWEEP_SUMMARY_COLUMNS] for row in stations],
    )
    replay = document.get("replay")
    lines += ["", "## Packet replays", ""]
    if replay is None:
        lines.append("No packet replay was requested.")
    else:
        lines.append(
            f"Evidence: `{replay['evidence']}`; kinds without a backend: "
            f"{', '.join(replay['missing']) or 'none'}."
        )
        replays = [
            (entry["id"], packet)
            for entry in document["runs"]
            for packet in ((entry["replay"] or {}).get("replays") or [])
        ]
        if replays:
            lines += [""]
            lines += _table(
                (
                    "Run",
                    "Kind",
                    "Model",
                    "Offered load (bit/s)",
                    "Offered",
                    "Received in window",
                    "Goodput (bit/s)",
                ),
                [
                    (
                        run_id,
                        packet.get("kind"),
                        packet.get("routing_model") or "",
                        _number(packet.get("offered_load_bps")),
                        _number(packet["summary"]["aggregate"]["offered_packets"]),
                        _number(packet["summary"]["aggregate"]["received_in_window_packets"]),
                        _number(packet["summary"]["aggregate"]["goodput_bps"]),
                    )
                    for run_id, packet in replays
                    if isinstance(packet.get("summary", {}).get("aggregate"), dict)
                ],
            )
    run_notes: dict[str, list[str]] = {}
    for entry in document["runs"]:
        results = json.loads(
            (directory / entry["directory"] / "results.json").read_text(encoding="utf-8")
        )
        for line in results.get("warnings", []) + results.get("limitations", []):
            run_notes.setdefault(line, []).append(entry["id"])
    lines += ["", "## Limitations", "", *_bullets(document["limitations"])]
    lines += ["", "## Run warnings and limitations", ""]
    lines += _bullets([f"{line} (runs: {', '.join(ids)})" for line, ids in run_notes.items()])
    lines += ["", "## Provenance", ""]
    lines += _table(
        ("Item", "SHA-256"),
        [
            ("manifest.json (sweep integrity anchor)", manifest_digest(directory)),
            ("sweep spec", document["spec_sha256"]),
            ("base scenario source file", document["base"]["source_sha256"]),
            ("normalised base scenario", document["base"]["normalised_sha256"]),
            *[
                (
                    f"run `{entry['id']}` manifest.json",
                    manifest_digest(directory / entry["directory"]),
                )
                for entry in document["runs"]
            ],
        ],
    )
    summary = _csv_bytes(
        SWEEP_SUMMARY_COLUMNS, [[row[c] for c in SWEEP_SUMMARY_COLUMNS] for row in stations]
    )
    context = {"kind": "sweep", "document": document, "stations": stations}
    return "\n".join(lines) + "\n", summary, context


# --- entry point -----------------------------------------------------------------------------------


def write_report(path: Path, output_dir: Path, *, figures: bool = False) -> Path:
    """Write ``report.md`` and ``summary.csv`` (and figures) for a results or sweep directory.

    ``output_dir`` must not exist or be an empty directory. Everything is built in a temporary
    sibling directory first and renamed into place only when complete, so a failure leaves
    ``output_dir`` exactly as it was. Returns ``output_dir``.
    """
    path, output_dir = Path(path), Path(output_dir)
    if (path / "results.json").is_file():
        build: Callable[[Path], tuple[str, bytes, dict]] = _results_report
    elif (path / "sweep.json").is_file():
        build = _sweep_report
    else:
        raise ValueError(f"{path} is neither a results or sweep directory")
    _check_output(output_dir)
    markdown, summary, context = build(path)
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}.report-", dir=output_dir.parent))
    try:
        text = markdown
        if figures:
            from openleo.report.figures import render_figures

            names = render_figures(context, stage)
            text += "\n## Figures\n\n" + "".join(
                f"![{name}]({name})\n\n" for name in names if name.endswith(".png")
            )
        (stage / "report.md").write_bytes(text.encode("utf-8"))
        (stage / "summary.csv").write_bytes(summary)
        _check_output(output_dir)
        stage.chmod(_default_directory_mode())  # mkdtemp made it 0700; publish it as mkdir would
        try:
            # Replaces an empty directory atomically on POSIX.
            os.replace(stage, output_dir)
        except OSError:
            if not output_dir.is_dir() or any(output_dir.iterdir()):
                raise
            output_dir.rmdir()  # platforms that cannot rename onto an empty directory
            os.rename(stage, output_dir)
    except OSError as exc:
        raise ValueError(f"could not publish the report at {output_dir}: {exc}") from exc
    finally:
        if stage.exists():
            shutil.rmtree(stage, ignore_errors=True)
    return output_dir


def _default_directory_mode() -> int:
    """0o777 & ~umask: the mode ``mkdir`` gives a new directory (umask is read, then restored).

    os.umask can only be read by setting it, so for an instant the process umask is 0; a thread
    that creates a file in that instant gets world-writable permissions. Reports are written from
    the CLI and from the app's single worker thread, so the window is accepted.
    """
    umask = os.umask(0)
    os.umask(umask)
    return 0o777 & ~umask


def _check_output(output_dir: Path) -> None:
    if output_dir.is_symlink() or (output_dir.exists() and not output_dir.is_dir()):
        raise ValueError(f"report output {output_dir} must be a directory")
    if output_dir.exists() and any(output_dir.iterdir()):
        raise ValueError(f"refusing to overwrite a non-empty output directory {output_dir}")


__all__ = ["write_report"]
