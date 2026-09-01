"""ITU-R P.676-13 Annex 1 specific gaseous attenuation.

Equations are adapted from https://github.com/inigodelportillo/ITU-Rpy,
``itur/models/itu676.py`` at commit
f739993c4b6d34076de22249ef53d03fa5a53d73, under the MIT license retained in
``THIRD_PARTY_NOTICES.md``.
"""

from __future__ import annotations

import csv
import json
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from importlib.metadata import version
from json import JSONDecodeError
from math import exp, isfinite, sqrt
from numbers import Real
from pathlib import Path
from typing import Any

from openleo._p676_coefficients import OXYGEN_LINES, WATER_VAPOUR_LINES
from openleo.output import _float, _json_dumps

MAX_BENCHMARK_BYTES = 1_000_000
MIN_FREQUENCY_HZ = 1e9
MAX_FREQUENCY_HZ = 1e12
RECOMMENDATION = "ITU-R P.676-13"
METHOD = "annex1_line_by_line_specific_attenuation"
BENCHMARK_KEYS = frozenset(
    (
        "schema_version",
        "name",
        "recommendation",
        "method",
        "dry_air_pressure_hpa",
        "temperature_k",
        "water_vapour_density_g_per_m3",
        "frequencies_hz",
    )
)
GASES_CSV_FIELDS = (
    "frequency_hz",
    "dry_air_specific_attenuation_db_per_km",
    "water_vapour_specific_attenuation_db_per_km",
    "total_specific_attenuation_db_per_km",
)
_GASES_CSV_NAME = "gaseous-specific-attenuation.csv"
GASES_LIMITATIONS = (
    "specific attenuation in dB/km at one declared homogeneous state; not integrated slant-path attenuation",
    "no atmospheric profile, refraction, rain, cloud, fog, scintillation, availability, or propagation-combination model",
    "no live weather, ERA5, radiosonde, or instantaneous local-weather claim",
    "no calibrated RF or operator-performance validation claim",
    "official validation workbook is not redistributed; public results reproduce literal published cases",
)


@dataclass(frozen=True, slots=True)
class SpecificGaseousAttenuation:
    dry_air_db_per_km: float
    water_vapour_db_per_km: float
    total_db_per_km: float


@dataclass(frozen=True, slots=True)
class GasesBenchmark:
    name: str
    recommendation: str
    method: str
    source_sha256: str
    dry_air_pressure_hpa: float
    temperature_k: float
    water_vapour_density_g_per_m3: float
    frequencies_hz: tuple[float, ...]


@dataclass(frozen=True, slots=True)
class GasesCase:
    frequency_hz: float
    attenuation: SpecificGaseousAttenuation


@dataclass(frozen=True, slots=True)
class GasesResult:
    benchmark: GasesBenchmark
    cases: tuple[GasesCase, ...]


def load_gases_benchmark(path: str | Path) -> GasesBenchmark:
    """Load a bounded, strict P.676-13 specific-attenuation benchmark."""
    benchmark_path = Path(path)
    try:
        source_bytes = _read_benchmark_bytes(benchmark_path)
        raw = json.loads(source_bytes.decode("utf-8"), object_pairs_hook=_unique_object)
        return _benchmark(raw, sha256(source_bytes).hexdigest())
    except UnicodeDecodeError as exc:
        raise ValueError(f"{benchmark_path}: benchmark JSON must be UTF-8") from exc
    except JSONDecodeError as exc:
        raise ValueError(f"{benchmark_path}: invalid JSON: {exc.msg}") from exc
    except OSError as exc:
        raise ValueError(f"{benchmark_path}: could not load benchmark: {exc}") from exc
    except (KeyError, TypeError) as exc:
        raise ValueError(f"{benchmark_path}: invalid benchmark: {exc}") from exc
    except ValueError as exc:
        if str(exc).startswith(f"{benchmark_path}:"):
            raise
        raise ValueError(f"{benchmark_path}: invalid benchmark: {exc}") from exc


def run_gases_benchmark(benchmark: GasesBenchmark) -> GasesResult:
    """Calculate the declared ordered specific-attenuation cases."""
    _validate_benchmark(benchmark)
    cases = tuple(
        GasesCase(
            frequency_hz=frequency_hz,
            attenuation=specific_gaseous_attenuation(
                frequency_hz,
                benchmark.dry_air_pressure_hpa,
                benchmark.temperature_k,
                benchmark.water_vapour_density_g_per_m3,
            ),
        )
        for frequency_hz in benchmark.frequencies_hz
    )
    return GasesResult(benchmark=benchmark, cases=cases)


