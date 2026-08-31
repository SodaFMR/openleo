import builtins
import csv
import json
import re
from copy import deepcopy
from pathlib import Path

import pytest
from matplotlib import pyplot as plt

from openleo.sensitivity_plotting import render_sensitivity_overview

FOOTER = (
    "Deterministic OAT assumed ranges; free-space synthetic RF; no probability interval; "
    "Shannon-Hartley upper bound, not throughput."
)
NON_RANKING = "Heterogeneous RF assumed spans are not an importance ranking."
FIELDS = (
    "parameter",
    "unit",
    "baseline_value",
    "case_value",
    "is_nominal",
    "row_count",
    "sampled_aos_utc",
    "sampled_los_utc",
    "sampled_duration_s",
    "maximum_elevation_deg",
    "minimum_range_m",
    "maximum_carrier_to_noise_density_db_hz",
    "maximum_shannon_capacity_upper_bound_bps",
    "integrated_shannon_capacity_upper_bound_bits",
    "maximum_capacity_upper_bound_delta_bps",
    "maximum_capacity_upper_bound_relative_change_percent",
    "integrated_capacity_upper_bound_delta_bits",
    "integrated_capacity_upper_bound_relative_change_percent",
)
BASELINE = {
    "row_count": 3,
    "sampled_aos_utc": "2026-08-30T06:15:30Z",
    "sampled_los_utc": "2026-08-30T06:17:30Z",
    "sampled_duration_s": 120.0,
    "maximum_elevation_deg": 60.0,
    "minimum_range_m": 400_000.0,
    "maximum_carrier_to_noise_density_db_hz": 80.0,
    "maximum_shannon_capacity_upper_bound_bps": 6_000_000.0,
    "integrated_shannon_capacity_upper_bound_bits": 600_000_000.0,
}
BASELINE_CONTEXT = {
    "element_epoch_utc": "2026-08-30T11:57:48.556224Z",
    "start_element_age_days": -0.23875643777777777,
    "stop_element_age_days": -0.23181199333333333,
    "maximum_absolute_element_age_days": 0.23875643777777777,
    "leap_second_table_source": "skyfield-builtin",
    "leap_second_table_sha256": "d" * 64,
    "warnings": [],
}


def _summary() -> dict:
    return {
        "schema_version": "1",
        "study_name": "test-oat",
        "method": "deterministic_one_at_a_time",
        "provenance": {
            "scenario": {"sha256": "a" * 64},
            "sensitivity": {"sha256": "b" * 64},
        },
        "sweeps": [
            {"parameter": "time_window.step_s", "unit": "s", "values": [1.0, 10.0]},
            {
                "parameter": "time_window.minimum_elevation_deg",
                "unit": "deg",
                "values": [10.0, 20.0],
            },
            {
                "parameter": "radio_link.eirp_dbw",
                "unit": "dBW",
                "values": [7.0, 10.0, 13.0],
            },
        ],
        "baseline_inputs": {
            "scenario_name": "test-pass",
            "orbit": {
                "source_url": "https://example.invalid/orbit.csv",
                "retrieved_at_utc": "2026-08-30T22:01:11Z",
                "terms_url": "https://example.invalid/terms",
                "sha256": "c" * 64,
            },
            "ground_station": {
                "name": "Cartagena",
                "latitude_deg": 37.6057,
                "longitude_deg": -0.9913,
                "height_m": 20.0,
            },
            "time_window": {
                "start_utc": "2026-08-30T06:14:00Z",
                "stop_utc": "2026-08-30T06:24:00Z",
                "step_s": 10.0,
                "minimum_elevation_deg": 10.0,
            },
            "radio_link": {
                "carrier_frequency_hz": 2_200_000_000.0,
                "channel_bandwidth_hz": 1_000_000.0,
                "eirp_dbw": 10.0,
                "receiver_gain_dbi": 20.0,
                "system_noise_temperature_k": 300.0,
                "miscellaneous_loss_db": 2.0,
            },
        },
        "baseline_context": deepcopy(BASELINE_CONTEXT),
        "baseline": deepcopy(BASELINE),
        "sweep_count": 3,
        "case_count": 7,
        "versions": {"openleo-link": "0.2.0a1", "skyfield": "1.55", "sgp4": "2.27"},
        "ordering": {
            "sweeps": "sensitivity JSON file order",
            "cases": "sweep order, then ascending declared value order",
        },
        "numeric_format": {
            "floats": "Python format(value, '.12g')",
            "timestamps": "ISO 8601 UTC with Z",
            "zero_denominator_relative_change": "null in JSON; empty CSV field",
        },
        "limitations": [
            "deterministic one-at-a-time assumed ranges, not probability distributions",
            "no confidence, credible, coverage, or standard-uncertainty interval",
            "no input correlations or covariance propagation",
            "frozen orbital geometry from public GP elements",
            "synthetic RF assumptions and free-space-only propagation",
            "Shannon-Hartley quantities are theoretical upper bounds, not throughput",
        ],
    }


