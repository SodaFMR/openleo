"""Controlled free-space/reference-atmosphere ablation and grid refinement."""

from __future__ import annotations

import csv
from dataclasses import replace
from hashlib import sha256
from math import isfinite
from pathlib import Path

from openleo.constellation import scenario_document, simulate_constellation
from openleo.network import add_network
from openleo.workbench import _json, _validate_document, write_workbench

CASE_NAMES = ("free_space", "reference", "refined")


def _case(scenario):
    canonical = _json(scenario_document(scenario))
    scenario = replace(scenario, source_sha256=sha256(canonical.encode("utf-8")).hexdigest())
    document = add_network(simulate_constellation(scenario))
    return {
        **document,
        "provenance": {
            **document["provenance"],
            "scenario_hash_encoding": "canonical JSON sorted compact UTF-8",
            "scenario_canonical_json": canonical,
        },
    }


def _comparison(reference, refined):
    if (
        reference["timestamps_utc"] != refined["timestamps_utc"]
        or reference["satellites"] != refined["satellites"]
        or reference["stations"] != refined["stations"]
    ):
        raise ValueError("refinement must preserve geometry and time samples")
    maxima = {
        "max_abs_gaseous_difference_db": 0.0,
        "max_abs_apparent_elevation_difference_deg": 0.0,
        "max_abs_excess_delay_difference_s": 0.0,
    }
    fields = (
        "gaseous_attenuation_db",
        "apparent_elevation_deg",
        "atmospheric_excess_delay_s",
    )
    count = disagreements = 0
    for first_frame, second_frame in zip(reference["links"], refined["links"], strict=True):
        for first, second in zip(first_frame, second_frame, strict=True):
            pair = (first["station_index"], first["satellite_index"])
            if pair != (second["station_index"], second["satellite_index"]):
                raise ValueError("refinement must preserve link sample ordering")
            for output, field in zip(maxima, fields, strict=True):
                difference = abs(first[field] - second[field])
                if not isfinite(difference):
                    raise ValueError("non-finite refinement difference")
                maxima[output] = max(maxima[output], difference)
            count += 1
            disagreements += first["modcod"] != second["modcod"]
    return {
        **maxima,
        "link_count": count,
        "modcod_disagreements": disagreements,
        "interpretation": "Deterministic grid differences; not measurement or probabilistic uncertainty.",
    }


def run_propagation_study(scenario) -> dict:
    """Run one baseline and two layer grids with all other inputs fixed."""
    if scenario.propagation is None:
        raise ValueError("a declared reference propagation model is required")
    if scenario.propagation.refinement not in (1, 2):
        raise ValueError("study refinement must start at 1 or 2 so a finer grid is available")
    configurations = (
        replace(scenario, propagation=None),
        scenario,
        replace(
            scenario,
            propagation=replace(
                scenario.propagation, refinement=scenario.propagation.refinement * 2
            ),
        ),
    )
    cases = {
        name: _case(configuration)
        for name, configuration in zip(CASE_NAMES, configurations, strict=True)
    }
    return {
        "schema_version": "1",
        "kind": "openleo.propagation-study",
        "input_configuration_sha256": scenario.source_sha256,
        "cases": cases,
        "comparison": _comparison(cases["reference"], cases["refined"]),
    }


