import builtins
import csv
import json
import struct
from copy import deepcopy
from pathlib import Path

import pytest
from matplotlib import pyplot as plt

from openleo.gases_plotting import render_gases_overview

FOOTER = (
    "P.676-13 Annex 1 specific attenuation at declared homogeneous conditions; "
    "validation points only; not slant-path loss or weather."
)
GUIDE_NOTE = (
    "Validation points connected only as a visual guide; no values between validation "
    "frequencies were evaluated."
)
FIELDS = (
    "frequency_hz",
    "dry_air_specific_attenuation_db_per_km",
    "water_vapour_specific_attenuation_db_per_km",
    "total_specific_attenuation_db_per_km",
)


def _write_run(directory: Path) -> Path:
    directory.mkdir()
    with (directory / "gaseous-specific-attenuation.csv").open(
        "w", newline="", encoding="utf-8"
    ) as file:
        writer = csv.writer(file)
        writer.writerow(FIELDS)
        writer.writerows(
            (
                (
                    "12000000000",
                    "0.00869826406877357",
                    "0.00953538822024593",
                    "0.0182336522890195",
                ),
                (
                    "20000000000",
                    "0.0118835504778076",
                    "0.0970473048151117",
                    "0.108930855292919",
                ),
                (
                    "60000000000",
                    "14.6234747964861",
                    "0.154841840636247",
                    "14.7783166371223",
                ),
                (
                    "90000000000",
                    "0.0388697110724235",
                    "0.341973394422181",
                    "0.380843105494605",
                ),
                (
                    "130000000000",
                    "0.0415090835995228",
                    "0.751844703646129",
                    "0.793353787245652",
                ),
            )
        )
    (directory / "gaseous-specific-attenuation-summary.json").write_text(
        json.dumps(
            {
                "schema_version": "1",
                "benchmark_name": "test-p676",
                "recommendation": "ITU-R P.676-13",
                "method": "annex1_line_by_line_specific_attenuation",
                "provenance": {"configuration": {"sha256": "a" * 64}},
                "conditions": {
                    "dry_air_pressure_hpa": 1013.25,
                    "temperature_k": 288.15,
                    "water_vapour_density_g_per_m3": 7.5,
                    "water_vapour_partial_pressure_hpa": 9.97288878634056,
                    "water_vapour_density_conversion_constant": 216.7,
                },
                "case_count": 5,
                "frequencies_hz": [
                    12000000000.0,
                    20000000000.0,
                    60000000000.0,
                    90000000000.0,
                    130000000000.0,
                ],
                "versions": {"openleo-link": "0.2.0b1"},
                "coefficient_source": {
                    "repository": "https://github.com/inigodelportillo/ITU-Rpy",
                    "commit": "f739993c4b6d34076de22249ef53d03fa5a53d73",
                    "license": "MIT",
                    "adapted_files": [
                        "itur/models/itu676.py",
                        "itur/data/676/v13_lines_oxygen.txt",
                        "itur/data/676/v13_lines_water_vapour.txt",
                    ],
                },
                "official_validation": {
                    "recommendation": "ITU-R P.676-13",
                    "workbook": {
                        "url": (
                            "https://www.itu.int/en/ITU-R/study-groups/rsg3/rwp3m/"
                            "Validation%20Example/CG-3M3J-13-ValEx-Rev8.3.0.xlsx"
                        ),
                        "sha256": (
                            "e2d8d864c80f59752318548cdd75d818792b44574da6e41dbdc5cb722aab7546"
                        ),
                        "version": "Rev8.3.0",
                    },
                },
                "ordering": {
                    "frequencies": "strictly increasing configuration order",
                    "cases": "frequency order",
                },
                "numeric_format": {
                    "floats": "Python format(value, '.15g')",
                    "csv": "UTF-8 with LF line endings",
                    "json": "UTF-8, sorted keys, indentation 2, LF trailing newline",
                },
                "limitations": [
                    "specific attenuation in dB/km at one declared homogeneous state; not integrated slant-path attenuation",
                    "no atmospheric profile, refraction, rain, cloud, fog, scintillation, availability, or propagation-combination model",
                    "no live weather, ERA5, radiosonde, or instantaneous local-weather claim",
                    "no calibrated RF or operator-performance validation claim",
                    "official validation workbook is not redistributed; public results reproduce literal published cases",
                ],
            }
        ),
        encoding="utf-8",
    )
    return directory