def _row(
    parameter: str,
    unit: str,
    baseline_value: float,
    case_value: float,
    integrated_bits: float,
    maximum_bps: float,
) -> dict[str, str]:
    maximum_delta = maximum_bps - BASELINE["maximum_shannon_capacity_upper_bound_bps"]
    integrated_delta = integrated_bits - BASELINE["integrated_shannon_capacity_upper_bound_bits"]
    nominal = case_value == baseline_value
    metrics = (
        BASELINE
        if nominal
        else {
            **BASELINE,
            "maximum_shannon_capacity_upper_bound_bps": maximum_bps,
            "integrated_shannon_capacity_upper_bound_bits": integrated_bits,
        }
    )
    return {
        "parameter": parameter,
        "unit": unit,
        "baseline_value": str(baseline_value),
        "case_value": str(case_value),
        "is_nominal": str(nominal).lower(),
        "row_count": str(metrics["row_count"]),
        "sampled_aos_utc": str(metrics["sampled_aos_utc"]),
        "sampled_los_utc": str(metrics["sampled_los_utc"]),
        "sampled_duration_s": str(metrics["sampled_duration_s"]),
        "maximum_elevation_deg": str(metrics["maximum_elevation_deg"]),
        "minimum_range_m": str(metrics["minimum_range_m"]),
        "maximum_carrier_to_noise_density_db_hz": str(
            metrics["maximum_carrier_to_noise_density_db_hz"]
        ),
        "maximum_shannon_capacity_upper_bound_bps": str(maximum_bps),
        "integrated_shannon_capacity_upper_bound_bits": str(integrated_bits),
        "maximum_capacity_upper_bound_delta_bps": str(maximum_delta),
        "maximum_capacity_upper_bound_relative_change_percent": str(
            maximum_delta / BASELINE["maximum_shannon_capacity_upper_bound_bps"] * 100.0
        ),
        "integrated_capacity_upper_bound_delta_bits": str(integrated_delta),
        "integrated_capacity_upper_bound_relative_change_percent": str(
            integrated_delta / BASELINE["integrated_shannon_capacity_upper_bound_bits"] * 100.0
        ),
    }


def _rows() -> list[dict[str, str]]:
    return [
        _row("time_window.step_s", "s", 10.0, 1.0, 610_000_000.0, 6_000_000.0),
        _row("time_window.step_s", "s", 10.0, 10.0, 600_000_000.0, 6_000_000.0),
        _row(
            "time_window.minimum_elevation_deg",
            "deg",
            10.0,
            10.0,
            600_000_000.0,
            6_000_000.0,
        ),
        _row(
            "time_window.minimum_elevation_deg",
            "deg",
            10.0,
            20.0,
            400_000_000.0,
            6_000_000.0,
        ),
        _row("radio_link.eirp_dbw", "dBW", 10.0, 7.0, 400_000_000.0, 4_000_000.0),
        _row("radio_link.eirp_dbw", "dBW", 10.0, 10.0, 600_000_000.0, 6_000_000.0),
        _row("radio_link.eirp_dbw", "dBW", 10.0, 13.0, 800_000_000.0, 8_000_000.0),
    ]