def write_propagation_study(result: dict, output_dir: str | Path) -> Path:
    """Export complete case bundles plus a compact study summary and table."""
    if result.get("kind") != "openleo.propagation-study" or set(result["cases"]) != set(CASE_NAMES):
        raise ValueError("invalid propagation study")
    for name in CASE_NAMES:
        _validate_document(result["cases"][name])
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    for name in CASE_NAMES:
        write_workbench(result["cases"][name], directory / name)
    summary = {
        **{key: value for key, value in result.items() if key != "cases"},
        "cases": {
            name: {
                "scenario": case["scenario"],
                "provenance": case["provenance"],
                "statistics": case["statistics"],
                "network_summary": case["network"]["summary"],
                "manifest_sha256": sha256(
                    (directory / name / "manifest.json").read_bytes()
                ).hexdigest(),
            }
            for name, case in result["cases"].items()
        },
    }
    target = directory / "propagation-study.json"
    target.write_text(_json(summary) + "\n", encoding="utf-8", newline="\n")
    with (directory / "comparison.csv").open("w", encoding="utf-8", newline="") as file:
        writer = csv.writer(file, lineterminator="\n")
        writer.writerow(
            (
                "case",
                "station_index",
                "visible_sample_fraction",
                "usable_sample_fraction",
                "best_link_integrated_bits",
                "fixed_baseline_integrated_bits",
            )
        )
        for name in CASE_NAMES:
            for station in result["cases"][name]["statistics"]:
                writer.writerow(
                    (
                        name,
                        station["station_index"],
                        station["visible_sample_fraction"],
                        station["usable_sample_fraction"],
                        station["best_link_integrated_bits"],
                        station["fixed_baseline_integrated_bits"],
                    )
                )
    return target


def render_propagation_study(result: dict, output_path: str | Path) -> Path:
    """Produce a standalone research figure from the already computed study."""
    output = Path(output_path)
    if output.suffix.lower() not in (".svg", ".png"):
        raise ValueError("figure output must end in .svg or .png")
    try:
        import matplotlib
    except ModuleNotFoundError as exc:
        if exc.name is None or (
            exc.name != "matplotlib" and not exc.name.startswith("matplotlib.")
        ):
            raise
        raise ValueError("plotting requires Matplotlib; install openleo-link[plot]") from exc

    matplotlib.use("Agg")
    from matplotlib import pyplot as plt

    figure, axes = plt.subplots(1, 2, figsize=(12, 4.5), layout="constrained")
    try:
        names = [station["name"] for station in result["cases"]["free_space"]["statistics"]]
        colors = ("#0072BD", "#D95319", "#77AC30")
        for i, (name, color) in enumerate(zip(CASE_NAMES, colors, strict=True)):
            values = [
                station["best_link_integrated_bits"] / 1e9
                for station in result["cases"][name]["statistics"]
            ]
            axes[0].bar(
                [j + (i - 1) * 0.25 for j in range(len(names))],
                values,
                width=0.25,
                label=name.replace("_", " "),
                color=color,
            )
        axes[0].set_xticks(range(len(names)), names)
        axes[0].set_ylabel("Best-link integrated reference bits (Gbit)")
        axes[0].set_title("Propagation ablation; declared RF assumptions")
        axes[0].legend(fontsize=8)
        reference = result["cases"]["reference"]["links"]
        refined = result["cases"]["refined"]["links"]
        x, y = [], []
        for first_frame, second_frame in zip(reference, refined, strict=True):
            for first, second in zip(first_frame, second_frame, strict=True):
                x.append(first["elevation_deg"])
                y.append(second["gaseous_attenuation_db"] - first["gaseous_attenuation_db"])
        axes[1].scatter(x, y, s=5, color=colors[0])
        axes[1].set_xlabel("Geometric elevation (deg)")
        axes[1].set_ylabel("Refined minus reference gaseous loss (dB)")
        axes[1].set_title("Layer-grid refinement difference")
        for axis in axes:
            axis.grid(alpha=0.25, zorder=0)
        figure.suptitle("P.835-7 reference atmosphere / P.676-13 propagation", fontsize=12)
        output.parent.mkdir(parents=True, exist_ok=True)
        metadata = {
            "Creator": "OpenLEO",
            "Date": None,
            "Description": "Model-derived reference-profile ablation; no measured weather or uncertainty interval.",
        }
        with matplotlib.rc_context(
            {"svg.fonttype": "none", "svg.hashsalt": "openleo-propagation-study"}
        ):
            figure.savefig(
                output, dpi=200 if output.suffix.lower() == ".png" else None, metadata=metadata
            )
    finally:
        plt.close(figure)
    return output
