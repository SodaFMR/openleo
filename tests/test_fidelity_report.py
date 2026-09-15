import re
from copy import deepcopy
from math import inf, nan

import pytest

from openleo.fidelity_report import render_fidelity_report


def _summary() -> dict:
    models = ("free_space", "reference", "refined")
    steps = (10.0, 30.0)
    runs = [
        {
            "case_id": "polar",
            "step_s": step,
            "propagation_model": model,
            "bundle_path": f"cases/polar/step-{step:g}/{model}",
            "manifest_sha256": f"{index:x}" * 64,
            "scenario_sha256": "b" * 64,
            "sample_count": 7 + index,
            "duration_s": 60.0,
        }
        for index, (step, model) in enumerate(
            ((step, model) for step in steps for model in models), start=1
        )
    ]
    station_metrics = []
    for index, run in enumerate(runs, start=1):
        station_metrics.append(
            {
                "case_id": run["case_id"],
                "step_s": run["step_s"],
                "propagation_model": run["propagation_model"],
                "station_index": 0,
                "name": "Tromsø",
                "visible_sample_fraction": 5 / 7,
                "usable_sample_fraction": 4 / 7,
                "best_link_integrated_bits": float(index * 100_000_000),
                "fixed_baseline_integrated_bits": 75_000_000.0,
                "handover_count": index,
                "visible_duration_s": 50.0,
                "usable_duration_s": 40.0,
                "rf_outage_duration_s": 10.0,
                "out_of_view_duration_s": 10.0,
                "visible_time_fraction": 5 / 6,
                "usable_time_fraction": 2 / 3,
                "mean_best_rate_bps": 10_000_000.0,
                "delta_bits_to_finest": 0.0 if run["step_s"] == 10.0 else -1_000_000.0,
                "relative_delta_percent_to_finest": (0.0 if run["step_s"] == 10.0 else -1.0),
                "delta_bits_to_free_space": float(index * 1_000),
            }
        )
    route_metrics = [
        {
            "case_id": "polar",
            "step_s": 10.0,
            "propagation_model": "reference",
            "routing_model": "maximum_rate",
            "connected_sample_fraction": 4 / 7,
            "integrated_bottleneck_bits": 321_000_000.0,
            "route_changes": 2,
            "connected_duration_s": 40.0,
            "disconnected_duration_s": 20.0,
            "connected_time_fraction": 2 / 3,
            "mean_bottleneck_bps": 8_025_000.0,
            "delta_bits_to_finest": 0.0,
            "relative_delta_percent_to_finest": 0.0,
            "delta_bits_to_free_space": -2_000_000.0,
        }
    ]
    return {
        "schema_version": "1",
        "kind": "openleo.fidelity-study",
        "name": "Polar fidelity matrix",
        "provenance": {
            "study_sha256": "a" * 64,
            "software": {"openleo-link": "0.5.0", "python": "3.12"},
        },
        "sampling_steps_s": list(steps),
        "numerical_reference_step_s": 10.0,
        "runs": runs,
        "station_metrics": station_metrics,
        "route_metrics": route_metrics,
        "refinement_comparisons": [
            {
                "case_id": "polar",
                "step_s": 10.0,
                "max_abs_gaseous_difference_db": 0.0125,
                "max_abs_apparent_elevation_difference_deg": 0.0025,
                "max_abs_excess_delay_difference_s": 0.000001,
                "link_count": 42,
                "modcod_disagreements": 1,
                "interpretation": "Deterministic grid differences; not uncertainty.",
            }
        ],
        "limitations": [
            "Finest sampling is a numerical reference, not truth or proof of convergence."
        ],
    }


def test_report_contains_comparisons_run_drilldowns_and_declared_scalars() -> None:
    summary = _summary()

    html = render_fidelity_report(summary)

    assert html.startswith("<!doctype html>")
    assert html.count("<svg") >= 2
    assert "Best-link integrated reference bits" in html
    assert "Sampling effect relative to the 10 s numerical reference" in html
    assert "not measured throughput or service availability" in html
    assert "a" * 64 in html
    assert "0.0125" in html
    assert "321000000" in html
    for run in summary["runs"]:
        assert f'href="{run["bundle_path"]}/explorer.html"' in html
        assert str(run["sample_count"]) in html


def test_report_places_results_before_metadata_and_resolves_jump_links() -> None:
    html = render_fidelity_report(_summary())

    results = html.index('<section class="panel"><p class="eyebrow">Case</p>')
    sources = html.index('<section class="panel" id="sources">')
    runs = html.index('<section class="panel" id="runs">')
    assert results < sources < runs
    assert '<a href="#sources">Sources &amp; software</a>' in html
    assert '<a href="#runs">Child runs</a>' in html
    assert html.count('id="sources"') == 1
    assert html.count('id="runs"') == 1