def _write_run(directory: Path, summary: dict | None = None, rows=None) -> Path:
    directory.mkdir()
    (directory / "sensitivity-summary.json").write_text(
        json.dumps(summary if summary is not None else _summary()), encoding="utf-8"
    )
    with (directory / "sensitivity.csv").open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(_rows() if rows is None else rows)
    return directory


def test_render_sensitivity_overview_writes_fixed_selectable_svg(tmp_path: Path) -> None:
    output = tmp_path / "plots" / "sensitivity.svg"

    assert render_sensitivity_overview(_write_run(tmp_path / "run"), output) == output

    svg = output.read_text(encoding="utf-8")
    assert "test-oat" in svg
    assert "numerical reference, not truth" in svg
    assert "nominal 10° mask" in svg
    assert NON_RANKING in svg
    assert FOOTER in svg
    assert "a" * 64 in svg
    assert "b" * 64 in svg
    assert "<text" in svg


def test_render_sensitivity_overview_keeps_top_rf_labels_clear_of_title(tmp_path: Path) -> None:
    output = tmp_path / "sensitivity.svg"
    summary = _summary()
    summary["sweeps"].extend(
        (
            {
                "parameter": "radio_link.system_noise_temperature_k",
                "unit": "K",
                "values": [200.0, 300.0, 400.0],
            },
            {
                "parameter": "radio_link.miscellaneous_loss_db",
                "unit": "dB",
                "values": [0.0, 2.0, 4.0],
            },
        )
    )
    rows = [
        *_rows(),
        _row(
            "radio_link.system_noise_temperature_k",
            "K",
            300.0,
            200.0,
            700_000_000.0,
            7_000_000.0,
        ),
        _row(
            "radio_link.system_noise_temperature_k",
            "K",
            300.0,
            300.0,
            600_000_000.0,
            6_000_000.0,
        ),
        _row(
            "radio_link.system_noise_temperature_k",
            "K",
            300.0,
            400.0,
            500_000_000.0,
            5_000_000.0,
        ),
        _row(
            "radio_link.miscellaneous_loss_db",
            "dB",
            2.0,
            0.0,
            700_000_000.0,
            7_000_000.0,
        ),
        _row(
            "radio_link.miscellaneous_loss_db",
            "dB",
            2.0,
            2.0,
            600_000_000.0,
            6_000_000.0,
        ),
        _row(
            "radio_link.miscellaneous_loss_db",
            "dB",
            2.0,
            4.0,
            500_000_000.0,
            5_000_000.0,
        ),
    ]
    summary["sweep_count"] = len(summary["sweeps"])
    summary["case_count"] = len(rows)

    render_sensitivity_overview(_write_run(tmp_path / "run", summary, rows), output)

    svg = output.read_text(encoding="utf-8")
    title_y = _svg_text_y(svg, "C  RF assumption sweeps")
    label_ys = tuple(_svg_text_y(svg, text) for text in ("0 dB", "2 dB", "4 dB"))
    assert min(label_ys) - title_y >= 20.0


def test_render_sensitivity_overview_uses_fixed_geometry(tmp_path: Path, monkeypatch) -> None:
    figures = []
    close = plt.close
    monkeypatch.setattr(plt, "close", figures.append)

    render_sensitivity_overview(_write_run(tmp_path / "run"), tmp_path / "plot.svg")

    figure = figures[-1]
    try:
        assert tuple(figure.get_size_inches()) == pytest.approx((15.0, 8.5))
        assert figure.get_layout_engine() is None
        assert len(figure.axes) == 3
        grid = figure.axes[0].get_subplotspec().get_gridspec()
        assert (grid.left, grid.right, grid.bottom, grid.top) == pytest.approx(
            (0.075, 0.975, 0.155, 0.865)
        )
        assert (grid.wspace, grid.hspace) == pytest.approx((0.34, 0.52))
        assert tuple(grid.get_height_ratios()) == pytest.approx((1.0, 1.15))
        assert tuple(label.get_text() for label in figure.axes[2].get_yticklabels()) == ("EIRP",)
    finally:
        close(figure)