def _read_summary(run: Path) -> dict:
    return json.loads(
        (run / "gaseous-specific-attenuation-summary.json").read_text(encoding="utf-8")
    )


def _write_summary(run: Path, summary: dict) -> None:
    (run / "gaseous-specific-attenuation-summary.json").write_text(
        json.dumps(summary), encoding="utf-8"
    )


def _read_rows(run: Path) -> list[dict[str, str]]:
    with (run / "gaseous-specific-attenuation.csv").open(encoding="utf-8", newline="") as file:
        return list(csv.DictReader(file))


def _write_rows(run: Path, rows: list[dict[str, str]], fields=FIELDS) -> None:
    with (run / "gaseous-specific-attenuation.csv").open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def _png_pixels_per_metre(path: Path) -> tuple[int, int, int]:
    data = path.read_bytes()
    offset = 8
    while offset < len(data):
        length = struct.unpack(">I", data[offset : offset + 4])[0]
        name = data[offset + 4 : offset + 8]
        payload = data[offset + 8 : offset + 8 + length]
        if name == b"pHYs":
            return struct.unpack(">IIB", payload)
        offset += length + 12
    raise AssertionError("PNG is missing pHYs resolution metadata")


def test_render_gases_overview_writes_svg_with_validation_context(tmp_path: Path) -> None:
    output = tmp_path / "plots" / "gases.svg"

    assert render_gases_overview(_write_run(tmp_path / "run"), output) == output

    svg = output.read_text(encoding="utf-8")
    assert "test-p676" in svg
    assert FOOTER in svg
    assert GUIDE_NOTE in svg
    assert "validation points only" in svg
    assert "a" * 64 in svg


def test_render_gases_overview_svg_is_byte_stable(tmp_path: Path) -> None:
    run = _write_run(tmp_path / "run")
    first = tmp_path / "first.svg"
    second = tmp_path / "second.svg"

    render_gases_overview(run, first)
    render_gases_overview(run, second)

    assert first.read_bytes() == second.read_bytes()


def test_render_gases_overview_uses_large_log_scale_figure(tmp_path: Path, monkeypatch) -> None:
    figures = []
    close = plt.close
    monkeypatch.setattr(plt, "close", figures.append)

    render_gases_overview(_write_run(tmp_path / "run"), tmp_path / "gases.svg")

    figure = figures[-1]
    try:
        assert tuple(figure.get_size_inches()) == pytest.approx((14.0, 7.0))
        axis = figure.axes[0]
        assert axis.get_yscale() == "log"
        assert axis.get_xlabel() == "Frequency (GHz)"
        assert axis.get_ylabel() == "Specific attenuation (dB/km)"
        assert len(axis.lines) == 3
        assert tuple(line.get_label() for line in axis.lines) == (
            "Dry air",
            "Water vapour",
            "Total",
        )
        assert all(len(line.get_xdata()) == 5 for line in axis.lines)
        assert tuple(line.get_marker() for line in axis.lines) == ("o", "s", "^")
        assert all(line.get_linewidth() == 2.0 for line in axis.lines)
        assert figure._suptitle.get_fontsize() == 17
        assert next(text for text in figure.texts if text.get_text() == FOOTER).get_fontsize() == 10
    finally:
        close(figure)


def test_render_gases_overview_writes_png(tmp_path: Path) -> None:
    output = tmp_path / "gases.png"

    render_gases_overview(_write_run(tmp_path / "run"), output)

    assert output.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    x_pixels_per_metre, y_pixels_per_metre, unit = _png_pixels_per_metre(output)
    assert unit == 1
    assert x_pixels_per_metre == pytest.approx(200 / 0.0254, abs=1)
    assert y_pixels_per_metre == pytest.approx(200 / 0.0254, abs=1)


