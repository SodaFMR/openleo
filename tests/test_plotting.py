import builtins
import csv
import json
import re
from math import hypot
from pathlib import Path

import pytest
from matplotlib import pyplot as plt

from openleo.plotting import render_pass_overview

LIMITATION = (
    "Frozen orbital geometry; synthetic RF inputs; free-space only; "
    "Shannon-Hartley upper bound, not throughput."
)
FIELDS = (
    "timestamp_utc",
    "azimuth_deg",
    "elevation_deg",
    "doppler_hz",
    "carrier_to_noise_density_db_hz",
    "shannon_capacity_upper_bound_bps",
)


def _write_run(directory: Path) -> Path:
    directory.mkdir()
    rows = (
        ("2026-08-30T06:15:30Z", "0", "0", "1000", "42", "0"),
        ("2026-08-30T06:16:30Z", "180", "90", "0", "55", "6000000"),
        ("2026-08-30T06:17:30Z", "360", "0", "-1000", "-42", "0"),
    )
    with (directory / "trace.csv").open("w", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)
        writer.writerow(FIELDS)
        writer.writerows(rows)
    (directory / "summary.json").write_text(
        json.dumps(
            {
                "schema_version": "1",
                "scenario_name": "test-pass",
                "provenance": {"scenario": {"sha256": "a" * 64}},
                "versions": {"openleo-link": "0.1.0"},
                "row_count": 3,
                "sampled_aos_utc": "2026-08-30T06:15:30Z",
                "sampled_los_utc": "2026-08-30T06:17:30Z",
            }
        ),
        encoding="utf-8",
    )
    return directory


def test_render_pass_overview_writes_svg_with_artifact_context(tmp_path: Path) -> None:
    output = tmp_path / "plots" / "pass-overview.svg"

    assert render_pass_overview(_write_run(tmp_path / "run"), output) == output

    svg = output.read_text(encoding="utf-8")
    assert "test-pass" in svg
    assert LIMITATION in svg
    assert "a" * 64 in svg


def test_render_pass_overview_svg_is_byte_stable(tmp_path: Path) -> None:
    run = _write_run(tmp_path / "run")
    first = tmp_path / "first.svg"
    second = tmp_path / "second.svg"

    render_pass_overview(run, first)
    render_pass_overview(run, second)

    assert first.read_bytes() == second.read_bytes()


def test_render_pass_overview_reserves_space_for_the_limitation_footer(tmp_path: Path) -> None:
    output = tmp_path / "pass-overview.svg"

    render_pass_overview(_write_run(tmp_path / "run"), output)

    svg = output.read_text(encoding="utf-8")
    footer_y = _svg_text_y(svg, LIMITATION)
    label_ys = _svg_text_ys(svg, "Minutes from sampled AOS")
    assert footer_y - max(label_ys) >= 20


def test_render_pass_overview_reserves_space_for_the_scenario_title(tmp_path: Path) -> None:
    output = tmp_path / "pass-overview.svg"

    render_pass_overview(_write_run(tmp_path / "run"), output)

    svg = output.read_text(encoding="utf-8")
    assert _svg_text_y(svg, "Sky track") - _svg_text_y(svg, "test-pass") >= 20


def test_render_pass_overview_writes_png(tmp_path: Path) -> None:
    output = tmp_path / "pass-overview.png"

    render_pass_overview(_write_run(tmp_path / "run"), output)

    assert output.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    x_dpi, y_dpi = _png_dpi(output.read_bytes())
    assert x_dpi == pytest.approx(200, abs=0.1)
    assert y_dpi == pytest.approx(200, abs=0.1)


