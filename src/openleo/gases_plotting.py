"""Static rendering of completed P.676-13 specific-attenuation artifacts."""

from __future__ import annotations

import csv
import json
from collections.abc import Mapping
from dataclasses import dataclass
from itertools import islice, pairwise
from math import isclose, isfinite
from pathlib import Path
from typing import Any

_CSV_NAME = "gaseous-specific-attenuation.csv"
_SUMMARY_NAME = "gaseous-specific-attenuation-summary.json"
_CSV_FIELDS = (
    "frequency_hz",
    "dry_air_specific_attenuation_db_per_km",
    "water_vapour_specific_attenuation_db_per_km",
    "total_specific_attenuation_db_per_km",
)
_SUMMARY_FIELDS = (
    "schema_version",
    "benchmark_name",
    "recommendation",
    "method",
    "provenance",
    "conditions",
    "frequencies_hz",
    "case_count",
    "versions",
    "coefficient_source",
    "official_validation",
    "ordering",
    "numeric_format",
    "limitations",
)
_RECOMMENDATION = "ITU-R P.676-13"
_METHOD = "annex1_line_by_line_specific_attenuation"
_COEFFICIENT_SOURCE = {
    "repository": "https://github.com/inigodelportillo/ITU-Rpy",
    "commit": "f739993c4b6d34076de22249ef53d03fa5a53d73",
    "license": "MIT",
    "adapted_files": [
        "itur/models/itu676.py",
        "itur/data/676/v13_lines_oxygen.txt",
        "itur/data/676/v13_lines_water_vapour.txt",
    ],
}
_OFFICIAL_VALIDATION = {
    "recommendation": _RECOMMENDATION,
    "workbook": {
        "url": (
            "https://www.itu.int/en/ITU-R/study-groups/rsg3/rwp3m/Validation%20Example/"
            "CG-3M3J-13-ValEx-Rev8.3.0.xlsx"
        ),
        "sha256": "e2d8d864c80f59752318548cdd75d818792b44574da6e41dbdc5cb722aab7546",
        "version": "Rev8.3.0",
    },
}
_ORDERING = {
    "frequencies": "strictly increasing configuration order",
    "cases": "frequency order",
}
_NUMERIC_FORMAT = {
    "floats": "Python format(value, '.15g')",
    "csv": "UTF-8 with LF line endings",
    "json": "UTF-8, sorted keys, indentation 2, LF trailing newline",
}
_LIMITATIONS = (
    "specific attenuation in dB/km at one declared homogeneous state; not integrated slant-path attenuation",
    "no atmospheric profile, refraction, rain, cloud, fog, scintillation, availability, or propagation-combination model",
    "no live weather, ERA5, radiosonde, or instantaneous local-weather claim",
    "no calibrated RF or operator-performance validation claim",
    "official validation workbook is not redistributed; public results reproduce literal published cases",
)
_FOOTER = (
    "P.676-13 Annex 1 specific attenuation at declared homogeneous conditions; "
    "validation points only; not slant-path loss or weather."
)
_GUIDE_NOTE = (
    "Validation points connected only as a visual guide; no values between validation "
    "frequencies were evaluated."
)
_MAX_ARTIFACT_BYTES = 1_000_000
_MAX_ROWS = 1_000


@dataclass(frozen=True, slots=True)
class _Summary:
    benchmark_name: str
    configuration_sha256: str
    dry_air_pressure_hpa: float
    temperature_k: float
    water_vapour_density_g_per_m3: float
    frequencies_hz: tuple[float, ...]
    case_count: int
    openleo_version: str


@dataclass(frozen=True, slots=True)
class _Row:
    frequency_hz: float
    dry_air_db_per_km: float
    water_vapour_db_per_km: float
    total_db_per_km: float