def test_report_escapes_untrusted_text_and_has_no_executable_content() -> None:
    summary = _summary()
    payload = '<script>alert("study")</script>'
    summary["name"] = payload
    summary["station_metrics"][0]["name"] = payload
    summary["limitations"].append(payload)

    html = render_fidelity_report(summary)

    assert payload not in html
    assert "&lt;script&gt;alert(&quot;study&quot;)&lt;/script&gt;" in html
    assert "<script" not in html.lower()
    assert "default-src &#x27;none&#x27;" not in html
    assert "default-src 'none';" in html
    assert "script-src 'none'" in html
    assert "frame-ancestors" not in html
    assert "http://" not in html
    assert "https://" not in html


def test_report_charts_show_quantitative_axes_and_units() -> None:
    html = render_fidelity_report(_summary())

    axis_values = re.findall(r'class="axis-value"[^>]*>([^<]+)</text>', html)
    assert {"0", "300000000", "600000000"} <= set(axis_values)
    assert {"-1000000", "-500000", "0", "500000", "1000000"} <= set(axis_values)
    unit_labels = re.findall(r'<text class="axis-unit"([^>]*)>bit</text>', html)
    assert len(unit_labels) == 2
    assert all('x="70"' in label and 'y="12"' in label for label in unit_labels)
    assert all("transform=" not in label for label in unit_labels)


def test_report_labels_bottleneck_mean_over_the_whole_window() -> None:
    html = render_fidelity_report(_summary())

    assert "Whole-window mean bottleneck rate (bit/s)" in html
    assert "Mean connected bottleneck rate" not in html


def test_report_labels_best_link_mean_over_the_whole_window() -> None:
    html = render_fidelity_report(_summary())

    assert "Whole-window mean best-link reference rate (bit/s)" in html
    assert "Mean best adaptive PHY rate" not in html


def test_report_uses_scientific_refinement_metric_labels() -> None:
    html = render_fidelity_report(_summary())

    assert "Maximum absolute gaseous attenuation difference (dB)" in html
    assert "Maximum absolute apparent elevation difference (deg)" in html
    assert "Maximum absolute atmospheric excess delay difference (s)" in html
    assert "Compared link samples" in html
    assert "MODCOD disagreements" in html
    assert "Interpretation" in html


@pytest.mark.parametrize(
    "path",
    (
        "/absolute/bundle",
        "../outside",
        "cases/polar/../../outside",
        "cases\\polar\\reference",
        "https://example.invalid/bundle",
        'cases/polar/" onmouseover="alert(1)',
        "cases//polar/reference",
        "",
    ),
)
def test_report_rejects_unsafe_child_bundle_paths(path: str) -> None:
    summary = _summary()
    summary["runs"][0]["bundle_path"] = path

    with pytest.raises(ValueError, match="safe relative"):
        render_fidelity_report(summary)


@pytest.mark.parametrize("value", (nan, inf, -inf, True))
@pytest.mark.parametrize("field", ("best_link_integrated_bits", "delta_bits_to_finest"))
def test_report_rejects_invalid_plot_values(field: str, value: float) -> None:
    summary = _summary()
    summary["station_metrics"][0][field] = value

    with pytest.raises(ValueError, match="finite"):
        render_fidelity_report(summary)


def test_report_keeps_zero_and_disconnected_results_readable() -> None:
    summary = _summary()
    summary["station_metrics"] = [
        {
            **row,
            "best_link_integrated_bits": 0.0,
            "fixed_baseline_integrated_bits": 0.0,
            "delta_bits_to_finest": 0.0,
            "relative_delta_percent_to_finest": None,
            "mean_best_rate_bps": 0.0,
        }
        for row in summary["station_metrics"]
    ]
    summary["route_metrics"] = [
        {
            **summary["route_metrics"][0],
            "connected_sample_fraction": 0.0,
            "integrated_bottleneck_bits": 0.0,
            "connected_duration_s": 0.0,
            "disconnected_duration_s": 60.0,
            "connected_time_fraction": 0.0,
            "mean_bottleneck_bps": 0.0,
            "relative_delta_percent_to_finest": None,
        }
    ]

    html = render_fidelity_report(summary)

    assert html.count("All plotted values are zero.") >= 2
    assert "No connected route time was recorded." in html
    assert "Not defined (zero reference bits)" in html


def test_report_does_not_describe_a_mixed_route_table_as_fully_disconnected() -> None:
    summary = _summary()
    disconnected = {
        **summary["route_metrics"][0],
        "routing_model": "minimum_delay",
        "connected_time_fraction": 0.0,
    }
    summary["route_metrics"].append(disconnected)

    html = render_fidelity_report(summary)

    assert "No connected route time was recorded." not in html


def test_report_is_deterministic_and_does_not_mutate_summary() -> None:
    summary = _summary()
    before = deepcopy(summary)

    first = render_fidelity_report(summary)
    second = render_fidelity_report(summary)

    assert first == second
    assert summary == before