def write_gases_result(result: GasesResult, output_dir: str | Path) -> tuple[Path, Path]:
    """Write deterministic CSV and JSON benchmark artifacts."""
    _validate_result(result)
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    csv_path = directory / _GASES_CSV_NAME
    summary_path = directory / "gaseous-specific-attenuation-summary.json"
    with csv_path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=GASES_CSV_FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(_gases_csv_row(case) for case in result.cases)
    csv_sha256 = sha256(csv_path.read_bytes()).hexdigest()
    summary_path.write_text(
        _json_dumps(_gases_summary(result, csv_sha256), significant_digits=15) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return csv_path, summary_path


def _validate_finite(value: float, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, Real) or not isfinite(value):
        raise ValueError(f"{name} must be finite and not a bool")


def specific_gaseous_attenuation(
    frequency_hz: float,
    dry_air_pressure_hpa: float,
    temperature_k: float,
    water_vapour_density_g_per_m3: float,
) -> SpecificGaseousAttenuation:
    """Return dry-air, water-vapour, and total specific attenuation in dB/km."""
    for value, name in (
        (frequency_hz, "frequency_hz"),
        (dry_air_pressure_hpa, "dry_air_pressure_hpa"),
        (temperature_k, "temperature_k"),
        (water_vapour_density_g_per_m3, "water_vapour_density_g_per_m3"),
    ):
        _validate_finite(value, name)
    if not 1e9 <= frequency_hz <= 1e12:
        raise ValueError("frequency_hz must be between 1e9 and 1e12 inclusive")
    if dry_air_pressure_hpa <= 0.0:
        raise ValueError("dry_air_pressure_hpa must be positive")
    if temperature_k <= 0.0:
        raise ValueError("temperature_k must be positive")
    if water_vapour_density_g_per_m3 < 0.0:
        raise ValueError("water_vapour_density_g_per_m3 must be non-negative")

    f_ghz = frequency_hz / 1e9
    theta = 300.0 / temperature_k
    water_vapour_pressure_hpa = water_vapour_density_g_per_m3 * temperature_k / 216.7
    total_pressure_hpa = dry_air_pressure_hpa + water_vapour_pressure_hpa

    oxygen_refractivity = 0.0
    for f_oxygen, a1, a2, a3, a4, a5, a6 in OXYGEN_LINES:
        width = (
            a3
            * 1e-4
            * (dry_air_pressure_hpa * theta ** (0.8 - a4) + 1.1 * water_vapour_pressure_hpa * theta)
        )
        width = sqrt(width**2 + 2.25e-6)
        line_mixing = (a5 + a6 * theta) * 1e-4 * total_pressure_hpa * theta**0.8
        line_shape = (
            f_ghz
            / f_oxygen
            * (
                (width - line_mixing * (f_oxygen - f_ghz)) / ((f_oxygen - f_ghz) ** 2 + width**2)
                + (width - line_mixing * (f_oxygen + f_ghz)) / ((f_oxygen + f_ghz) ** 2 + width**2)
            )
        )
        line_strength = a1 * 1e-7 * dry_air_pressure_hpa * theta**3 * exp(a2 * (1.0 - theta))
        oxygen_refractivity += line_strength * line_shape

    continuum_width = 5.6e-4 * total_pressure_hpa * theta**0.8
    dry_continuum = (
        f_ghz
        * dry_air_pressure_hpa
        * theta**2
        * (
            6.14e-5 / (continuum_width * (1.0 + (f_ghz / continuum_width) ** 2))
            + 1.4e-12 * dry_air_pressure_hpa * theta**1.5 / (1.0 + 1.9e-5 * f_ghz**1.5)
        )
    )
    dry_air_db_per_km = 0.1820 * f_ghz * (oxygen_refractivity + dry_continuum)

    water_refractivity = 0.0
    for f_water, b1, b2, b3, b4, b5, b6 in WATER_VAPOUR_LINES:
        width = (
            b3
            * 1e-4
            * (dry_air_pressure_hpa * theta**b4 + b5 * water_vapour_pressure_hpa * theta**b6)
        )
        width = 0.535 * width + sqrt(0.217 * width**2 + 2.1316e-12 * f_water**2 / theta)
        line_shape = (
            f_ghz
            / f_water
            * (
                width / ((f_water - f_ghz) ** 2 + width**2)
                + width / ((f_water + f_ghz) ** 2 + width**2)
            )
        )
        line_strength = b1 * 1e-1 * water_vapour_pressure_hpa * theta**3.5 * exp(b2 * (1.0 - theta))
        water_refractivity += line_strength * line_shape

    water_vapour_db_per_km = 0.1820 * f_ghz * water_refractivity
    total_db_per_km = dry_air_db_per_km + water_vapour_db_per_km
    if not all(
        isfinite(value) and value >= 0.0
        for value in (dry_air_db_per_km, water_vapour_db_per_km, total_db_per_km)
    ):
        raise ValueError("P.676-13 calculation produced invalid attenuation")
    return SpecificGaseousAttenuation(
        dry_air_db_per_km,
        water_vapour_db_per_km,
        total_db_per_km,
    )


def _read_benchmark_bytes(path: Path) -> bytes:
    with path.open("rb") as benchmark_file:
        source_bytes = benchmark_file.read(MAX_BENCHMARK_BYTES + 1)
    if len(source_bytes) > MAX_BENCHMARK_BYTES:
        raise ValueError(f"{path}: benchmark exceeds {MAX_BENCHMARK_BYTES} bytes")
    return source_bytes


def _benchmark(raw: Any, source_sha256: str) -> GasesBenchmark:
    data = _object(raw, BENCHMARK_KEYS, "benchmark")
    if data["schema_version"] != "1":
        raise ValueError("schema_version must be '1'")
    if data["recommendation"] != RECOMMENDATION:
        raise ValueError(f"recommendation must be {RECOMMENDATION!r}")
    if data["method"] != METHOD:
        raise ValueError(f"method must be {METHOD!r}")
    benchmark = GasesBenchmark(
        name=_name(data["name"]),
        recommendation=RECOMMENDATION,
        method=METHOD,
        source_sha256=source_sha256,
        dry_air_pressure_hpa=_finite_number(data["dry_air_pressure_hpa"], "dry_air_pressure_hpa"),
        temperature_k=_finite_number(data["temperature_k"], "temperature_k"),
        water_vapour_density_g_per_m3=_finite_number(
            data["water_vapour_density_g_per_m3"], "water_vapour_density_g_per_m3"
        ),
        frequencies_hz=_frequencies(data["frequencies_hz"]),
    )
    _validate_benchmark(benchmark)
    return benchmark


def _validate_benchmark(benchmark: GasesBenchmark) -> None:
    if not isinstance(benchmark, GasesBenchmark):
        raise ValueError("benchmark must be a GasesBenchmark")  # noqa: TRY004
    if benchmark.recommendation != RECOMMENDATION:
        raise ValueError(f"recommendation must be {RECOMMENDATION!r}")
    if benchmark.method != METHOD:
        raise ValueError(f"method must be {METHOD!r}")
    _name(benchmark.name)
    if (
        not isinstance(benchmark.source_sha256, str)
        or len(benchmark.source_sha256) != 64
        or any(char not in "0123456789abcdef" for char in benchmark.source_sha256)
    ):
        raise ValueError("source_sha256 must be exactly 64 lowercase hexadecimal characters")
    _validate_finite(benchmark.dry_air_pressure_hpa, "dry_air_pressure_hpa")
    _validate_finite(benchmark.temperature_k, "temperature_k")
    _validate_finite(benchmark.water_vapour_density_g_per_m3, "water_vapour_density_g_per_m3")
    if benchmark.dry_air_pressure_hpa <= 0.0:
        raise ValueError("dry_air_pressure_hpa must be positive")
    if benchmark.temperature_k <= 0.0:
        raise ValueError("temperature_k must be positive")
    if benchmark.water_vapour_density_g_per_m3 < 0.0:
        raise ValueError("water_vapour_density_g_per_m3 must be non-negative")
    if not isinstance(benchmark.frequencies_hz, tuple):
        raise ValueError("frequencies_hz must be a tuple")  # noqa: TRY004
    _frequencies(benchmark.frequencies_hz)


def _frequencies(raw: Any) -> tuple[float, ...]:
    if not isinstance(raw, (list, tuple)):
        raise TypeError("frequencies_hz must be a JSON array")
    if not 1 <= len(raw) <= 1000:
        raise ValueError("frequencies_hz must contain between 1 and 1000 entries")
    frequencies = tuple(
        _finite_number(frequency, f"frequencies_hz[{index}]") for index, frequency in enumerate(raw)
    )
    for index, frequency_hz in enumerate(frequencies):
        if not MIN_FREQUENCY_HZ <= frequency_hz <= MAX_FREQUENCY_HZ:
            raise ValueError(
                f"frequencies_hz[{index}] frequency_hz must be between 1e9 and 1e12 inclusive"
            )
        if index and frequency_hz <= frequencies[index - 1]:
            raise ValueError(
                f"frequencies_hz[{index}] must be strictly greater than the previous value"
            )
    return frequencies


def _validate_result(result: GasesResult) -> None:
    if not isinstance(result, GasesResult):
        raise ValueError("result must be a GasesResult")  # noqa: TRY004
    _validate_benchmark(result.benchmark)
    if not isinstance(result.cases, tuple):
        raise ValueError("cases must be a tuple")  # noqa: TRY004
    if len(result.cases) != len(result.benchmark.frequencies_hz):
        raise ValueError("cases must match the declared frequencies")
    for frequency_hz, case in zip(result.benchmark.frequencies_hz, result.cases, strict=True):
        if not isinstance(case, GasesCase):
            raise ValueError("cases must contain GasesCase values")  # noqa: TRY004
        if case.frequency_hz != frequency_hz:
            raise ValueError("case frequency_hz must match the declared frequency")
        if not isinstance(case.attenuation, SpecificGaseousAttenuation):
            raise ValueError("case attenuation must be SpecificGaseousAttenuation")  # noqa: TRY004
        attenuation = case.attenuation
        for value, name in (
            (attenuation.dry_air_db_per_km, "dry_air_db_per_km"),
            (attenuation.water_vapour_db_per_km, "water_vapour_db_per_km"),
            (attenuation.total_db_per_km, "total_db_per_km"),
        ):
            _validate_finite(value, name)
            if value < 0.0:
                raise ValueError(f"{name} must be non-negative")
        if attenuation.total_db_per_km != (
            attenuation.dry_air_db_per_km + attenuation.water_vapour_db_per_km
        ):
            raise ValueError(
                "total_db_per_km must equal dry_air_db_per_km plus water_vapour_db_per_km"
            )


def _gases_csv_row(case: GasesCase) -> dict[str, str]:
    attenuation = case.attenuation
    return {
        "frequency_hz": _float(case.frequency_hz, significant_digits=15),
        "dry_air_specific_attenuation_db_per_km": _float(
            attenuation.dry_air_db_per_km, significant_digits=15
        ),
        "water_vapour_specific_attenuation_db_per_km": _float(
            attenuation.water_vapour_db_per_km, significant_digits=15
        ),
        "total_specific_attenuation_db_per_km": _float(
            attenuation.total_db_per_km, significant_digits=15
        ),
    }


def _gases_summary(result: GasesResult, csv_sha256: str) -> dict[str, Any]:
    benchmark = result.benchmark
    water_vapour_partial_pressure_hpa = (
        benchmark.water_vapour_density_g_per_m3 * benchmark.temperature_k / 216.7
    )
    return {
        "schema_version": "1",
        "artifacts": {
            "csv": {
                "filename": _GASES_CSV_NAME,
                "sha256": csv_sha256,
            }
        },
        "benchmark_name": benchmark.name,
        "recommendation": benchmark.recommendation,
        "method": benchmark.method,
        "provenance": {"configuration": {"sha256": benchmark.source_sha256}},
        "conditions": {
            "dry_air_pressure_hpa": benchmark.dry_air_pressure_hpa,
            "temperature_k": benchmark.temperature_k,
            "water_vapour_density_g_per_m3": benchmark.water_vapour_density_g_per_m3,
            "water_vapour_partial_pressure_hpa": water_vapour_partial_pressure_hpa,
            "water_vapour_density_conversion_constant": 216.7,
        },
        "case_count": len(result.cases),
        "frequencies_hz": [case.frequency_hz for case in result.cases],
        "versions": {"openleo-link": version("openleo-link")},
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
            "recommendation": RECOMMENDATION,
            "workbook": {
                "url": (
                    "https://www.itu.int/en/ITU-R/study-groups/rsg3/rwp3m/Validation%20Example/"
                    "CG-3M3J-13-ValEx-Rev8.3.0.xlsx"
                ),
                "sha256": "e2d8d864c80f59752318548cdd75d818792b44574da6e41dbdc5cb722aab7546",
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
        "limitations": list(GASES_LIMITATIONS),
    }


def _object(raw: Any, expected: frozenset[str], path: str) -> Mapping[str, Any]:
    if not isinstance(raw, Mapping):
        raise TypeError(f"{path} must be a JSON object")
    actual = set(raw)
    missing = expected - actual
    unknown = actual - expected
    if missing:
        raise ValueError(f"{min(missing)} is required")
    if unknown:
        raise ValueError(f"{min(unknown)} is not allowed")
    return raw


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    data: dict[str, Any] = {}
    for key, value in pairs:
        if key in data:
            raise ValueError(f"duplicate JSON key {key!r}")
        data[key] = value
    return data


def _name(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("name must be a non-empty string")
    if len(value) > 128:
        raise ValueError("name must be at most 128 Unicode code points")
    return value


def _finite_number(value: Any, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real) or not isfinite(value):
        raise ValueError(f"{path} must be finite")
    return float(value)