@pytest.mark.parametrize(
    ("run_kind", "output_name", "message"),
    [
        ("missing", "gases.svg", "could not read gaseous-specific-attenuation-summary.json"),
        ("malformed", "gases.svg", "schema_version"),
        ("missing", "gases.txt", "output_path must end in .svg or .png"),
    ],
)
def test_render_gases_overview_rejects_bad_inputs(tmp_path, run_kind, output_name, message) -> None:
    run_directory = tmp_path / "run"
    if run_kind == "malformed":
        run_directory.mkdir()
        (run_directory / "gaseous-specific-attenuation-summary.json").write_text(
            "{}", encoding="utf-8"
        )
    output_path = tmp_path / output_name

    with pytest.raises(ValueError, match=message):
        render_gases_overview(run_directory, output_path)

    assert not output_path.exists()


def test_render_gases_overview_missing_matplotlib_returns_install_error(
    tmp_path, monkeypatch
) -> None:
    original_import = builtins.__import__

    def block_matplotlib(name, *args, **kwargs):
        if name == "matplotlib":
            raise ModuleNotFoundError("No module named 'matplotlib'", name="matplotlib")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", block_matplotlib)

    with pytest.raises(ValueError, match=r"install openleo-link\[plot\]"):
        render_gases_overview(_write_run(tmp_path / "run"), tmp_path / "gases.svg")


def test_render_gases_overview_rejects_negative_or_inconsistent_values(tmp_path: Path) -> None:
    run = _write_run(tmp_path / "run")
    csv_path = run / "gaseous-specific-attenuation.csv"
    text = csv_path.read_text(encoding="utf-8")
    csv_path.write_text(
        text.replace("0.0182336522890195", "0.1", 1),
        encoding="utf-8",
        newline="\n",
    )

    with pytest.raises(ValueError, match="total attenuation must equal dry plus water vapour"):
        render_gases_overview(run, tmp_path / "gases.svg")


def test_render_gases_overview_rejects_csv_frequency_not_declared_by_summary(
    tmp_path: Path,
) -> None:
    run = _write_run(tmp_path / "run")
    rows = _read_rows(run)
    rows[0]["frequency_hz"] = "13000000000"
    _write_rows(run, rows)

    with pytest.raises(ValueError, match="frequencies_hz must match CSV frequency_hz"):
        render_gases_overview(run, tmp_path / "gases.svg")


def test_render_gases_overview_rejects_zero_attenuation_on_log_scale(tmp_path: Path) -> None:
    run = _write_run(tmp_path / "run")
    rows = _read_rows(run)
    rows[0]["water_vapour_specific_attenuation_db_per_km"] = "0"
    rows[0]["total_specific_attenuation_db_per_km"] = rows[0][
        "dry_air_specific_attenuation_db_per_km"
    ]
    _write_rows(run, rows)

    with pytest.raises(ValueError, match="log scale requires strictly positive attenuation"):
        render_gases_overview(run, tmp_path / "gases.svg")


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ("schema", "schema_version"),
        ("extra", "unexpected fields"),
        ("recommendation", "recommendation"),
        ("method", "method"),
        ("configuration_hash", "configuration.sha256"),
        ("derived_pressure", "water_vapour_partial_pressure_hpa"),
        ("ordering", "ordering"),
        ("numeric_format", "numeric_format"),
        ("coefficient_source", "coefficient_source"),
        ("workbook_hash", "official_validation"),
        ("versions", "versions"),
        ("frequencies", "frequencies_hz"),
        ("limitations", "limitations"),
        ("case_count", "case_count"),
    ],
)
def test_render_gases_overview_rejects_invalid_summary_contract(
    tmp_path: Path, change: str, message: str
) -> None:
    run = _write_run(tmp_path / "run")
    summary = deepcopy(_read_summary(run))
    if change == "schema":
        summary["schema_version"] = "2"
    elif change == "extra":
        summary["unexpected"] = True
    elif change == "recommendation":
        summary["recommendation"] = "ITU-R P.676-12"
    elif change == "method":
        summary["method"] = "approximate"
    elif change == "configuration_hash":
        summary["provenance"]["configuration"]["sha256"] = "not-a-hash"
    elif change == "derived_pressure":
        summary["conditions"]["water_vapour_partial_pressure_hpa"] = 10.0
    elif change == "ordering":
        summary["ordering"]["cases"] = "arbitrary"
    elif change == "numeric_format":
        summary["numeric_format"]["floats"] = "binary"
    elif change == "coefficient_source":
        summary["coefficient_source"]["commit"] = "0" * 40
    elif change == "workbook_hash":
        summary["official_validation"]["workbook"]["sha256"] = "0" * 64
    elif change == "versions":
        summary["versions"]["other"] = "1"
    elif change == "frequencies":
        summary["frequencies_hz"][0] = 13e9
    elif change == "limitations":
        summary["limitations"] = summary["limitations"][:-1]
    elif change == "case_count":
        summary["case_count"] = 4
    _write_summary(run, summary)

    with pytest.raises(ValueError, match=message):
        render_gases_overview(run, tmp_path / "gases.svg")