def render_gases_overview(run_directory: str | Path, output_path: str | Path) -> Path:
    """Render completed schema-v1 gaseous specific-attenuation artifacts."""
    output = Path(output_path)
    suffix = output.suffix.lower()
    if suffix not in {".svg", ".png"}:
        raise ValueError("output_path must end in .svg or .png")

    run = Path(run_directory)
    summary = _read_summary(run / _SUMMARY_NAME)
    rows = _read_rows(run / _CSV_NAME)
    if summary.case_count != len(rows):
        raise ValueError("summary case_count does not match CSV row count")
    if summary.frequencies_hz != tuple(row.frequency_hz for row in rows):
        raise ValueError("summary frequencies_hz must match CSV frequency_hz values")

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

    frequencies_ghz = tuple(row.frequency_hz / 1e9 for row in rows)
    figure = plt.figure(figsize=(14, 7), facecolor="white", layout="none")
    try:
        axis = figure.add_subplot(1, 1, 1)
        figure.subplots_adjust(left=0.085, right=0.975, bottom=0.245, top=0.815)
        for values, label, color, marker in (
            (
                tuple(row.dry_air_db_per_km for row in rows),
                "Dry air",
                "#0072B2",
                "o",
            ),
            (
                tuple(row.water_vapour_db_per_km for row in rows),
                "Water vapour",
                "#D55E00",
                "s",
            ),
            (
                tuple(row.total_db_per_km for row in rows),
                "Total",
                "#009E73",
                "^",
            ),
        ):
            axis.plot(
                frequencies_ghz,
                values,
                label=label,
                color=color,
                marker=marker,
                markersize=7,
                linewidth=2,
            )
        axis.set_yscale("log")
        axis.set_xlabel("Frequency (GHz)", fontsize=12)
        axis.set_ylabel("Specific attenuation (dB/km)", fontsize=12)
        axis.grid(which="both", alpha=0.3)
        axis.legend(fontsize=11)
        axis.tick_params(labelsize=10)
        figure.suptitle("ITU-R P.676-13 Annex 1 specific attenuation", y=0.955, fontsize=17)
        figure.text(
            0.5,
            0.875,
            (
                f"{summary.benchmark_name} · "
                f"dry-air pressure {summary.dry_air_pressure_hpa:g} hPa · "
                f"temperature {summary.temperature_k:g} K · "
                f"water-vapour density {summary.water_vapour_density_g_per_m3:g} g/m³"
            ),
            ha="center",
            fontsize=11,
        )
        figure.text(0.5, 0.125, _GUIDE_NOTE, ha="center", fontsize=10)
        figure.text(0.5, 0.045, _FOOTER, ha="center", fontsize=10)
        output.parent.mkdir(parents=True, exist_ok=True)
        metadata = {
            "Title": f"{_RECOMMENDATION} Annex 1 specific attenuation",
            "Description": (
                f"benchmark={summary.benchmark_name}; "
                f"configuration_sha256={summary.configuration_sha256}; "
                f"workbook_sha256={_OFFICIAL_VALIDATION['workbook']['sha256']}; "
                f"recommendation={_RECOMMENDATION}; method={_METHOD}; "
                f"openleo={summary.openleo_version}; matplotlib={matplotlib.__version__}"
            ),
            "Creator": "OpenLEO",
            "Date": None,
        }
        with matplotlib.rc_context(
            {"svg.fonttype": "none", "svg.hashsalt": "openleo-gases-overview"}
        ):
            figure.savefig(output, dpi=200 if suffix == ".png" else None, metadata=metadata)
    finally:
        plt.close(figure)
    return output


