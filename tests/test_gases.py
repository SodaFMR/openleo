import json
from dataclasses import FrozenInstanceError, replace
from hashlib import sha256
from math import isclose
from pathlib import Path

import pytest

from openleo.gases import (
    GasesBenchmark,
    load_gases_benchmark,
    run_gases_benchmark,
    specific_gaseous_attenuation,
    write_gases_result,
)

CANONICAL_BENCHMARK = Path("examples/atmosphere/p676_13_validation.json")
CANONICAL_BENCHMARK_SHA256 = "8bb73d2f3b3fe971cee54fa5feeeb4e041e2e77a3f272dc83842614d996e7bc0"


def test_load_gases_benchmark_accepts_the_frozen_official_validation_config() -> None:
    benchmark = load_gases_benchmark(CANONICAL_BENCHMARK)

    assert benchmark == GasesBenchmark(
        name="itu-p676-13-specific-attenuation-validation",
        recommendation="ITU-R P.676-13",
        method="annex1_line_by_line_specific_attenuation",
        source_sha256=CANONICAL_BENCHMARK_SHA256,
        dry_air_pressure_hpa=1013.25,
        temperature_k=288.15,
        water_vapour_density_g_per_m3=7.5,
        frequencies_hz=(12e9, 20e9, 60e9, 90e9, 130e9),
    )
    assert sha256(CANONICAL_BENCHMARK.read_bytes()).hexdigest() == CANONICAL_BENCHMARK_SHA256


@pytest.mark.parametrize(
    ("contents", "message"),
    [
        ("{", "invalid JSON"),
        (
            """{
                \"schema_version\": \"1\",
                \"name\": \"discarded\",
                \"name\": \"valid\",
                \"recommendation\": \"ITU-R P.676-13\",
                \"method\": \"annex1_line_by_line_specific_attenuation\",
                \"dry_air_pressure_hpa\": 1013.25,
                \"temperature_k\": 288.15,
                \"water_vapour_density_g_per_m3\": 7.5,
                \"frequencies_hz\": [12000000000.0]
            }""",
            "duplicate JSON key",
        ),
        ('{"schema_version": "1"}', "is required"),
        (
            json.dumps(
                {
                    "schema_version": "1",
                    "name": "valid",
                    "recommendation": "ITU-R P.676-13",
                    "method": "annex1_line_by_line_specific_attenuation",
                    "dry_air_pressure_hpa": 1013.25,
                    "temperature_k": 288.15,
                    "water_vapour_density_g_per_m3": 7.5,
                    "frequencies_hz": [12e9],
                    "extra": True,
                }
            ),
            "is not allowed",
        ),
        (
            json.dumps(
                {
                    "schema_version": "1",
                    "name": "valid",
                    "recommendation": "wrong",
                    "method": "annex1_line_by_line_specific_attenuation",
                    "dry_air_pressure_hpa": 1013.25,
                    "temperature_k": 288.15,
                    "water_vapour_density_g_per_m3": 7.5,
                    "frequencies_hz": [12e9],
                }
            ),
            "recommendation",
        ),
        (
            json.dumps(
                {
                    "schema_version": "1",
                    "name": "valid",
                    "recommendation": "ITU-R P.676-13",
                    "method": "wrong",
                    "dry_air_pressure_hpa": 1013.25,
                    "temperature_k": 288.15,
                    "water_vapour_density_g_per_m3": 7.5,
                    "frequencies_hz": [12e9],
                }
            ),
            "method",
        ),
        (
            json.dumps(
                {
                    "schema_version": "1",
                    "name": "valid",
                    "recommendation": "ITU-R P.676-13",
                    "method": "annex1_line_by_line_specific_attenuation",
                    "dry_air_pressure_hpa": 1013.25,
                    "temperature_k": 288.15,
                    "water_vapour_density_g_per_m3": 7.5,
                    "frequencies_hz": [20e9, 12e9],
                }
            ),
            "strictly greater",
        ),
        (
            json.dumps(
                {
                    "schema_version": "1",
                    "name": "valid",
                    "recommendation": "ITU-R P.676-13",
                    "method": "annex1_line_by_line_specific_attenuation",
                    "dry_air_pressure_hpa": 1013.25,
                    "temperature_k": 288.15,
                    "water_vapour_density_g_per_m3": 7.5,
                    "frequencies_hz": [1e9 - 1],
                }
            ),
            "frequency_hz",
        ),
        (
            json.dumps(
                {
                    "schema_version": "1",
                    "name": "valid",
                    "recommendation": "ITU-R P.676-13",
                    "method": "annex1_line_by_line_specific_attenuation",
                    "dry_air_pressure_hpa": 1013.25,
                    "temperature_k": 288.15,
                    "water_vapour_density_g_per_m3": 7.5,
                    "frequencies_hz": [],
                }
            ),
            "between 1 and 1000",
        ),
    ],
)
def test_load_gases_benchmark_rejects_invalid_configurations(tmp_path, contents, message) -> None:
    path = tmp_path / "benchmark.json"
    path.write_text(contents, encoding="utf-8", newline="\n")

    with pytest.raises(ValueError, match=message):
        load_gases_benchmark(path)