def test_render_gases_overview_rejects_duplicate_summary_keys(tmp_path: Path) -> None:
    run = _write_run(tmp_path / "run")
    path = run / "gaseous-specific-attenuation-summary.json"
    text = path.read_text(encoding="utf-8").replace(
        '"schema_version": "1"', '"schema_version": "1", "schema_version": "1"', 1
    )
    path.write_text(text, encoding="utf-8")

    with pytest.raises(ValueError, match="duplicate JSON key"):
        render_gases_overview(run, tmp_path / "gases.svg")


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ("header", "CSV fields"),
        ("non_finite", "must be finite"),
        ("negative", "log scale requires strictly positive attenuation"),
        ("unsorted", "strictly increasing"),
        ("out_of_range", "between 1e9 and 1e12"),
    ],
)
def test_render_gases_overview_rejects_invalid_csv_contract(
    tmp_path: Path, change: str, message: str
) -> None:
    run = _write_run(tmp_path / "run")
    rows = _read_rows(run)
    fields = FIELDS
    if change == "header":
        fields = FIELDS[:-1]
        rows = [{key: value for key, value in row.items() if key in fields} for row in rows]
    elif change == "non_finite":
        rows[0]["dry_air_specific_attenuation_db_per_km"] = "nan"
    elif change == "negative":
        rows[0]["water_vapour_specific_attenuation_db_per_km"] = "-1"
    elif change == "unsorted":
        rows[1]["frequency_hz"] = rows[0]["frequency_hz"]
    elif change == "out_of_range":
        rows[0]["frequency_hz"] = "1000000"
    _write_rows(run, rows, fields)

    with pytest.raises(ValueError, match=message):
        render_gases_overview(run, tmp_path / "gases.svg")


def test_render_gases_overview_bounds_artifact_sizes_and_rows(tmp_path: Path) -> None:
    summary_run = _write_run(tmp_path / "summary-run")
    (summary_run / "gaseous-specific-attenuation-summary.json").write_bytes(b"x" * 1_000_001)
    with pytest.raises(ValueError, match="exceeds 1000000 bytes"):
        render_gases_overview(summary_run, tmp_path / "summary.svg")

    csv_run = _write_run(tmp_path / "csv-run")
    (csv_run / "gaseous-specific-attenuation.csv").write_bytes(b"x" * 1_000_001)
    with pytest.raises(ValueError, match="exceeds 1000000 bytes"):
        render_gases_overview(csv_run, tmp_path / "csv.svg")

    rows_run = _write_run(tmp_path / "rows-run")
    _write_rows(rows_run, _read_rows(rows_run) * 201)
    with pytest.raises(ValueError, match="exceeds 1000 rows"):
        render_gases_overview(rows_run, tmp_path / "rows.svg")