@pytest.mark.parametrize(
    ("parameter", "unit", "values", "baseline", "label"),
    [
        ("radio_link.eirp_dbw", "dBW", (7.0, 10.0, 13.0), 10.0, "EIRP"),
        (
            "radio_link.system_noise_temperature_k",
            "K",
            (200.0, 300.0, 400.0),
            300.0,
            "Noise temp.",
        ),
        ("radio_link.miscellaneous_loss_db", "dB", (0.0, 2.0, 4.0), 2.0, "Misc. loss"),
    ],
)
def test_render_sensitivity_overview_uses_compact_rf_labels(
    tmp_path: Path, monkeypatch, parameter, unit, values, baseline, label
) -> None:
    summary = _summary()
    summary["sweeps"][2] = {"parameter": parameter, "unit": unit, "values": list(values)}
    rows = _rows()
    for row, value in zip(rows[-3:], values, strict=True):
        row.update(
            parameter=parameter,
            unit=unit,
            baseline_value=str(baseline),
            case_value=str(value),
            is_nominal=str(value == baseline).lower(),
        )
    figures = []
    close = plt.close
    monkeypatch.setattr(plt, "close", figures.append)

    render_sensitivity_overview(
        _write_run(tmp_path / "run", summary=summary, rows=rows), tmp_path / "plot.svg"
    )

    try:
        assert tuple(item.get_text() for item in figures[-1].axes[2].get_yticklabels()) == (label,)
    finally:
        close(figures[-1])


def test_render_sensitivity_overview_svg_is_byte_stable(tmp_path: Path) -> None:
    run = _write_run(tmp_path / "run")
    first = tmp_path / "first.svg"
    second = tmp_path / "second.svg"

    render_sensitivity_overview(run, first)
    render_sensitivity_overview(run, second)

    assert first.read_bytes() == second.read_bytes()


def test_render_sensitivity_overview_writes_200_dpi_png(tmp_path: Path) -> None:
    output = tmp_path / "sensitivity.png"

    render_sensitivity_overview(_write_run(tmp_path / "run"), output)

    assert output.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    assert _png_dpi(output.read_bytes()) == pytest.approx((200.0, 200.0), abs=0.1)


def test_render_sensitivity_overview_missing_matplotlib_has_install_hint(
    tmp_path: Path, monkeypatch
) -> None:
    original_import = builtins.__import__

    def block_matplotlib(name, *args, **kwargs):
        if name == "matplotlib":
            raise ModuleNotFoundError("No module named 'matplotlib'", name="matplotlib")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", block_matplotlib)

    with pytest.raises(
        ValueError, match="plotting requires Matplotlib; install openleo-link\\[plot\\]"
    ):
        render_sensitivity_overview(_write_run(tmp_path / "run"), tmp_path / "plot.svg")


