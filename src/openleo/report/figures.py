"""Optional report figures (matplotlib, the ``plot`` extra), as PNG and SVG.

Each figure plots values the report already lists, with their units; nothing is interpolated.
A results directory gets the per-station visible and RF-usable fractions; a sweep gets each
station's best-link integrated bits per run.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

_HINT = "report figures need matplotlib: install it with 'openleo-link[plot]'"


def _pyplot():
    try:
        import matplotlib
    except ImportError as exc:
        raise ValueError(_HINT) from exc
    matplotlib.use("Agg")
    matplotlib.rcParams["svg.hashsalt"] = "openleo-report"
    from matplotlib import pyplot

    return pyplot


def _bars(pyplot, groups, series, ylabel: str, title: str, stage: Path, stem: str) -> list[str]:
    """Grouped bars: one group per label in ``groups``, one bar per (name, values) series."""
    figure, axes = pyplot.subplots(figsize=(7.5, 4.0), layout="constrained")
    positions = np.arange(len(groups))
    width = 0.8 / max(1, len(series))
    for index, (name, values) in enumerate(series):
        axes.bar(positions + (index - (len(series) - 1) / 2) * width, values, width, label=name)
    axes.set_xticks(positions, groups, rotation=20 if len(groups) > 4 else 0, ha="right")
    axes.set_ylabel(ylabel)
    axes.set_title(title)
    axes.spines[["top", "right"]].set_visible(False)
    if len(series) > 1:
        axes.legend(frameon=False)
    names = [f"{stem}.png", f"{stem}.svg"]
    figure.savefig(stage / names[0], dpi=150, metadata={"Software": None})
    figure.savefig(stage / names[1], metadata={"Date": None, "Creator": None})
    pyplot.close(figure)
    return names


def render_figures(context: dict, stage: Path) -> list[str]:
    """Write the figures of a report context into ``stage``; return their file names."""
    pyplot = _pyplot()
    if context["kind"] == "results":
        table = context["results"].tables["station_metrics"]
        return _bars(
            pyplot,
            list(table["name"]),
            [
                ("visible", table["visible_fraction"].tolist()),
                ("RF usable (MODCOD lock)", table["rf_usable_fraction"].tolist()),
            ],
            "fraction of the window",
            "Station visibility and RF-usable time",
            stage,
            "station-fractions",
        )
    rows = [r for r in context["stations"] if r["metric"].endswith("/best_link_integrated_bits")]
    runs = list(dict.fromkeys(r["run_id"] for r in rows))
    stations = list(dict.fromkeys(r["metric"].split("/")[1] for r in rows))
    values = {(r["run_id"], r["metric"].split("/")[1]): float(r["value"] or "nan") for r in rows}
    return _bars(
        pyplot,
        runs,
        [(s, [values.get((run, s), np.nan) for run in runs]) for s in stations],
        "best-link integrated bits (bit)",
        "Best-link integrated bits per run",
        stage,
        "best-link-bits",
    )
