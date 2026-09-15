"""Pure standalone HTML rendering for verified constellation fidelity summaries."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from html import escape
from math import isfinite
from pathlib import PurePosixPath

_MODELS = ("free_space", "reference", "refined")
_COLORS = {"free_space": "#2563a6", "reference": "#d97706", "refined": "#16836b"}
_SAFE_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_STATION_COLUMNS = (
    "step_s",
    "propagation_model",
    "station_index",
    "name",
    "visible_sample_fraction",
    "usable_sample_fraction",
    "best_link_integrated_bits",
    "fixed_baseline_integrated_bits",
    "handover_count",
    "visible_duration_s",
    "usable_duration_s",
    "rf_outage_duration_s",
    "out_of_view_duration_s",
    "visible_time_fraction",
    "usable_time_fraction",
    "mean_best_rate_bps",
    "delta_bits_to_finest",
    "relative_delta_percent_to_finest",
    "delta_bits_to_free_space",
)
_ROUTE_COLUMNS = (
    "step_s",
    "propagation_model",
    "routing_model",
    "connected_sample_fraction",
    "integrated_bottleneck_bits",
    "route_changes",
    "connected_duration_s",
    "disconnected_duration_s",
    "connected_time_fraction",
    "mean_bottleneck_bps",
    "delta_bits_to_finest",
    "relative_delta_percent_to_finest",
    "delta_bits_to_free_space",
)
_RUN_COLUMNS = (
    "case_id",
    "step_s",
    "propagation_model",
    "sample_count",
    "duration_s",
    "scenario_sha256",
    "manifest_sha256",
)
_LABELS = {
    "case_id": "Case",
    "step_s": "Sampling interval (s)",
    "propagation_model": "Propagation",
    "station_index": "Station index",
    "name": "Station",
    "visible_sample_fraction": "Visible sample fraction",
    "usable_sample_fraction": "RF-usable sample fraction",
    "best_link_integrated_bits": "Best-link integrated reference bits (bit)",
    "fixed_baseline_integrated_bits": "Fixed-baseline integrated reference bits (bit)",
    "handover_count": "Handovers",
    "visible_duration_s": "Visible duration (s)",
    "usable_duration_s": "RF-usable duration (s)",
    "rf_outage_duration_s": "Visible RF-outage duration (s)",
    "out_of_view_duration_s": "Out-of-view duration (s)",
    "visible_time_fraction": "Visible time fraction",
    "usable_time_fraction": "RF-usable time fraction",
    "mean_best_rate_bps": "Whole-window mean best-link reference rate (bit/s)",
    "routing_model": "Routing model",
    "connected_sample_fraction": "Connected sample fraction",
    "integrated_bottleneck_bits": "Integrated bottleneck reference bits (bit)",
    "route_changes": "Route changes",
    "connected_duration_s": "Connected duration (s)",
    "disconnected_duration_s": "Disconnected duration (s)",
    "connected_time_fraction": "Connected time fraction",
    "mean_bottleneck_bps": "Whole-window mean bottleneck rate (bit/s)",
    "delta_bits_to_finest": "Difference from finest sampling (bit)",
    "relative_delta_percent_to_finest": "Difference from finest sampling (%)",
    "delta_bits_to_free_space": "Difference from free space (bit)",
    "sample_count": "Samples",
    "duration_s": "Window duration (s)",
    "scenario_sha256": "Scenario SHA-256",
    "manifest_sha256": "Child manifest SHA-256",
    "max_abs_gaseous_difference_db": "Maximum absolute gaseous attenuation difference (dB)",
    "max_abs_apparent_elevation_difference_deg": (
        "Maximum absolute apparent elevation difference (deg)"
    ),
    "max_abs_excess_delay_difference_s": (
        "Maximum absolute atmospheric excess delay difference (s)"
    ),
    "link_count": "Compared link samples",
    "modcod_disagreements": "MODCOD disagreements",
    "interpretation": "Interpretation",
}
_CSS = """
:root{color-scheme:light;font-family:Inter,ui-sans-serif,system-ui,sans-serif;color:#172033;background:#eef2f5}
*{box-sizing:border-box}body{margin:0}header,main,footer{max-width:1440px;margin:auto}header{padding:1.5rem 2rem .75rem}
h1{margin:.2rem 0;font-size:clamp(1.5rem,3vw,2rem)}h2{margin-top:0}h3{margin:.25rem 0 1rem}
p{line-height:1.55}.eyebrow,.muted{color:#5d6a7a}.eyebrow{text-transform:uppercase;letter-spacing:.12em;font-size:.75rem;font-weight:700}
.jump{display:flex;gap:1rem;flex-wrap:wrap;font-size:.85rem;margin-top:.5rem}
main{padding:0 2rem 2rem}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:.75rem}
.card,.panel{background:#fff;border:1px solid #d6dde5;border-radius:10px;box-shadow:0 2px 8px #1720330a}
.card{padding:.8rem}.card strong{display:block;font-size:1.15rem;margin-top:.25rem}.panel{padding:1.25rem;margin:1rem 0}
.station{border-top:1px solid #d6dde5;padding-top:1rem;margin-top:1.25rem}.charts{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,430px),1fr));gap:1rem}
figure{margin:0;border:1px solid #e2e7ec;border-radius:8px;padding:.75rem}figcaption{font-weight:650;margin-bottom:.25rem}
svg{display:block;width:100%;height:auto}.zero{margin:.4rem 0 0;color:#5d6a7a;font-size:.85rem}.legend{display:flex;gap:1rem;flex-wrap:wrap;list-style:none;padding:0;margin:.5rem 0 0;font-size:.8rem}
.legend i{display:inline-block;width:.75rem;height:.75rem;border-radius:2px;margin-right:.3rem}table{border-collapse:collapse;width:100%;font-size:.82rem}
caption{text-align:left;font-weight:650;padding:0 0 .6rem}.table-wrap{overflow:auto}th,td{text-align:left;padding:.55rem .65rem;border-bottom:1px solid #e1e6eb;vertical-align:top;white-space:nowrap}
th{background:#f4f7f9;color:#3c4858}code{font-size:.76rem}a{color:#125b96;text-underline-offset:2px}ul{line-height:1.55}
.hash{overflow-wrap:anywhere;white-space:normal}.notice{border-left:4px solid #d97706;padding:.7rem 1rem;background:#fff8e8}.sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}
footer{padding:0 2rem 2rem;color:#5d6a7a;font-size:.85rem}@media(max-width:650px){header,main,footer{padding-left:1rem;padding-right:1rem}.panel{padding:.8rem}}
""".strip()


def render_fidelity_report(summary: Mapping) -> str:
    """Return deterministic standalone HTML for an already verified study summary."""
    if summary.get("schema_version") != "1" or summary.get("kind") != "openleo.fidelity-study":
        raise ValueError("invalid fidelity study summary")
    runs = _mappings(summary.get("runs"), "runs")
    stations = _mappings(summary.get("station_metrics"), "station_metrics")
    routes = _mappings(summary.get("route_metrics"), "route_metrics")
    comparisons = _mappings(summary.get("refinement_comparisons"), "refinement_comparisons")
    links = tuple(_child_link(run.get("bundle_path")) for run in runs)
    for row in stations:
        _finite(row.get("best_link_integrated_bits"), "best_link_integrated_bits")
        _finite(row.get("delta_bits_to_finest"), "delta_bits_to_finest")

    steps = tuple(_finite(value, "sampling_steps_s") for value in summary["sampling_steps_s"])
    reference_step = _finite(summary["numerical_reference_step_s"], "numerical_reference_step_s")
    cases = _unique(run.get("case_id") for run in runs)
    provenance = summary.get("provenance")
    if not isinstance(provenance, Mapping):
        raise TypeError("invalid fidelity study provenance")

    sections = []
    for case in cases:
        case_stations = tuple(row for row in stations if row.get("case_id") == case)
        case_routes = tuple(row for row in routes if row.get("case_id") == case)
        station_sections = []
        identities = _unique((row.get("station_index"), row.get("name")) for row in case_stations)
        for station_index, station_name in identities:
            rows = tuple(
                row
                for row in case_stations
                if (row.get("station_index"), row.get("name")) == (station_index, station_name)
            )
            station_sections.append(
                '<section class="station"><h3>'
                + _e(station_name)
                + ' <span class="muted">· station '
                + _e(station_index)
                + '</span></h3><div class="charts">'
                + _bar_chart(
                    rows,
                    "best_link_integrated_bits",
                    "Best-link integrated reference bits",
                    "Model-derived integrated reference bits; not measured throughput.",
                    "bit",
                )
                + _bar_chart(
                    rows,
                    "delta_bits_to_finest",
                    f"Sampling effect relative to the {_number(reference_step)} s numerical reference",
                    "Signed deterministic difference; the finest grid is not truth or proof of convergence.",
                    "bit",
                )
                + "</div>"
                + _table(rows, _STATION_COLUMNS, "Station metrics")
                + "</section>"
            )
        disconnected = bool(case_routes) and all(
            row.get("connected_time_fraction") == 0 for row in case_routes
        )
        route_note = (
            '<p class="notice">No connected route time was recorded.</p>' if disconnected else ""
        )
        case_comparisons = tuple(row for row in comparisons if row.get("case_id") == case)
        sections.append(
            '<section class="panel"><p class="eyebrow">Case</p><h2>'
            + _e(case)
            + "</h2>"
            + "".join(station_sections)
            + route_note
            + _table(case_routes, _ROUTE_COLUMNS, "Routing metrics")
            + _table(
                case_comparisons,
                _columns(case_comparisons, ("case_id", "step_s")),
                "Layer-grid refinement comparisons",
            )
            + "</section>"
        )

    software = provenance.get("software")
    software_html = (
        " · ".join(f"{_e(key)} {_e(value)}" for key, value in sorted(software.items()))
        if isinstance(software, Mapping)
        else _e(software)
    )
    grid = ", ".join(f"{_number(step)} s" for step in steps)
    run_rows = "".join(
        "<tr>"
        + "".join(
            f'<td class="{"hash" if "sha256" in key else ""}">{_value(run.get(key), key)}</td>'
            for key in _RUN_COLUMNS
        )
        + f'<td><a href="{_e(link, quote=True)}">Open 3D child report</a></td></tr>'
        for run, link in zip(runs, links, strict=True)
    )
    limitations = summary.get("limitations")
    if not isinstance(limitations, (list, tuple)):
        raise TypeError("invalid fidelity study limitations")
    return (
        '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="referrer" content="no-referrer">'
        "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; script-src 'none'; style-src 'unsafe-inline'; img-src data:; base-uri 'none'; form-action 'none'; object-src 'none'\">"
        '<meta name="color-scheme" content="light"><title>'
        + _e(summary.get("name"))
        + " · OpenLEO fidelity study</title><style>"
        + _CSS
        + '</style></head><body><header><p class="eyebrow">OpenLEO scientific workbench</p><h1>'
        + _e(summary.get("name"))
        + '</h1><p class="muted">Deterministic constellation fidelity comparison. Reference bits and time fractions are model-derived quantities, not measured throughput or service availability.</p><nav class="jump" aria-label="Report details"><a href="#sources">Sources &amp; software</a><a href="#runs">Child runs</a></nav></header><main>'
        + '<section class="grid" aria-label="Study overview">'
        + f'<div class="card">Cases<strong>{len(cases)}</strong></div>'
        + f'<div class="card">Runs<strong>{len(runs)}</strong></div>'
        + f'<div class="card">Sampling grid<strong>{_e(grid)}</strong></div>'
        + f'<div class="card">Numerical reference<strong>{_number(reference_step)} s</strong></div></section>'
        + "".join(sections)
        + '<section class="panel" id="sources"><h2>Sources and software</h2><dl>'
        + f'<dt>Study SHA-256</dt><dd><code class="hash">{_e(provenance.get("study_sha256"))}</code></dd>'
        + f"<dt>Software</dt><dd>{software_html}</dd></dl></section>"
        + '<section class="panel" id="runs"><h2>Child runs</h2><p>Each link opens the archived 3D explorer for that exact case, sampling interval and propagation model.</p><div class="table-wrap"><table><thead><tr>'
        + "".join(f'<th scope="col">{_e(_label(key))}</th>' for key in _RUN_COLUMNS)
        + '<th scope="col">Drill-down</th></tr></thead><tbody>'
        + run_rows
        + "</tbody></table></div></section>"
        + '<section class="panel"><h2>Interpretation and limitations</h2><ul>'
        + "".join(f"<li>{_e(item)}</li>" for item in limitations)
        + '</ul><p class="notice">The smallest declared sampling interval is a numerical reference, not truth or proof of convergence. Fractions describe this declared deterministic window.</p></section></main>'
        + "<footer>Standalone local report · no remote assets or executable scripts.</footer></body></html>\n"
    )


def _bar_chart(
    rows: tuple[Mapping, ...], field: str, title: str, description: str, unit: str
) -> str:
    steps = sorted({_finite(row.get("step_s"), "step_s") for row in rows})
    models = tuple(
        model for model in _MODELS if any(row.get("propagation_model") == model for row in rows)
    )
    values = [_finite(row.get(field), field) for row in rows]
    bound = max((abs(value) for value in values), default=0.0)
    signed = field == "delta_bits_to_finest"
    width, height = 720.0, 270.0
    left, top, plot_width, plot_height = 70.0, 20.0, 625.0, 190.0
    zero_y = top + plot_height / 2 if signed and bound else top + plot_height
    scale = plot_height / (2 * bound) if signed and bound else plot_height / bound if bound else 0.0
    bars = []
    group_width = plot_width / max(len(steps), 1)
    bar_width = min(34.0, group_width / max(len(models) + 1, 1))
    for step_index, step in enumerate(steps):
        center = left + group_width * (step_index + 0.5)
        bars.append(
            f'<text x="{center:.3f}" y="235" text-anchor="middle" font-size="12" fill="#4d5968">{_number(step)} s</text>'
        )
        for model_index, model in enumerate(models):
            row = next(
                (
                    item
                    for item in rows
                    if item.get("step_s") == step and item.get("propagation_model") == model
                ),
                None,
            )
            if row is None:
                continue
            value = _finite(row.get(field), field)
            x = center + (model_index - (len(models) - 1) / 2) * bar_width - bar_width * 0.42
            bar_height = abs(value) * scale
            y = zero_y - bar_height if value >= 0 else zero_y
            bars.append(
                f'<rect x="{x:.3f}" y="{y:.3f}" width="{bar_width * 0.84:.3f}" height="{bar_height:.3f}" fill="{_COLORS[model]}"><title>{_e(model.replace("_", " "))}, {_number(step)} s: {_number(value)} {unit}</title></rect>'
            )
    tick_values = (
        (-bound, -bound / 2, 0.0, bound / 2, bound)
        if signed and bound
        else (0.0, bound / 2, bound)
        if bound
        else (0.0,)
    )
    ticks = "".join(
        f'<line x1="{left}" y1="{zero_y - value * scale:.3f}" x2="{left + plot_width}" y2="{zero_y - value * scale:.3f}" stroke="{("#718096" if value == 0 else "#d8dee6")}"/>'
        f'<text class="axis-value" x="{left - 8}" y="{zero_y - value * scale + 4:.3f}" text-anchor="end" font-size="11" fill="#5d6a7a">{_number(value)}</text>'
        for value in tick_values
    )
    legend = "".join(
        f'<li><i style="background:{_COLORS[model]}"></i>{_e(model.replace("_", " "))}</li>'
        for model in models
    )
    zero_note = '<p class="zero">All plotted values are zero.</p>' if not bound else ""
    return (
        f'<figure><figcaption>{_e(title)}</figcaption><p class="muted">{_e(description)}</p>'
        f'<svg viewBox="0 0 {width:g} {height:g}" role="img" aria-label="{_e(title, quote=True)} grouped bar chart"><title>{_e(title)}</title>'
        + ticks
        + "".join(bars)
        + f'<text class="axis-unit" x="{left:g}" y="12" font-size="11" fill="#5d6a7a">{_e(unit)}</text></svg>'
        + zero_note
        + f'<ul class="legend" aria-label="Propagation models">{legend}</ul></figure>'
    )


def _table(rows: tuple[Mapping, ...], columns: tuple[str, ...], caption: str) -> str:
    if not rows:
        return f'<p class="muted">No {_e(caption.lower())}.</p>'
    return (
        '<div class="table-wrap"><table><caption>'
        + _e(caption)
        + "</caption><thead><tr>"
        + "".join(f'<th scope="col">{_e(_label(column))}</th>' for column in columns)
        + "</tr></thead><tbody>"
        + "".join(
            "<tr>"
            + "".join(f"<td>{_value(row.get(column), column)}</td>" for column in columns)
            + "</tr>"
            for row in rows
        )
        + "</tbody></table></div>"
    )


def _columns(rows: tuple[Mapping, ...], first: tuple[str, ...]) -> tuple[str, ...]:
    return first + tuple(sorted({key for row in rows for key in row if key not in first}))


def _mappings(value, name: str) -> tuple[Mapping, ...]:
    if not isinstance(value, (list, tuple)) or any(not isinstance(row, Mapping) for row in value):
        raise ValueError(f"invalid fidelity study {name}")
    return tuple(value)


def _child_link(value) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError("child bundle_path must be a safe relative path")
    path = PurePosixPath(value)
    parts = value.split("/")
    if path.is_absolute() or any(not _SAFE_SEGMENT.fullmatch(part) for part in parts):
        raise ValueError("child bundle_path must be a safe relative path")
    return value + "/explorer.html"


def _finite(value, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value):
        raise ValueError(f"{name} plot value must be finite")
    return float(value)


def _value(value, key: str) -> str:
    if value is None:
        return "Not defined (zero reference bits)" if key.startswith("relative_delta") else "—"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        if not isfinite(value):
            raise ValueError(f"{key} value must be finite")
        return _number(value)
    return _e(value)


def _number(value: float) -> str:
    return format(value, ".12g")


def _label(key: str) -> str:
    return _LABELS.get(key, key.replace("_", " ").capitalize())


def _unique(values: Iterable) -> tuple:
    return tuple(dict.fromkeys(values))


def _e(value, *, quote: bool = True) -> str:
    return escape(str(value), quote=quote)