def test_render_sensitivity_overview_closes_figure_when_save_fails(tmp_path, monkeypatch) -> None:
    figures = set(plt.get_fignums())

    def fail_save(*args, **kwargs):
        raise OSError("save failed")

    monkeypatch.setattr("matplotlib.figure.Figure.savefig", fail_save)
    try:
        with pytest.raises(OSError, match="save failed"):
            render_sensitivity_overview(_write_run(tmp_path / "run"), tmp_path / "plot.svg")
        assert set(plt.get_fignums()) == figures
    finally:
        for figure in set(plt.get_fignums()) - figures:
            plt.close(figure)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda summary: summary.update(extra=True), "unexpected fields"),
        (lambda summary: summary.update(schema_version="2"), "schema_version must be 1"),
        (lambda summary: summary.update(method="other"), "method"),
        (lambda summary: summary.update(study_name=""), "study_name"),
        (
            lambda summary: summary["provenance"]["scenario"].update(sha256="A" * 64),
            "provenance.scenario.sha256",
        ),
        (lambda summary: summary["versions"].pop("openleo-link"), "versions"),
        (lambda summary: summary["ordering"].update(cases="other"), "ordering"),
        (lambda summary: summary["numeric_format"].update(floats="other"), "numeric_format"),
        (lambda summary: summary["limitations"].pop(), "limitations"),
        (lambda summary: summary["baseline_inputs"].pop("orbit"), "baseline_inputs"),
        (lambda summary: summary["baseline_context"].pop("warnings"), "baseline_context"),
        (lambda summary: summary["baseline"].pop("minimum_range_m"), "baseline"),
    ],
)
def test_render_sensitivity_overview_rejects_malformed_summary_structure(
    tmp_path: Path, mutate, message: str
) -> None:
    summary = _summary()
    mutate(summary)

    with pytest.raises(ValueError, match=message):
        render_sensitivity_overview(
            _write_run(tmp_path / "run", summary=summary), tmp_path / "plot.svg"
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("element_epoch_utc", "2026-08-30T11:57:48", "element_epoch_utc"),
        ("start_element_age_days", float("inf"), "start_element_age_days"),
        ("stop_element_age_days", float("nan"), "stop_element_age_days"),
        ("maximum_absolute_element_age_days", -1.0, "maximum_absolute_element_age_days"),
        ("leap_second_table_source", "", "leap_second_table_source"),
        ("leap_second_table_sha256", "D" * 64, "leap_second_table_sha256"),
        ("warnings", [1], "warnings"),
        ("warnings", "warning", "warnings"),
    ],
)
def test_render_sensitivity_overview_rejects_invalid_baseline_context(
    tmp_path: Path, field: str, value: object, message: str
) -> None:
    summary = _summary()
    summary["baseline_context"][field] = value

    with pytest.raises(ValueError, match=message):
        render_sensitivity_overview(
            _write_run(tmp_path / "run", summary=summary), tmp_path / "plot.svg"
        )


def test_render_sensitivity_overview_rejects_inconsistent_maximum_element_age(
    tmp_path: Path,
) -> None:
    summary = _summary()
    summary["baseline_context"]["maximum_absolute_element_age_days"] = 0.2

    with pytest.raises(ValueError, match="maximum_absolute_element_age_days"):
        render_sensitivity_overview(
            _write_run(tmp_path / "run", summary=summary), tmp_path / "plot.svg"
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("row_count", "0"),
        ("sampled_duration_s", "-1"),
        ("maximum_elevation_deg", "-1"),
        ("minimum_range_m", "-1"),
        ("maximum_shannon_capacity_upper_bound_bps", "-1"),
        ("integrated_shannon_capacity_upper_bound_bits", "-1"),
    ],
)
def test_render_sensitivity_overview_rejects_negative_raw_metrics(
    tmp_path: Path, field: str, value: str
) -> None:
    rows = _rows()
    rows[0][field] = value

    with pytest.raises(ValueError, match=field):
        render_sensitivity_overview(_write_run(tmp_path / "run", rows=rows), tmp_path / "plot.svg")


@pytest.mark.parametrize(
    ("mutate_summary", "mutate_rows", "message"),
    [
        (lambda value: value.update(sweep_count=4), None, "sweep_count"),
        (lambda value: value.update(case_count=8), None, "case_count"),
        (None, lambda rows: rows[0].update(unit="ms"), "unit"),
        (None, lambda rows: rows[0].update(baseline_value="5"), "baseline_value"),
        (None, lambda rows: rows[0].update(case_value="11"), "ordering"),
        (None, lambda rows: rows[1].update(is_nominal="false"), "nominal"),
        (
            None,
            lambda rows: rows[0].update(integrated_capacity_upper_bound_delta_bits="1"),
            "integrated_capacity_upper_bound_delta_bits",
        ),
        (
            None,
            lambda rows: rows[0].update(
                integrated_capacity_upper_bound_relative_change_percent="1"
            ),
            "integrated_capacity_upper_bound_relative_change_percent",
        ),
        (None, lambda rows: rows.reverse(), "ordering"),
    ],
)
def test_render_sensitivity_overview_rejects_summary_csv_inconsistency(
    tmp_path: Path, mutate_summary, mutate_rows, message: str
) -> None:
    summary = _summary()
    rows = _rows()
    if mutate_summary:
        mutate_summary(summary)
    if mutate_rows:
        mutate_rows(rows)

    with pytest.raises(ValueError, match=message):
        render_sensitivity_overview(
            _write_run(tmp_path / "run", summary=summary, rows=rows), tmp_path / "plot.svg"
        )


