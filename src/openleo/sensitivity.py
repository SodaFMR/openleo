"""Strict deterministic one-at-a-time sensitivity study loading."""

from __future__ import annotations

import csv
import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from hashlib import sha256
from importlib.metadata import version
from json import JSONDecodeError
from math import isclose, isfinite
from numbers import Real
from pathlib import Path
from typing import Any

from openleo.input import MAX_TIME_GRID_SAMPLES
from openleo.model import Scenario
from openleo.output import _float, _json_dumps, _utc
from openleo.simulation import SimulationResult, simulate_scenario

MAX_STUDY_BYTES = 1_000_000
MAX_SWEEPS = 10
MAX_VALUES_PER_SWEEP = 20
MAX_DECLARED_CASES = 64
SUPPORTED_PARAMETERS = (
    "time_window.step_s",
    "time_window.minimum_elevation_deg",
    "radio_link.eirp_dbw",
    "radio_link.system_noise_temperature_k",
    "radio_link.miscellaneous_loss_db",
)
PARAMETER_UNITS = {
    "time_window.step_s": "s",
    "time_window.minimum_elevation_deg": "deg",
    "radio_link.eirp_dbw": "dBW",
    "radio_link.system_noise_temperature_k": "K",
    "radio_link.miscellaneous_loss_db": "dB",
}
STUDY_KEYS = frozenset(("schema_version", "name", "method", "sweeps"))
SWEEP_KEYS = frozenset(("parameter", "values"))
SENSITIVITY_CSV_FIELDS = (
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
SENSITIVITY_LIMITATIONS = (
    "deterministic one-at-a-time assumed ranges, not probability distributions",
    "no confidence, credible, coverage, or standard-uncertainty interval",
    "no input correlations or covariance propagation",
    "frozen orbital geometry from public GP elements",
    "synthetic RF assumptions and free-space-only propagation",
    "Shannon-Hartley quantities are theoretical upper bounds, not throughput",
)


@dataclass(frozen=True, slots=True)
class SensitivitySweep:
    parameter: str
    unit: str
    values: tuple[float, ...]


@dataclass(frozen=True, slots=True)
class SensitivityStudy:
    name: str
    method: str
    source_sha256: str
    sweeps: tuple[SensitivitySweep, ...]


@dataclass(frozen=True, slots=True)
class SensitivityMetrics:
    row_count: int
    sampled_aos_utc: datetime
    sampled_los_utc: datetime
    sampled_duration_s: float
    maximum_elevation_deg: float
    minimum_range_m: float
    maximum_carrier_to_noise_density_db_hz: float
    maximum_shannon_capacity_upper_bound_bps: float
    integrated_shannon_capacity_upper_bound_bits: float


@dataclass(frozen=True, slots=True)
class SensitivityBaselineContext:
    element_epoch_utc: datetime
    start_element_age_days: float
    stop_element_age_days: float
    maximum_absolute_element_age_days: float
    leap_second_table_source: str
    leap_second_table_sha256: str
    warnings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SensitivityCase:
    parameter: str
    unit: str
    baseline_value: float
    case_value: float
    is_nominal: bool
    metrics: SensitivityMetrics
    maximum_capacity_upper_bound_delta_bps: float
    maximum_capacity_upper_bound_relative_change_percent: float | None
    integrated_capacity_upper_bound_delta_bits: float
    integrated_capacity_upper_bound_relative_change_percent: float | None


@dataclass(frozen=True, slots=True)
class SensitivityResult:
    scenario: Scenario
    study: SensitivityStudy
    baseline: SensitivityMetrics
    baseline_context: SensitivityBaselineContext
    cases: tuple[SensitivityCase, ...]


def load_sensitivity_study(path: str | Path, scenario: Scenario) -> SensitivityStudy:
    study_path = Path(path)
    try:
        source_bytes = _read_study_bytes(study_path)
        source_sha256 = sha256(source_bytes).hexdigest()
        raw = json.loads(source_bytes.decode("utf-8"))
        return _study(raw, scenario, source_sha256)
    except UnicodeDecodeError as exc:
        raise ValueError(f"{study_path}: sensitivity study JSON must be UTF-8") from exc
    except JSONDecodeError as exc:
        raise ValueError(f"{study_path}: invalid JSON: {exc.msg}") from exc
    except OSError as exc:
        raise ValueError(f"{study_path}: could not load sensitivity study: {exc}") from exc
    except (KeyError, TypeError) as exc:
        raise ValueError(f"{study_path}: invalid sensitivity study: {exc}") from exc
    except ValueError as exc:
        if str(exc).startswith(f"{study_path}:"):
            raise
        raise ValueError(f"{study_path}: invalid sensitivity study: {exc}") from exc


def run_sensitivity(scenario: Scenario, study: SensitivityStudy) -> SensitivityResult:
    _validate_study(study, scenario)
    baseline_result = simulate_scenario(scenario)
    baseline = _metrics(baseline_result)
    cases = tuple(
        _case(scenario, sweep, value, baseline) for sweep in study.sweeps for value in sweep.values
    )
    return SensitivityResult(
        scenario=scenario,
        study=study,
        baseline=baseline,
        baseline_context=_baseline_context(baseline_result),
        cases=cases,
    )


def write_sensitivity_result(
    result: SensitivityResult, output_dir: str | Path
) -> tuple[Path, Path]:
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    csv_path = directory / "sensitivity.csv"
    summary_path = directory / "sensitivity-summary.json"
    with csv_path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=SENSITIVITY_CSV_FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(_sensitivity_csv_row(case) for case in result.cases)
    summary_path.write_text(
        _json_dumps(_sensitivity_summary(result)) + "\n", encoding="utf-8", newline="\n"
    )
    return csv_path, summary_path


def _sensitivity_csv_row(case: SensitivityCase) -> dict[str, str]:
    metrics = case.metrics
    return {
        "parameter": case.parameter,
        "unit": case.unit,
        "baseline_value": _float(case.baseline_value),
        "case_value": _float(case.case_value),
        "is_nominal": str(case.is_nominal).lower(),
        "row_count": str(metrics.row_count),
        "sampled_aos_utc": _utc(metrics.sampled_aos_utc),
        "sampled_los_utc": _utc(metrics.sampled_los_utc),
        "sampled_duration_s": _float(metrics.sampled_duration_s),
        "maximum_elevation_deg": _float(metrics.maximum_elevation_deg),
        "minimum_range_m": _float(metrics.minimum_range_m),
        "maximum_carrier_to_noise_density_db_hz": _float(
            metrics.maximum_carrier_to_noise_density_db_hz
        ),
        "maximum_shannon_capacity_upper_bound_bps": _float(
            metrics.maximum_shannon_capacity_upper_bound_bps
        ),
        "integrated_shannon_capacity_upper_bound_bits": _float(
            metrics.integrated_shannon_capacity_upper_bound_bits
        ),
        "maximum_capacity_upper_bound_delta_bps": _float(
            case.maximum_capacity_upper_bound_delta_bps
        ),
        "maximum_capacity_upper_bound_relative_change_percent": _optional_float(
            case.maximum_capacity_upper_bound_relative_change_percent
        ),
        "integrated_capacity_upper_bound_delta_bits": _float(
            case.integrated_capacity_upper_bound_delta_bits
        ),
        "integrated_capacity_upper_bound_relative_change_percent": _optional_float(
            case.integrated_capacity_upper_bound_relative_change_percent
        ),
    }


def _sensitivity_summary(result: SensitivityResult) -> dict[str, Any]:
    scenario = result.scenario
    return {
        "schema_version": "1",
        "study_name": result.study.name,
        "method": result.study.method,
        "provenance": {
            "scenario": {"sha256": scenario.source_sha256},
            "sensitivity": {"sha256": result.study.source_sha256},
        },
        "sweeps": [
            {"parameter": sweep.parameter, "unit": sweep.unit, "values": list(sweep.values)}
            for sweep in result.study.sweeps
        ],
        "baseline_inputs": {
            "scenario_name": scenario.name,
            "orbit": {
                "source_url": scenario.orbit.provenance.source_url,
                "retrieved_at_utc": _utc(scenario.orbit.provenance.retrieved_at_utc),
                "terms_url": scenario.orbit.provenance.terms_url,
                "sha256": scenario.orbit.provenance.sha256,
            },
            "ground_station": {
                "name": scenario.ground_station.name,
                "latitude_deg": scenario.ground_station.latitude_deg,
                "longitude_deg": scenario.ground_station.longitude_deg,
                "height_m": scenario.ground_station.height_m,
            },
            "time_window": {
                "start_utc": _utc(scenario.time_window.start_utc),
                "stop_utc": _utc(scenario.time_window.stop_utc),
                "step_s": scenario.time_window.step_s,
                "minimum_elevation_deg": scenario.time_window.minimum_elevation_deg,
            },
            "radio_link": {
                "carrier_frequency_hz": scenario.radio_link.carrier_frequency_hz,
                "channel_bandwidth_hz": scenario.radio_link.channel_bandwidth_hz,
                "eirp_dbw": scenario.radio_link.eirp_dbw,
                "receiver_gain_dbi": scenario.radio_link.receiver_gain_dbi,
                "system_noise_temperature_k": scenario.radio_link.system_noise_temperature_k,
                "miscellaneous_loss_db": scenario.radio_link.miscellaneous_loss_db,
            },
        },
        "baseline_context": {
            "element_epoch_utc": _utc(result.baseline_context.element_epoch_utc),
            "start_element_age_days": result.baseline_context.start_element_age_days,
            "stop_element_age_days": result.baseline_context.stop_element_age_days,
            "maximum_absolute_element_age_days": (
                result.baseline_context.maximum_absolute_element_age_days
            ),
            "leap_second_table_source": result.baseline_context.leap_second_table_source,
            "leap_second_table_sha256": result.baseline_context.leap_second_table_sha256,
            "warnings": list(result.baseline_context.warnings),
        },
        "baseline": _metrics_summary(result.baseline),
        "sweep_count": len(result.study.sweeps),
        "case_count": len(result.cases),
        "versions": {
            "openleo-link": version("openleo-link"),
            "skyfield": version("skyfield"),
            "sgp4": version("sgp4"),
        },
        "ordering": {
            "sweeps": "sensitivity JSON file order",
            "cases": "sweep order, then ascending declared value order",
        },
        "numeric_format": {
            "floats": "Python format(value, '.12g')",
            "timestamps": "ISO 8601 UTC with Z",
            "zero_denominator_relative_change": "null in JSON; empty CSV field",
        },
        "limitations": list(SENSITIVITY_LIMITATIONS),
    }


def _metrics_summary(metrics: SensitivityMetrics) -> dict[str, Any]:
    return {
        "row_count": metrics.row_count,
        "sampled_aos_utc": _utc(metrics.sampled_aos_utc),
        "sampled_los_utc": _utc(metrics.sampled_los_utc),
        "sampled_duration_s": metrics.sampled_duration_s,
        "maximum_elevation_deg": metrics.maximum_elevation_deg,
        "minimum_range_m": metrics.minimum_range_m,
        "maximum_carrier_to_noise_density_db_hz": (metrics.maximum_carrier_to_noise_density_db_hz),
        "maximum_shannon_capacity_upper_bound_bps": (
            metrics.maximum_shannon_capacity_upper_bound_bps
        ),
        "integrated_shannon_capacity_upper_bound_bits": (
            metrics.integrated_shannon_capacity_upper_bound_bits
        ),
    }


def _optional_float(value: float | None) -> str:
    return "" if value is None else _float(value)


def _case(
    scenario: Scenario,
    sweep: SensitivitySweep,
    value: float,
    baseline: SensitivityMetrics,
) -> SensitivityCase:
    baseline_value = _baseline(sweep.parameter, scenario)
    is_nominal = value == baseline_value
    try:
        metrics = (
            baseline
            if is_nominal
            else _metrics(simulate_scenario(_scenario_value(scenario, sweep.parameter, value)))
        )
    except ValueError as exc:
        raise ValueError(f"{sweep.parameter}={value}: {exc}") from exc
    maximum_delta = (
        metrics.maximum_shannon_capacity_upper_bound_bps
        - baseline.maximum_shannon_capacity_upper_bound_bps
    )
    integrated_delta = (
        metrics.integrated_shannon_capacity_upper_bound_bits
        - baseline.integrated_shannon_capacity_upper_bound_bits
    )
    return SensitivityCase(
        parameter=sweep.parameter,
        unit=sweep.unit,
        baseline_value=baseline_value,
        case_value=value,
        is_nominal=is_nominal,
        metrics=metrics,
        maximum_capacity_upper_bound_delta_bps=maximum_delta,
        maximum_capacity_upper_bound_relative_change_percent=_relative_change(
            maximum_delta, baseline.maximum_shannon_capacity_upper_bound_bps
        ),
        integrated_capacity_upper_bound_delta_bits=integrated_delta,
        integrated_capacity_upper_bound_relative_change_percent=_relative_change(
            integrated_delta, baseline.integrated_shannon_capacity_upper_bound_bits
        ),
    )


def _metrics(result: SimulationResult) -> SensitivityMetrics:
    summary = result.summary
    return SensitivityMetrics(
        row_count=len(result.rows),
        sampled_aos_utc=summary.sampled_aos_utc,
        sampled_los_utc=summary.sampled_los_utc,
        sampled_duration_s=summary.sampled_duration_s,
        maximum_elevation_deg=summary.maximum_elevation_deg,
        minimum_range_m=summary.minimum_range_m,
        maximum_carrier_to_noise_density_db_hz=max(
            row.carrier_to_noise_density_db_hz for row in result.rows
        ),
        maximum_shannon_capacity_upper_bound_bps=summary.maximum_capacity_upper_bound_bps,
        integrated_shannon_capacity_upper_bound_bits=summary.integrated_capacity_upper_bound_bits,
    )


def _baseline_context(result: SimulationResult) -> SensitivityBaselineContext:
    summary = result.summary
    return SensitivityBaselineContext(
        element_epoch_utc=summary.element_epoch_utc,
        start_element_age_days=summary.start_element_age_days,
        stop_element_age_days=summary.stop_element_age_days,
        maximum_absolute_element_age_days=summary.maximum_absolute_element_age_days,
        leap_second_table_source=summary.leap_second_table_source,
        leap_second_table_sha256=summary.leap_second_table_sha256,
        warnings=summary.warnings,
    )


def _scenario_value(scenario: Scenario, parameter: str, value: float) -> Scenario:
    if parameter == "time_window.step_s":
        return replace(scenario, time_window=replace(scenario.time_window, step_s=value))
    if parameter == "time_window.minimum_elevation_deg":
        return replace(
            scenario,
            time_window=replace(scenario.time_window, minimum_elevation_deg=value),
        )
    if parameter == "radio_link.eirp_dbw":
        return replace(scenario, radio_link=replace(scenario.radio_link, eirp_dbw=value))
    if parameter == "radio_link.system_noise_temperature_k":
        return replace(
            scenario,
            radio_link=replace(scenario.radio_link, system_noise_temperature_k=value),
        )
    if parameter == "radio_link.miscellaneous_loss_db":
        return replace(
            scenario,
            radio_link=replace(scenario.radio_link, miscellaneous_loss_db=value),
        )
    raise ValueError(f"unsupported sensitivity parameter {parameter!r}")


def _relative_change(delta: float, baseline: float) -> float | None:
    return None if baseline == 0.0 else delta / baseline * 100.0


def _read_study_bytes(path: Path) -> bytes:
    with path.open("rb") as study_file:
        source_bytes = study_file.read(MAX_STUDY_BYTES + 1)
    if len(source_bytes) > MAX_STUDY_BYTES:
        raise ValueError(f"{path}: sensitivity study exceeds {MAX_STUDY_BYTES} bytes")
    return source_bytes


def _validate_study(study: SensitivityStudy, scenario: Scenario) -> None:
    if not isinstance(study, SensitivityStudy):
        raise ValueError("study must be a SensitivityStudy")  # noqa: TRY004
    if study.method != "deterministic_one_at_a_time":
        raise ValueError("method must be 'deterministic_one_at_a_time'")
    _name(study.name)
    if (
        not isinstance(study.source_sha256, str)
        or len(study.source_sha256) != 64
        or any(char not in "0123456789abcdef" for char in study.source_sha256)
    ):
        raise ValueError("source_sha256 must be exactly 64 lowercase hexadecimal characters")
    if not isinstance(study.sweeps, tuple):
        raise ValueError("sweeps must be a tuple")  # noqa: TRY004
    _validate_sensitivity_sweeps(study.sweeps, scenario)


def _study(raw: Any, scenario: Scenario, source_sha256: str) -> SensitivityStudy:
    data = _object(raw, STUDY_KEYS, "study")
    if data["schema_version"] != "1":
        raise ValueError("schema_version must be '1'")
    if data["method"] != "deterministic_one_at_a_time":
        raise ValueError("method must be 'deterministic_one_at_a_time'")
    name = _name(data["name"])
    sweeps = _sweeps(data["sweeps"], scenario)
    return SensitivityStudy(
        name=name,
        method="deterministic_one_at_a_time",
        source_sha256=source_sha256,
        sweeps=sweeps,
    )


def _sweeps(raw: Any, scenario: Scenario) -> tuple[SensitivitySweep, ...]:
    if not isinstance(raw, list):
        raise TypeError("sweeps must be a JSON array")
    sweeps = tuple(_sweep(value, index, scenario) for index, value in enumerate(raw))
    _validate_sensitivity_sweeps(sweeps, scenario)
    return sweeps


def _validate_sensitivity_sweeps(sweeps: tuple[SensitivitySweep, ...], scenario: Scenario) -> None:
    if not 1 <= len(sweeps) <= MAX_SWEEPS:
        raise ValueError(f"sweeps must contain between 1 and {MAX_SWEEPS} entries")
    for index, sweep in enumerate(sweeps):
        if not isinstance(sweep, SensitivitySweep):
            raise ValueError(f"sweeps[{index}] must be a SensitivitySweep")  # noqa: TRY004
        if sweep.parameter not in SUPPORTED_PARAMETERS:
            raise ValueError(f"unsupported sensitivity parameter {sweep.parameter!r}")
        expected_unit = PARAMETER_UNITS[sweep.parameter]
        if sweep.unit != expected_unit:
            raise ValueError(f"sweeps[{index}].unit={sweep.unit!r} must be {expected_unit!r}")
        if not isinstance(sweep.values, tuple):
            raise ValueError(f"sweeps[{index}].values must be a tuple")  # noqa: TRY004
        _values(sweep.values, f"sweeps[{index}]", sweep.parameter, scenario)
    parameters = tuple(sweep.parameter for sweep in sweeps)
    if len(set(parameters)) != len(parameters):
        duplicate = next(parameter for parameter in parameters if parameters.count(parameter) > 1)
        raise ValueError(
            f"sweeps[{parameters.index(duplicate)}].parameter={duplicate!r} is duplicated"
        )
    total_cases = sum(len(sweep.values) for sweep in sweeps)
    if total_cases > MAX_DECLARED_CASES:
        raise ValueError(f"sweeps declare {total_cases} cases; limit is {MAX_DECLARED_CASES}")


def _sweep(raw: Any, index: int, scenario: Scenario) -> SensitivitySweep:
    path = f"sweeps[{index}]"
    data = _object(raw, SWEEP_KEYS, path)
    parameter = data["parameter"]
    if parameter not in SUPPORTED_PARAMETERS:
        raise ValueError(f"{path}.parameter={_safe_repr(parameter)} is not supported")
    values = _values(data["values"], path, parameter, scenario)
    return SensitivitySweep(parameter=parameter, unit=PARAMETER_UNITS[parameter], values=values)


def _values(raw: Any, path: str, parameter: str, scenario: Scenario) -> tuple[float, ...]:
    field = f"{path}.values"
    if not isinstance(raw, (list, tuple)):
        raise TypeError(f"{field} must be a JSON array")
    if not 2 <= len(raw) <= MAX_VALUES_PER_SWEEP:
        raise ValueError(f"{field} must contain between 2 and {MAX_VALUES_PER_SWEEP} entries")
    values = tuple(_finite_number(value, f"{field}[{index}]") for index, value in enumerate(raw))
    for index, value in enumerate(values):
        _parameter_value(parameter, value, f"{field}[{index}]", scenario)
        if index and value <= values[index - 1]:
            raise ValueError(f"{field}[{index}] must be strictly greater than the previous value")
    baseline = _baseline(parameter, scenario)
    if values.count(baseline) != 1:
        raise ValueError(f"{field} must contain scenario baseline {baseline!r} exactly once")
    return values


def _parameter_value(parameter: str, value: float, path: str, scenario: Scenario) -> None:
    if parameter == "time_window.step_s":
        if value <= 0.0:
            raise ValueError(f"{path} for time_window.step_s must be positive")
        duration_s = (
            scenario.time_window.stop_utc - scenario.time_window.start_utc
        ).total_seconds()
        if value > duration_s:
            raise ValueError(f"{path} for time_window.step_s must be no larger than {duration_s}")
        step = timedelta(seconds=value)
        represented_value = step.total_seconds()
        if represented_value <= 0.0 or not isclose(
            represented_value, value, rel_tol=0.0, abs_tol=1e-12
        ):
            raise ValueError(
                f"{path} for time_window.step_s must be exactly representable at microsecond resolution"
            )
        quotient, remainder = divmod(
            scenario.time_window.stop_utc - scenario.time_window.start_utc,
            step,
        )
        sample_count = quotient + 1 + bool(remainder)
        if sample_count > MAX_TIME_GRID_SAMPLES:
            raise ValueError(
                f"{path} for time_window.step_s={value!r} produces {sample_count} inclusive "
                f"samples; limit is {MAX_TIME_GRID_SAMPLES}; increase time_window.step_s or "
                "shorten the window"
            )
    elif parameter == "time_window.minimum_elevation_deg":
        if not 0.0 <= value < 90.0:
            raise ValueError(f"{path} for time_window.minimum_elevation_deg must be in [0.0, 90.0)")
    elif parameter == "radio_link.system_noise_temperature_k" and value <= 0.0:
        raise ValueError(f"{path} for radio_link.system_noise_temperature_k must be positive")
    elif parameter == "radio_link.miscellaneous_loss_db" and value < 0.0:
        raise ValueError(f"{path} for radio_link.miscellaneous_loss_db must be non-negative")


def _baseline(parameter: str, scenario: Scenario) -> float:
    if parameter == "time_window.step_s":
        return scenario.time_window.step_s
    if parameter == "time_window.minimum_elevation_deg":
        return scenario.time_window.minimum_elevation_deg
    if parameter == "radio_link.eirp_dbw":
        return scenario.radio_link.eirp_dbw
    if parameter == "radio_link.system_noise_temperature_k":
        return scenario.radio_link.system_noise_temperature_k
    if parameter == "radio_link.miscellaneous_loss_db":
        return scenario.radio_link.miscellaneous_loss_db
    raise ValueError(f"unsupported sensitivity parameter {parameter!r}")


def _object(raw: Any, expected: frozenset[str], path: str) -> Mapping[str, Any]:
    if not isinstance(raw, Mapping):
        raise TypeError(f"{path} must be a JSON object")
    actual = set(raw)
    missing = expected - actual
    unknown = actual - expected
    if missing:
        raise ValueError(f"{_field(path, min(missing))} is required")
    if unknown:
        raise ValueError(f"{_field(path, min(unknown))} is not allowed")
    return raw


def _field(path: str, name: str) -> str:
    return name if path == "study" else f"{path}.{name}"


def _name(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("name must be a non-empty string")
    if len(value) > 128:
        raise ValueError("name must be at most 128 Unicode code points")
    return value


def _finite_number(value: Any, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real) or not isfinite(value):
        raise ValueError(f"{path}={_safe_repr(value)} must be finite")
    return float(value)


def _safe_repr(value: Any, limit: int = 120) -> str:
    text = repr(value)
    return text if len(text) <= limit else f"{text[: limit - 3]}..."