def _read_summary(path: Path) -> _Summary:
    try:
        _check_size(path)
        raw = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"could not read {_SUMMARY_NAME}: {exc}") from exc
    if not isinstance(raw, Mapping) or raw.get("schema_version") != "1":
        raise ValueError("summary schema_version must be 1")
    data = _object(raw, _SUMMARY_FIELDS, "summary")
    benchmark_name = _string(data["benchmark_name"], "benchmark_name")
    if len(benchmark_name) > 128:
        raise ValueError("benchmark_name must be at most 128 Unicode code points")
    if data["recommendation"] != _RECOMMENDATION:
        raise ValueError(f"summary recommendation must be {_RECOMMENDATION!r}")
    if data["method"] != _METHOD:
        raise ValueError(f"summary method must be {_METHOD!r}")

    provenance = _object(data["provenance"], frozenset(("configuration",)), "provenance")
    configuration = _object(
        provenance["configuration"], frozenset(("sha256",)), "provenance.configuration"
    )
    configuration_sha256 = _sha256(configuration["sha256"], "provenance.configuration.sha256")

    conditions = _object(
        data["conditions"],
        frozenset(
            (
                "dry_air_pressure_hpa",
                "temperature_k",
                "water_vapour_density_g_per_m3",
                "water_vapour_partial_pressure_hpa",
                "water_vapour_density_conversion_constant",
            )
        ),
        "conditions",
    )
    dry_air_pressure_hpa = _number(
        conditions["dry_air_pressure_hpa"], "conditions.dry_air_pressure_hpa"
    )
    temperature_k = _number(conditions["temperature_k"], "conditions.temperature_k")
    density = _number(
        conditions["water_vapour_density_g_per_m3"],
        "conditions.water_vapour_density_g_per_m3",
    )
    partial_pressure = _number(
        conditions["water_vapour_partial_pressure_hpa"],
        "conditions.water_vapour_partial_pressure_hpa",
    )
    if dry_air_pressure_hpa <= 0.0:
        raise ValueError("conditions.dry_air_pressure_hpa must be positive")
    if temperature_k <= 0.0:
        raise ValueError("conditions.temperature_k must be positive")
    if density < 0.0:
        raise ValueError("conditions.water_vapour_density_g_per_m3 must be non-negative")
    if conditions["water_vapour_density_conversion_constant"] != 216.7:
        raise ValueError("conditions.water_vapour_density_conversion_constant must be 216.7")
    if not isclose(
        partial_pressure,
        density * temperature_k / 216.7,
        rel_tol=5e-14,
        abs_tol=1e-13,
    ):
        raise ValueError("conditions.water_vapour_partial_pressure_hpa does not match rho*T/216.7")
    frequencies_hz = _summary_frequencies(data["frequencies_hz"])
    case_count = data["case_count"]
    if (
        not isinstance(case_count, int)
        or isinstance(case_count, bool)
        or not 1 <= case_count <= _MAX_ROWS
    ):
        raise ValueError(f"summary case_count must be between 1 and {_MAX_ROWS}")
    versions = _object(data["versions"], frozenset(("openleo-link",)), "versions")
    openleo_version = _string(versions["openleo-link"], "versions.openleo-link")

    _exact_object(data["coefficient_source"], _COEFFICIENT_SOURCE, "coefficient_source")
    _exact_object(data["official_validation"], _OFFICIAL_VALIDATION, "official_validation")
    _exact_object(data["ordering"], _ORDERING, "ordering")
    _exact_object(data["numeric_format"], _NUMERIC_FORMAT, "numeric_format")
    limitations = data["limitations"]
    if not isinstance(limitations, list) or tuple(limitations) != _LIMITATIONS:
        raise ValueError("summary limitations do not match the schema-v1 contract")

    return _Summary(
        benchmark_name=benchmark_name,
        configuration_sha256=configuration_sha256,
        dry_air_pressure_hpa=dry_air_pressure_hpa,
        temperature_k=temperature_k,
        water_vapour_density_g_per_m3=density,
        frequencies_hz=frequencies_hz,
        case_count=case_count,
        openleo_version=openleo_version,
    )


def _read_rows(path: Path) -> tuple[_Row, ...]:
    try:
        _check_size(path)
        with path.open(encoding="utf-8", newline="") as file:
            reader = csv.DictReader(file)
            if tuple(reader.fieldnames or ()) != _CSV_FIELDS:
                raise ValueError(f"{_CSV_NAME} CSV fields must exactly match schema v1")
            raw_rows = tuple(islice(reader, _MAX_ROWS + 1))
    except (OSError, UnicodeError, csv.Error) as exc:
        raise ValueError(f"could not read {_CSV_NAME}: {exc}") from exc
    if len(raw_rows) > _MAX_ROWS:
        raise ValueError(f"{_CSV_NAME} exceeds {_MAX_ROWS} rows")
    rows = tuple(_parse_row(row) for row in raw_rows)
    if not rows:
        raise ValueError(f"{_CSV_NAME} must contain at least one row")
    if any(left.frequency_hz >= right.frequency_hz for left, right in pairwise(rows)):
        raise ValueError(f"{_CSV_NAME} frequencies must be strictly increasing")
    return rows