@pytest.mark.parametrize(
    ("parameter", "message"),
    [
        ("time_window.step_s", "sampling sweep"),
        ("time_window.minimum_elevation_deg", "elevation sweep"),
        ("radio_link.eirp_dbw", "RF sweep"),
    ],
)
def test_render_sensitivity_overview_requires_all_plot_groups(
    tmp_path: Path, parameter: str, message: str
) -> None:
    summary = _summary()
    index = next(
        index for index, sweep in enumerate(summary["sweeps"]) if sweep["parameter"] == parameter
    )
    summary["sweeps"].pop(index)
    rows = [row for row in _rows() if row["parameter"] != parameter]
    summary["sweep_count"] -= 1
    summary["case_count"] = len(rows)

    with pytest.raises(ValueError, match=message):
        render_sensitivity_overview(
            _write_run(tmp_path / "run", summary=summary, rows=rows), tmp_path / "plot.svg"
        )


def test_render_sensitivity_overview_requires_exact_csv_header(tmp_path: Path) -> None:
    run = _write_run(tmp_path / "run")
    path = run / "sensitivity.csv"
    path.write_text(path.read_text(encoding="utf-8").replace("parameter,unit", "unit,parameter", 1))

    with pytest.raises(ValueError, match="header"):
        render_sensitivity_overview(run, tmp_path / "plot.svg")


def test_render_sensitivity_overview_rejects_surplus_csv_row_columns(tmp_path: Path) -> None:
    run = _write_run(tmp_path / "run")
    path = run / "sensitivity.csv"
    lines = path.read_text(encoding="utf-8").splitlines()
    lines[1] += ",surplus"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match=r"sensitivity\.csv row 1 fields"):
        render_sensitivity_overview(run, tmp_path / "plot.svg")


def test_render_sensitivity_overview_rejects_oversized_artifacts(tmp_path: Path) -> None:
    run = _write_run(tmp_path / "run")
    with (run / "sensitivity-summary.json").open("r+b") as file:
        file.truncate(10_000_001)
    with pytest.raises(ValueError, match="sensitivity-summary.json exceeds 10000000 bytes"):
        render_sensitivity_overview(run, tmp_path / "plot.svg")

    run = _write_run(tmp_path / "second")
    with (run / "sensitivity.csv").open("r+b") as file:
        file.truncate(1_000_001)
    with pytest.raises(ValueError, match="sensitivity.csv exceeds 1000000 bytes"):
        render_sensitivity_overview(run, tmp_path / "plot.svg")


def test_render_sensitivity_overview_rejects_more_than_64_rows(tmp_path: Path) -> None:
    summary = _summary()
    rows = _rows() * 10

    with pytest.raises(ValueError, match="sensitivity.csv exceeds 64 rows"):
        render_sensitivity_overview(
            _write_run(tmp_path / "run", summary=summary, rows=rows), tmp_path / "plot.svg"
        )


def test_render_sensitivity_overview_rejects_unsupported_suffix(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="output_path must end in .svg or .png"):
        render_sensitivity_overview(_write_run(tmp_path / "run"), tmp_path / "plot.pdf")


def _png_dpi(png: bytes) -> tuple[float, float]:
    offset = 8
    while offset < len(png):
        length = int.from_bytes(png[offset : offset + 4], "big")
        if png[offset + 4 : offset + 8] == b"pHYs":
            x_ppm = int.from_bytes(png[offset + 8 : offset + 12], "big")
            y_ppm = int.from_bytes(png[offset + 12 : offset + 16], "big")
            assert png[offset + 16] == 1
            return x_ppm / 39.37007874, y_ppm / 39.37007874
        offset += length + 12
    raise AssertionError("PNG is missing pHYs metadata")


def _svg_text_y(svg: str, text: str) -> float:
    pattern = rf'<text[^>]* y="([0-9.]+)"[^>]*>{re.escape(text)}</text>'
    values = [float(value) for value in re.findall(pattern, svg)]
    assert len(values) == 1
    return values[0]