def test_render_pass_overview_places_zenith_at_the_polar_centre(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    figures = []
    close = plt.close
    monkeypatch.setattr(plt, "close", figures.append)

    render_pass_overview(_write_run(tmp_path / "run"), tmp_path / "pass-overview.svg")

    polar = next(axis for axis in figures[-1].axes if axis.name == "polar")
    centre = polar.transAxes.transform((0.5, 0.5))
    zenith = polar.transData.transform((0.0, 0.0))
    horizon = polar.transData.transform((0.0, 90.0))
    close(figures[-1])
    assert hypot(*(zenith - centre)) < 1.0
    assert hypot(*(horizon - centre)) > 1.0


def test_render_pass_overview_labels_polar_rings_as_elevation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    figures = []
    close = plt.close
    monkeypatch.setattr(plt, "close", figures.append)

    render_pass_overview(_write_run(tmp_path / "run"), tmp_path / "pass-overview.svg")

    polar = next(axis for axis in figures[-1].axes if axis.name == "polar")
    try:
        assert tuple(polar.get_yticks()) == (20.0, 40.0, 60.0, 80.0)
        assert tuple(label.get_text() for label in polar.get_yticklabels()) == (
            "70°",
            "50°",
            "30°",
            "10°",
        )
    finally:
        close(figures[-1])


def test_render_pass_overview_labels_nominal_first_order_doppler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    figures = []
    close = plt.close
    monkeypatch.setattr(plt, "close", figures.append)

    render_pass_overview(_write_run(tmp_path / "run"), tmp_path / "pass-overview.svg")

    try:
        assert "Nominal first-order Doppler (kHz)" in {
            axis.get_ylabel() for axis in figures[-1].axes
        }
    finally:
        close(figures[-1])


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("azimuth_deg", "-0.001", "azimuth_deg must be between 0 and 360 inclusive"),
        ("azimuth_deg", "360.001", "azimuth_deg must be between 0 and 360 inclusive"),
        ("elevation_deg", "-0.001", "elevation_deg must be between 0 and 90 inclusive"),
        ("elevation_deg", "90.001", "elevation_deg must be between 0 and 90 inclusive"),
        (
            "shannon_capacity_upper_bound_bps",
            "-0.001",
            "shannon_capacity_upper_bound_bps must be non-negative",
        ),
    ],
)
def test_render_pass_overview_rejects_finite_values_outside_their_domains(
    tmp_path: Path, field: str, value: str, message: str
) -> None:
    run = _write_run(tmp_path / "run")
    _replace_trace_value(run / "trace.csv", field, value)

    with pytest.raises(ValueError, match=message):
        render_pass_overview(run, tmp_path / "pass-overview.svg")


def test_render_pass_overview_missing_matplotlib_has_install_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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
        render_pass_overview(_write_run(tmp_path / "run"), tmp_path / "pass-overview.svg")


def test_render_pass_overview_does_not_mask_unrelated_missing_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_import = builtins.__import__

    def block_matplotlib(name, *args, **kwargs):
        if name == "matplotlib":
            raise ModuleNotFoundError("No module named 'unrelated'", name="unrelated")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", block_matplotlib)

    with pytest.raises(ModuleNotFoundError, match="No module named 'unrelated'"):
        render_pass_overview(_write_run(tmp_path / "run"), tmp_path / "pass-overview.svg")


def test_render_pass_overview_closes_figure_when_output_directory_creation_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = _write_run(tmp_path / "run")
    output = tmp_path / "plots" / "pass-overview.svg"
    figures = set(plt.get_fignums())
    mkdir = Path.mkdir

    def fail_output_mkdir(path, *args, **kwargs):
        if path == output.parent:
            raise OSError("directory failed")
        return mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", fail_output_mkdir)

    try:
        with pytest.raises(OSError, match="directory failed"):
            render_pass_overview(run, output)
        assert set(plt.get_fignums()) == figures
    finally:
        for figure in set(plt.get_fignums()) - figures:
            plt.close(figure)


def test_render_pass_overview_closes_figure_when_save_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    figures = set(plt.get_fignums())

    def fail_save(*args, **kwargs):
        raise OSError("save failed")

    monkeypatch.setattr("matplotlib.figure.Figure.savefig", fail_save)

    try:
        with pytest.raises(OSError, match="save failed"):
            render_pass_overview(_write_run(tmp_path / "run"), tmp_path / "pass-overview.svg")
        assert set(plt.get_fignums()) == figures
    finally:
        for figure in set(plt.get_fignums()) - figures:
            plt.close(figure)