def test_load_gases_benchmark_rejects_too_large_config(tmp_path) -> None:
    path = tmp_path / "benchmark.json"
    path.write_bytes(b" " * 1_000_001)

    with pytest.raises(ValueError, match="exceeds 1000000 bytes"):
        load_gases_benchmark(path)


def test_run_and_write_gases_benchmark_are_immutable_and_deterministic(tmp_path) -> None:
    result = run_gases_benchmark(load_gases_benchmark(CANONICAL_BENCHMARK))

    with pytest.raises(FrozenInstanceError):
        result.cases[0].frequency_hz = 0.0
    with pytest.raises(FrozenInstanceError):
        result.benchmark.name = "changed"

    first_csv, first_summary = write_gases_result(result, tmp_path / "first")
    second_csv, second_summary = write_gases_result(result, tmp_path / "second")

    assert first_csv.read_bytes() == second_csv.read_bytes()
    assert first_summary.read_bytes() == second_summary.read_bytes()
    assert b"\r" not in first_csv.read_bytes()
    assert first_csv.read_text(encoding="utf-8").splitlines()[0] == (
        "frequency_hz,dry_air_specific_attenuation_db_per_km,"
        "water_vapour_specific_attenuation_db_per_km,"
        "total_specific_attenuation_db_per_km"
    )
    assert first_csv.read_text(encoding="utf-8").splitlines()[1] == (
        "12000000000,0.00869826406877357,0.00953538822024593,0.0182336522890195"
    )
    summary = json.loads(first_summary.read_text(encoding="utf-8"))
    assert set(summary) == {
        "artifacts",
        "benchmark_name",
        "case_count",
        "coefficient_source",
        "conditions",
        "frequencies_hz",
        "limitations",
        "method",
        "numeric_format",
        "official_validation",
        "ordering",
        "provenance",
        "recommendation",
        "schema_version",
        "versions",
    }
    assert summary["artifacts"] == {
        "csv": {
            "filename": "gaseous-specific-attenuation.csv",
            "sha256": sha256(first_csv.read_bytes()).hexdigest(),
        }
    }
    assert set(summary["conditions"]) == {
        "dry_air_pressure_hpa",
        "temperature_k",
        "water_vapour_density_conversion_constant",
        "water_vapour_density_g_per_m3",
        "water_vapour_partial_pressure_hpa",
    }
    assert summary["conditions"]["water_vapour_partial_pressure_hpa"] == 9.97288878634056
    assert summary["conditions"]["water_vapour_density_conversion_constant"] == 216.7
    assert summary["numeric_format"]["floats"] == "Python format(value, '.15g')"
    assert summary["case_count"] == 5
    assert summary["frequencies_hz"] == [
        12000000000.0,
        20000000000.0,
        60000000000.0,
        90000000000.0,
        130000000000.0,
    ]
    assert summary["provenance"]["configuration"] == {"sha256": CANONICAL_BENCHMARK_SHA256}
    assert summary["official_validation"]["workbook"]["sha256"] == (
        "e2d8d864c80f59752318548cdd75d818792b44574da6e41dbdc5cb722aab7546"
    )
    assert summary["coefficient_source"]["commit"] == "f739993c4b6d34076de22249ef53d03fa5a53d73"


def test_write_gases_result_preserves_zero_water_vapour_attenuation(tmp_path) -> None:
    benchmark = replace(
        load_gases_benchmark(CANONICAL_BENCHMARK),
        water_vapour_density_g_per_m3=0.0,
        frequencies_hz=(12e9,),
    )

    csv_path, summary_path = write_gases_result(
        run_gases_benchmark(benchmark), tmp_path / "zero-water"
    )

    assert csv_path.read_text(encoding="utf-8").splitlines()[1].split(",")[2] == "0"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary["conditions"]["water_vapour_density_g_per_m3"] == 0.0
    assert summary["artifacts"]["csv"]["sha256"] == sha256(csv_path.read_bytes()).hexdigest()


