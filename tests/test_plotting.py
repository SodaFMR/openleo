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
        ("2026-08-30T06:15:30Z", "20", "10", "1000", "42", "1000000"),
        ("2026-08-30T06:16:30Z", "180", "60", "0", "55", "6000000"),
        ("2026-08-30T06:17:30Z", "340", "10", "-1000", "42", "1000000"),
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