def _parse_row(raw: dict[str | None, str | None]) -> _Row:
    if set(raw) != set(_CSV_FIELDS):
        raise ValueError(f"{_CSV_NAME} rows must exactly match the CSV fields")
    frequency_hz = _csv_number(raw["frequency_hz"], "frequency_hz")
    dry_air = _csv_number(
        raw["dry_air_specific_attenuation_db_per_km"],
        "dry_air_specific_attenuation_db_per_km",
    )
    water_vapour = _csv_number(
        raw["water_vapour_specific_attenuation_db_per_km"],
        "water_vapour_specific_attenuation_db_per_km",
    )
    total = _csv_number(
        raw["total_specific_attenuation_db_per_km"],
        "total_specific_attenuation_db_per_km",
    )
    if not 1e9 <= frequency_hz <= 1e12:
        raise ValueError(f"{_CSV_NAME} frequency_hz must be between 1e9 and 1e12 inclusive")
    if min(dry_air, water_vapour, total) < 0.0:
        raise ValueError(f"{_CSV_NAME} attenuation values must be non-negative")
    if not isclose(total, dry_air + water_vapour, rel_tol=1e-12, abs_tol=1e-13):
        raise ValueError(f"{_CSV_NAME} total attenuation must equal dry plus water vapour")
    return _Row(frequency_hz, dry_air, water_vapour, total)


def _csv_number(value: str | None, field: str) -> float:
    try:
        number = float(value) if value is not None else float("nan")
    except ValueError as exc:
        raise ValueError(f"{_CSV_NAME} {field} must be finite") from exc
    if not isfinite(number):
        raise ValueError(f"{_CSV_NAME} {field} must be finite")
    return number


def _check_size(path: Path) -> None:
    if path.stat().st_size > _MAX_ARTIFACT_BYTES:
        raise ValueError(f"{path.name} exceeds {_MAX_ARTIFACT_BYTES} bytes")


def _object(raw: Any, expected: tuple[str, ...] | frozenset[str], path: str) -> Mapping[str, Any]:
    if not isinstance(raw, Mapping):
        raise ValueError(f"summary {path} must be an object")  # noqa: TRY004
    actual = set(raw)
    expected_set = set(expected)
    if actual != expected_set:
        missing = [field for field in expected if field not in actual]
        extra = sorted(actual - expected_set)
        details = []
        if missing:
            details.append(f"missing fields: {', '.join(missing)}")
        if extra:
            details.append(f"unexpected fields: {', '.join(extra)}")
        raise ValueError(f"summary {path} has {'; '.join(details)}")
    return raw


def _exact_object(raw: Any, expected: dict[str, Any], path: str) -> None:
    data = _object(raw, frozenset(expected), path)
    if data != expected:
        raise ValueError(f"summary {path} does not match the schema-v1 contract")


def _string(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"summary {path} must be a non-empty string")
    return value


def _number(value: Any, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value):
        raise ValueError(f"summary {path} must be finite")
    return float(value)


def _summary_frequencies(raw: Any) -> tuple[float, ...]:
    if not isinstance(raw, list) or not 1 <= len(raw) <= _MAX_ROWS:
        raise ValueError(f"summary frequencies_hz must contain 1 to {_MAX_ROWS} values")
    frequencies = tuple(_number(value, "frequencies_hz") for value in raw)
    if any(frequency < 1e9 or frequency > 1e12 for frequency in frequencies):
        raise ValueError("summary frequencies_hz values must be between 1e9 and 1e12")
    if any(left >= right for left, right in pairwise(frequencies)):
        raise ValueError("summary frequencies_hz values must be strictly increasing")
    return frequencies


def _sha256(value: Any, path: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"summary {path} must be 64 lowercase hexadecimal characters")
    return value


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    data: dict[str, Any] = {}
    for key, value in pairs:
        if key in data:
            raise ValueError(f"duplicate JSON key {key!r}")
        data[key] = value
    return data