def test_run_gases_benchmark_rejects_invalid_programmatic_benchmark() -> None:
    benchmark = load_gases_benchmark(CANONICAL_BENCHMARK)

    with pytest.raises(ValueError, match="method"):
        run_gases_benchmark(replace(benchmark, method="wrong"))


@pytest.mark.parametrize(
    ("frequency_hz", "expected_dry", "expected_wet", "expected_total"),
    [
        (12e9, 0.00869826406877357, 0.00953538822024593, 0.0182336522890195),
        (20e9, 0.0118835504778076, 0.0970473048151117, 0.108930855292919),
        (60e9, 14.6234747964861, 0.154841840636247, 14.7783166371223),
        (90e9, 0.0388697110724235, 0.341973394422181, 0.380843105494605),
        (130e9, 0.0415090835995228, 0.751844703646129, 0.793353787245652),
    ],
)
def test_specific_gaseous_attenuation_matches_official_p676_13_cases(
    frequency_hz: float,
    expected_dry: float,
    expected_wet: float,
    expected_total: float,
) -> None:
    attenuation = specific_gaseous_attenuation(frequency_hz, 1013.25, 288.15, 7.5)

    assert isclose(
        attenuation.dry_air_db_per_km,
        expected_dry,
        rel_tol=1e-12,
        abs_tol=1e-13,
    )
    assert isclose(
        attenuation.water_vapour_db_per_km,
        expected_wet,
        rel_tol=1e-12,
        abs_tol=1e-13,
    )
    assert isclose(
        attenuation.total_db_per_km,
        expected_total,
        rel_tol=1e-12,
        abs_tol=1e-13,
    )
    assert (
        attenuation.total_db_per_km
        == attenuation.dry_air_db_per_km + attenuation.water_vapour_db_per_km
    )


@pytest.mark.parametrize("frequency_hz", [1e9, 1e12])
def test_specific_gaseous_attenuation_accepts_frequency_boundaries(frequency_hz: float) -> None:
    attenuation = specific_gaseous_attenuation(frequency_hz, 1013.25, 288.15, 7.5)

    assert attenuation.dry_air_db_per_km >= 0.0
    assert attenuation.water_vapour_db_per_km >= 0.0
    assert (
        attenuation.total_db_per_km
        == attenuation.dry_air_db_per_km + attenuation.water_vapour_db_per_km
    )


def test_specific_gaseous_attenuation_accepts_dry_air() -> None:
    attenuation = specific_gaseous_attenuation(12e9, 1013.25, 288.15, 0.0)

    assert attenuation.water_vapour_db_per_km == 0.0
    assert attenuation.total_db_per_km == attenuation.dry_air_db_per_km


@pytest.mark.parametrize(
    ("args", "field"),
    [
        ((1e9 - 1.0, 1013.25, 288.15, 7.5), "frequency_hz"),
        ((1e12 + 1.0, 1013.25, 288.15, 7.5), "frequency_hz"),
        ((float("nan"), 1013.25, 288.15, 7.5), "frequency_hz"),
        ((float("inf"), 1013.25, 288.15, 7.5), "frequency_hz"),
        ((12e9, 0.0, 288.15, 7.5), "dry_air_pressure_hpa"),
        ((12e9, -1.0, 288.15, 7.5), "dry_air_pressure_hpa"),
        ((12e9, float("nan"), 288.15, 7.5), "dry_air_pressure_hpa"),
        ((12e9, 1013.25, 0.0, 7.5), "temperature_k"),
        ((12e9, 1013.25, -1.0, 7.5), "temperature_k"),
        ((12e9, 1013.25, float("inf"), 7.5), "temperature_k"),
        ((12e9, 1013.25, 288.15, -1.0), "water_vapour_density_g_per_m3"),
        ((12e9, 1013.25, 288.15, float("nan")), "water_vapour_density_g_per_m3"),
        ((True, 1013.25, 288.15, 7.5), "frequency_hz"),
        ((12e9, True, 288.15, 7.5), "dry_air_pressure_hpa"),
        ((12e9, 1013.25, True, 7.5), "temperature_k"),
        ((12e9, 1013.25, 288.15, False), "water_vapour_density_g_per_m3"),
    ],
)
def test_specific_gaseous_attenuation_rejects_invalid_domains(
    args: tuple[float, float, float, float],
    field: str,
) -> None:
    with pytest.raises(ValueError, match=field):
        specific_gaseous_attenuation(*args)