@pytest.mark.parametrize(
    ("output_name", "change", "message"),
    [
        ("pass-overview.pdf", None, "output_path must end in .svg or .png"),
        ("pass-overview.svg", "missing_field", "trace.csv missing required field"),
        ("pass-overview.svg", "non_finite", "trace.csv doppler_hz must be finite"),
        ("pass-overview.svg", "non_utc", "trace.csv timestamp_utc must be UTC"),
        ("pass-overview.svg", "unsorted", "trace.csv timestamps must be strictly increasing"),
        ("pass-overview.svg", "row_count", "summary.json row_count does not match trace.csv"),
    ],
)
def test_render_pass_overview_rejects_invalid_artifacts(
    tmp_path: Path, output_name: str, change: str | None, message: str
) -> None:
    run = _write_run(tmp_path / "run")
    if change == "missing_field":
        (run / "trace.csv").write_text("timestamp_utc,azimuth_deg\n2026-08-30T06:15:30Z,20\n")
    elif change == "non_finite":
        text = (run / "trace.csv").read_text(encoding="utf-8").replace(",1000,42,", ",nan,42,")
        (run / "trace.csv").write_text(text, encoding="utf-8")
    elif change == "non_utc":
        text = (run / "trace.csv").read_text(encoding="utf-8").replace("Z", "+01:00", 1)
        (run / "trace.csv").write_text(text, encoding="utf-8")
    elif change == "unsorted":
        text = (
            (run / "trace.csv")
            .read_text(encoding="utf-8")
            .replace("2026-08-30T06:16:30Z", "2026-08-30T06:15:00Z")
        )
        (run / "trace.csv").write_text(text, encoding="utf-8")
    elif change == "row_count":
        summary = json.loads((run / "summary.json").read_text(encoding="utf-8"))
        summary["row_count"] = 4
        (run / "summary.json").write_text(json.dumps(summary), encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        render_pass_overview(run, tmp_path / output_name)


def test_render_pass_overview_rejects_oversized_summary_before_reading(tmp_path: Path) -> None:
    run = _write_run(tmp_path / "run")
    (run / "summary.json").write_bytes(b"x" * 10_000_001)

    with pytest.raises(ValueError, match="summary.json exceeds 10000000 bytes"):
        render_pass_overview(run, tmp_path / "pass-overview.svg")


def test_render_pass_overview_accepts_a_trace_larger_than_the_summary_limit(
    tmp_path: Path,
) -> None:
    run = _write_run(tmp_path / "run")
    path = run / "trace.csv"
    summary = json.loads((run / "summary.json").read_text(encoding="utf-8"))
    summary.update(
        row_count=100,
        sampled_aos_utc="2026-08-30T06:00:00Z",
        sampled_los_utc="2026-08-30T06:01:39Z",
    )
    (run / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    path.write_text(
        ",".join((*FIELDS, "padding"))
        + "\n"
        + "\n".join(
            ",".join(
                (
                    f"2026-08-30T06:{index // 60:02}:{index % 60:02}Z",
                    "20",
                    "10",
                    "1000",
                    "42",
                    "1000000",
                    "x" * 110_000,
                )
            )
            for index in range(100)
        )
        + "\n",
        encoding="utf-8",
    )

    render_pass_overview(run, tmp_path / "pass-overview.svg")


def test_render_pass_overview_rejects_oversized_trace_before_reading(tmp_path: Path) -> None:
    run = _write_run(tmp_path / "run")
    with (run / "trace.csv").open("r+b") as file:
        file.truncate(50_000_001)

    with pytest.raises(ValueError, match="trace.csv exceeds 50000000 bytes"):
        render_pass_overview(run, tmp_path / "pass-overview.svg")


def test_render_pass_overview_rejects_more_than_100000_trace_rows(tmp_path: Path) -> None:
    run = _write_run(tmp_path / "run")
    row = "2026-08-30T06:15:30Z,20,10,1000,42,1000000\n"
    (run / "trace.csv").write_text(",".join(FIELDS) + "\n" + row * 100_001, encoding="utf-8")

    with pytest.raises(ValueError, match="trace.csv exceeds 100000 rows"):
        render_pass_overview(run, tmp_path / "pass-overview.svg")


def _svg_text_ys(svg: str, text: str) -> list[float]:
    pattern = rf'<text[^>]* y="([0-9.]+)"[^>]*>{re.escape(text)}</text>'
    return [float(value) for value in re.findall(pattern, svg)]


def _svg_text_y(svg: str, text: str) -> float:
    values = _svg_text_ys(svg, text)
    assert len(values) == 1
    return values[0]


def _replace_trace_value(path: Path, field: str, value: str) -> None:
    with path.open(encoding="utf-8", newline="") as file:
        rows = list(csv.DictReader(file))
    rows[0] = {**rows[0], field: value}
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)


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
