"""Static rendering of completed deterministic sensitivity artifacts."""

from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from itertools import islice, pairwise
from math import isclose, isfinite
from pathlib import Path
from typing import Any

_FIELDS = (
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
_TOP_LEVEL_FIELDS = frozenset(
    (
        "schema_version",
        "study_name",
        "method",
        "provenance",
        "sweeps",
        "baseline_inputs",
        "baseline_context",
        "baseline",
        "sweep_count",
        "case_count",
        "versions",
        "ordering",
        "numeric_format",
        "limitations",
    )
)
_METRIC_FIELDS = frozenset(
    (
        "row_count",
        "sampled_aos_utc",
        "sampled_los_utc",
        "sampled_duration_s",
        "maximum_elevation_deg",
        "minimum_range_m",
        "maximum_carrier_to_noise_density_db_hz",
        "maximum_shannon_capacity_upper_bound_bps",
        "integrated_shannon_capacity_upper_bound_bits",
    )
)
_PARAMETER_UNITS = {
    "time_window.step_s": "s",
    "time_window.minimum_elevation_deg": "deg",
    "radio_link.eirp_dbw": "dBW",
    "radio_link.system_noise_temperature_k": "K",
    "radio_link.miscellaneous_loss_db": "dB",
}
_RF_PARAMETERS = frozenset(
    (
        "radio_link.eirp_dbw",
        "radio_link.system_noise_temperature_k",
        "radio_link.miscellaneous_loss_db",
    )
)
_RF_LABELS = {
    "radio_link.eirp_dbw": "EIRP",
    "radio_link.system_noise_temperature_k": "Noise temp.",
    "radio_link.miscellaneous_loss_db": "Misc. loss",
}
_ORDERING = {
    "sweeps": "sensitivity JSON file order",
    "cases": "sweep order, then ascending declared value order",
}
_NUMERIC_FORMAT = {
    "floats": "Python format(value, '.12g')",
    "timestamps": "ISO 8601 UTC with Z",
    "zero_denominator_relative_change": "null in JSON; empty CSV field",
}
_LIMITATIONS = (
    "deterministic one-at-a-time assumed ranges, not probability distributions",
    "no confidence, credible, coverage, or standard-uncertainty interval",
    "no input correlations or covariance propagation",
    "frozen orbital geometry from public GP elements",
    "synthetic RF assumptions and free-space-only propagation",
    "Shannon-Hartley quantities are theoretical upper bounds, not throughput",
)
_FOOTER = (
    "Deterministic OAT assumed ranges; free-space synthetic RF; no probability interval; "
    "Shannon-Hartley upper bound, not throughput."
)
_NON_RANKING = "Heterogeneous RF assumed spans are not an importance ranking."
_MAX_SUMMARY_BYTES = 10_000_000
_MAX_CSV_BYTES = 1_000_000
_MAX_ROWS = 64


@dataclass(frozen=True)
class _Metrics:
    row_count: int
    sampled_aos_utc: datetime
    sampled_los_utc: datetime
    sampled_duration_s: float
    maximum_elevation_deg: float
    minimum_range_m: float
    maximum_carrier_to_noise_density_db_hz: float
    maximum_shannon_capacity_upper_bound_bps: float
    integrated_shannon_capacity_upper_bound_bits: float


@dataclass(frozen=True)
class _Row:
    parameter: str
    unit: str
    baseline_value: float
    case_value: float
    is_nominal: bool
    metrics: _Metrics
    maximum_delta_bps: float
    maximum_relative_percent: float | None
    integrated_delta_bits: float
    integrated_relative_percent: float | None


@dataclass(frozen=True)
class _Sweep:
    parameter: str
    unit: str
    values: tuple[float, ...]


@dataclass(frozen=True)
class _Summary:
    study_name: str
    scenario_hash: str
    sensitivity_hash: str
    openleo_version: str
    sweeps: tuple[_Sweep, ...]
    baselines: dict[str, float]
    baseline: _Metrics
    sweep_count: int
    case_count: int


def render_sensitivity_overview(run_directory: str | Path, output_path: str | Path) -> Path:
    """Render completed schema-v1 sensitivity artifacts as SVG or PNG."""
    run = Path(run_directory)
    output = Path(output_path)
    suffix = output.suffix.lower()
    if suffix not in {".svg", ".png"}:
        raise ValueError("output_path must end in .svg or .png")

    summary = _read_summary(run / "sensitivity-summary.json")
    rows = _read_rows(run / "sensitivity.csv")
    _validate_consistency(summary, rows)
    grouped = {
        sweep.parameter: tuple(row for row in rows if row.parameter == sweep.parameter)
        for sweep in summary.sweeps
    }
    sampling = grouped["time_window.step_s"]
    elevation = grouped["time_window.minimum_elevation_deg"]
    rf = tuple(
        (sweep, grouped[sweep.parameter])
        for sweep in summary.sweeps
        if sweep.parameter in _RF_PARAMETERS
    )
    if any(
        row.integrated_relative_percent is None
        for row in (*elevation, *(row for _, values in rf for row in values))
    ):
        raise ValueError("sensitivity.csv relative changes are required for sensitivity plotting")

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

    figure = plt.figure(figsize=(15, 8.5), facecolor="white", layout="none")
    try:
        grid = figure.add_gridspec(
            2,
            2,
            left=0.075,
            right=0.975,
            bottom=0.155,
            top=0.865,
            wspace=0.34,
            hspace=0.52,
            height_ratios=(1.0, 1.15),
        )
        sampling_axis = figure.add_subplot(grid[0, 0])
        elevation_axis = figure.add_subplot(grid[0, 1])
        rf_axis = figure.add_subplot(grid[1, :])
        reference = min(sampling, key=lambda row: row.case_value)
        sampling_axis.plot(
            tuple(row.case_value for row in sampling),
            tuple(
                (
                    row.metrics.integrated_shannon_capacity_upper_bound_bits
                    - reference.metrics.integrated_shannon_capacity_upper_bound_bits
                )
                / 1_000_000.0
                for row in sampling
            ),
            marker="o",
            color="#0072B2",
            linewidth=2.0,
        )
        sampling_axis.axhline(0.0, color="#666666", linewidth=1.0)
        sampling_axis.set_title(
            f"A  Sampling step ({reference.case_value:g} s numerical reference, not truth)",
            fontsize=13,
        )
        sampling_axis.set_xlabel("Sampling interval (s)", fontsize=11)
        sampling_axis.set_ylabel("Integrated-bound difference from reference (Mbit)", fontsize=11)
        sampling_axis.grid(alpha=0.3)

        elevation_axis.plot(
            tuple(row.case_value for row in elevation),
            tuple(float(row.integrated_relative_percent) for row in elevation),
            marker="o",
            color="#D55E00",
            linewidth=2.0,
        )
        elevation_axis.axhline(0.0, color="#666666", linewidth=1.0)
        elevation_axis.set_title(
            f"B  Elevation mask (nominal {summary.baselines['time_window.minimum_elevation_deg']:g}° mask)",
            fontsize=13,
        )
        elevation_axis.set_xlabel("Minimum elevation (deg)", fontsize=11)
        elevation_axis.set_ylabel("Integrated-bound change from nominal (%)", fontsize=11)
        elevation_axis.grid(alpha=0.3)

        for index, (sweep, values) in enumerate(rf):
            changes = tuple(float(row.integrated_relative_percent) for row in values)
            rf_axis.hlines(index, min(changes), max(changes), color="#009E73", linewidth=3.0)
            rf_axis.scatter(changes, (index,) * len(changes), color="#009E73", s=55)
            top_row = index == len(rf) - 1
            for row, change in zip(values, changes, strict=True):
                rf_axis.annotate(
                    f"{row.case_value:g} {sweep.unit}",
                    (change, index),
                    xytext=(0, -10 if top_row else 8),
                    textcoords="offset points",
                    ha="center",
                    va="top" if top_row else "baseline",
                    fontsize=9,
                )
        rf_axis.axvline(0.0, color="#666666", linewidth=1.0)
        rf_axis.set_yticks(range(len(rf)), tuple(_RF_LABELS[sweep.parameter] for sweep, _ in rf))
        rf_axis.set_xlabel("Integrated-bound change from nominal (%)", fontsize=11)
        rf_axis.set_title("C  RF assumption sweeps", fontsize=13)
        rf_axis.grid(axis="x", alpha=0.3)
        for axis in figure.axes:
            axis.tick_params(labelsize=10)

        figure.suptitle(summary.study_name, fontsize=16)
        figure.text(0.5, 0.06, _NON_RANKING, ha="center", fontsize=10)
        figure.text(0.5, 0.018, _FOOTER, ha="center", fontsize=10)
        output.parent.mkdir(parents=True, exist_ok=True)
        metadata = {
            "Title": summary.study_name,
            "Description": (
                f"scenario_sha256={summary.scenario_hash}; "
                f"sensitivity_sha256={summary.sensitivity_hash}; "
                f"openleo={summary.openleo_version}; matplotlib={matplotlib.__version__}"
            ),
            "Creator": "OpenLEO",
            "Date": None,
        }
        with matplotlib.rc_context(
            {"svg.fonttype": "none", "svg.hashsalt": "openleo-sensitivity-overview"}
        ):
            figure.savefig(output, dpi=200 if suffix == ".png" else None, metadata=metadata)
    finally:
        plt.close(figure)
    return output


def _read_summary(path: Path) -> _Summary:
    try:
        _check_artifact_size(path, _MAX_SUMMARY_BYTES)
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"could not read sensitivity-summary.json: {exc}") from exc
    summary = _exact_object(value, _TOP_LEVEL_FIELDS, "sensitivity-summary.json")
    if summary["schema_version"] != "1":
        raise ValueError("sensitivity-summary.json schema_version must be 1")
    if summary["method"] != "deterministic_one_at_a_time":
        raise ValueError("sensitivity-summary.json method must be deterministic_one_at_a_time")
    study_name = _string(summary["study_name"], "study_name")
    provenance = _exact_object(
        summary["provenance"], frozenset(("scenario", "sensitivity")), "provenance"
    )
    scenario_hash = _hash_object(provenance["scenario"], "provenance.scenario")
    sensitivity_hash = _hash_object(provenance["sensitivity"], "provenance.sensitivity")
    versions = _exact_object(
        summary["versions"], frozenset(("openleo-link", "skyfield", "sgp4")), "versions"
    )
    for name, value in versions.items():
        _string(value, f"versions.{name}")
    if summary["ordering"] != _ORDERING:
        raise ValueError("sensitivity-summary.json ordering does not match schema 1")
    if summary["numeric_format"] != _NUMERIC_FORMAT:
        raise ValueError("sensitivity-summary.json numeric_format does not match schema 1")
    if summary["limitations"] != list(_LIMITATIONS):
        raise ValueError("sensitivity-summary.json limitations do not match schema 1")
    baselines = _baseline_inputs(summary["baseline_inputs"])
    _baseline_context(summary["baseline_context"])
    baseline = _parse_metrics(summary["baseline"], "sensitivity-summary.json baseline")
    sweeps = _sweeps(summary["sweeps"], baselines)
    sweep_count = _positive_int(summary["sweep_count"], "sweep_count")
    case_count = _positive_int(summary["case_count"], "case_count")
    if sweep_count != len(sweeps):
        raise ValueError("sensitivity-summary.json sweep_count does not match sweeps")
    if case_count != sum(len(sweep.values) for sweep in sweeps):
        raise ValueError("sensitivity-summary.json case_count does not match sweeps")
    return _Summary(
        study_name=study_name,
        scenario_hash=scenario_hash,
        sensitivity_hash=sensitivity_hash,
        openleo_version=str(versions["openleo-link"]),
        sweeps=sweeps,
        baselines=baselines,
        baseline=baseline,
        sweep_count=sweep_count,
        case_count=case_count,
    )


def _baseline_inputs(value: Any) -> dict[str, float]:
    inputs = _exact_object(
        value,
        frozenset(("scenario_name", "orbit", "ground_station", "time_window", "radio_link")),
        "baseline_inputs",
    )
    _string(inputs["scenario_name"], "baseline_inputs.scenario_name")
    orbit = _exact_object(
        inputs["orbit"],
        frozenset(("source_url", "retrieved_at_utc", "terms_url", "sha256")),
        "baseline_inputs.orbit",
    )
    _string(orbit["source_url"], "baseline_inputs.orbit.source_url")
    _utc_timestamp(orbit["retrieved_at_utc"], "baseline_inputs.orbit.retrieved_at_utc")
    _string(orbit["terms_url"], "baseline_inputs.orbit.terms_url")
    _hash(orbit["sha256"], "baseline_inputs.orbit.sha256")
    station = _exact_object(
        inputs["ground_station"],
        frozenset(("name", "latitude_deg", "longitude_deg", "height_m")),
        "baseline_inputs.ground_station",
    )
    _string(station["name"], "baseline_inputs.ground_station.name")
    latitude = _finite_json(station["latitude_deg"], "baseline_inputs.ground_station.latitude_deg")
    longitude = _finite_json(
        station["longitude_deg"], "baseline_inputs.ground_station.longitude_deg"
    )
    if not -90.0 <= latitude <= 90.0:
        raise ValueError("baseline_inputs.ground_station.latitude_deg must be between -90 and 90")
    if not -180.0 <= longitude <= 180.0:
        raise ValueError(
            "baseline_inputs.ground_station.longitude_deg must be between -180 and 180"
        )
    _finite_json(station["height_m"], "baseline_inputs.ground_station.height_m")
    window = _exact_object(
        inputs["time_window"],
        frozenset(("start_utc", "stop_utc", "step_s", "minimum_elevation_deg")),
        "baseline_inputs.time_window",
    )
    start = _utc_timestamp(window["start_utc"], "baseline_inputs.time_window.start_utc")
    stop = _utc_timestamp(window["stop_utc"], "baseline_inputs.time_window.stop_utc")
    if stop <= start:
        raise ValueError("baseline_inputs.time_window.stop_utc must be after start_utc")
    step = _finite_json(window["step_s"], "baseline_inputs.time_window.step_s")
    elevation = _finite_json(
        window["minimum_elevation_deg"], "baseline_inputs.time_window.minimum_elevation_deg"
    )
    if step <= 0.0:
        raise ValueError("baseline_inputs.time_window.step_s must be positive")
    if not 0.0 <= elevation < 90.0:
        raise ValueError("baseline_inputs.time_window.minimum_elevation_deg must be in [0, 90)")
    radio = _exact_object(
        inputs["radio_link"],
        frozenset(
            (
                "carrier_frequency_hz",
                "channel_bandwidth_hz",
                "eirp_dbw",
                "receiver_gain_dbi",
                "system_noise_temperature_k",
                "miscellaneous_loss_db",
            )
        ),
        "baseline_inputs.radio_link",
    )
    carrier = _finite_json(
        radio["carrier_frequency_hz"], "baseline_inputs.radio_link.carrier_frequency_hz"
    )
    bandwidth = _finite_json(
        radio["channel_bandwidth_hz"], "baseline_inputs.radio_link.channel_bandwidth_hz"
    )
    eirp = _finite_json(radio["eirp_dbw"], "baseline_inputs.radio_link.eirp_dbw")
    _finite_json(radio["receiver_gain_dbi"], "baseline_inputs.radio_link.receiver_gain_dbi")
    temperature = _finite_json(
        radio["system_noise_temperature_k"],
        "baseline_inputs.radio_link.system_noise_temperature_k",
    )
    loss = _finite_json(
        radio["miscellaneous_loss_db"], "baseline_inputs.radio_link.miscellaneous_loss_db"
    )
    if carrier <= 0.0 or bandwidth <= 0.0 or temperature <= 0.0 or loss < 0.0:
        raise ValueError("sensitivity-summary.json baseline_inputs radio values are out of range")
    return {
        "time_window.step_s": step,
        "time_window.minimum_elevation_deg": elevation,
        "radio_link.eirp_dbw": eirp,
        "radio_link.system_noise_temperature_k": temperature,
        "radio_link.miscellaneous_loss_db": loss,
    }


def _baseline_context(value: Any) -> None:
    context = _exact_object(
        value,
        frozenset(
            (
                "element_epoch_utc",
                "start_element_age_days",
                "stop_element_age_days",
                "maximum_absolute_element_age_days",
                "leap_second_table_source",
                "leap_second_table_sha256",
                "warnings",
            )
        ),
        "baseline_context",
    )
    _utc_timestamp(context["element_epoch_utc"], "baseline_context.element_epoch_utc")
    start_age = _finite_json(
        context["start_element_age_days"], "baseline_context.start_element_age_days"
    )
    stop_age = _finite_json(
        context["stop_element_age_days"], "baseline_context.stop_element_age_days"
    )
    maximum_age = _finite_json(
        context["maximum_absolute_element_age_days"],
        "baseline_context.maximum_absolute_element_age_days",
    )
    if maximum_age < 0.0 or maximum_age != max(abs(start_age), abs(stop_age)):
        raise ValueError(
            "sensitivity-summary.json baseline_context.maximum_absolute_element_age_days "
            "must equal the maximum absolute endpoint age"
        )
    _string(context["leap_second_table_source"], "baseline_context.leap_second_table_source")
    _hash(context["leap_second_table_sha256"], "baseline_context.leap_second_table_sha256")
    warnings = context["warnings"]
    if not isinstance(warnings, list) or any(not isinstance(item, str) for item in warnings):
        raise ValueError("sensitivity-summary.json baseline_context.warnings must be strings")


def _sweeps(value: Any, baselines: dict[str, float]) -> tuple[_Sweep, ...]:
    if not isinstance(value, list) or not 1 <= len(value) <= 10:
        raise ValueError("sensitivity-summary.json sweeps must contain between 1 and 10 entries")
    sweeps = []
    for index, raw in enumerate(value):
        path = f"sweeps[{index}]"
        sweep = _exact_object(raw, frozenset(("parameter", "unit", "values")), path)
        parameter = sweep["parameter"]
        if parameter not in _PARAMETER_UNITS:
            raise ValueError(f"sensitivity-summary.json {path}.parameter is unsupported")
        if sweep["unit"] != _PARAMETER_UNITS[parameter]:
            raise ValueError(f"sensitivity-summary.json {path}.unit does not match parameter")
        raw_values = sweep["values"]
        if not isinstance(raw_values, list) or not 2 <= len(raw_values) <= 20:
            raise ValueError(f"sensitivity-summary.json {path}.values must contain 2 to 20 values")
        values = tuple(
            _finite_json(item, f"{path}.values[{value_index}]")
            for value_index, item in enumerate(raw_values)
        )
        if any(left >= right for left, right in pairwise(values)):
            raise ValueError(f"sensitivity-summary.json {path}.values must be strictly increasing")
        if values.count(baselines[parameter]) != 1:
            raise ValueError(f"sensitivity-summary.json {path}.values must contain its baseline")
        sweeps.append(_Sweep(parameter, str(sweep["unit"]), values))
    if len({sweep.parameter for sweep in sweeps}) != len(sweeps):
        raise ValueError("sensitivity-summary.json sweep parameters must be unique")
    if sum(len(sweep.values) for sweep in sweeps) > _MAX_ROWS:
        raise ValueError("sensitivity-summary.json sweeps exceed 64 cases")
    return tuple(sweeps)


def _read_rows(path: Path) -> tuple[_Row, ...]:
    try:
        _check_artifact_size(path, _MAX_CSV_BYTES)
        with path.open(encoding="utf-8", newline="") as file:
            reader = csv.DictReader(file)
            if tuple(reader.fieldnames or ()) != _FIELDS:
                raise ValueError("sensitivity.csv header must exactly match schema 1")
            raw_rows = tuple(islice(reader, _MAX_ROWS + 1))
    except (OSError, UnicodeError, csv.Error) as exc:
        raise ValueError(f"could not read sensitivity.csv: {exc}") from exc
    if len(raw_rows) > _MAX_ROWS:
        raise ValueError("sensitivity.csv exceeds 64 rows")
    if not raw_rows:
        raise ValueError("sensitivity.csv must contain at least one row")
    expected_fields = set(_FIELDS)
    for index, row in enumerate(raw_rows, start=1):
        if set(row) != expected_fields:
            raise ValueError(f"sensitivity.csv row {index} fields must exactly match schema 1")
    return tuple(_parse_row(row) for row in raw_rows)


def _parse_row(value: dict[str, str | None]) -> _Row:
    parameter = value.get("parameter")
    if parameter not in _PARAMETER_UNITS:
        raise ValueError("sensitivity.csv parameter is unsupported")
    unit = value.get("unit")
    if unit != _PARAMETER_UNITS[parameter]:
        raise ValueError("sensitivity.csv unit does not match parameter")
    nominal = value.get("is_nominal")
    if nominal not in {"true", "false"}:
        raise ValueError("sensitivity.csv is_nominal must be true or false")
    metrics = _parse_metrics(value, "sensitivity.csv")
    return _Row(
        parameter=parameter,
        unit=unit,
        baseline_value=_finite_csv(value, "baseline_value"),
        case_value=_finite_csv(value, "case_value"),
        is_nominal=nominal == "true",
        metrics=metrics,
        maximum_delta_bps=_finite_csv(value, "maximum_capacity_upper_bound_delta_bps"),
        maximum_relative_percent=_optional_finite_csv(
            value, "maximum_capacity_upper_bound_relative_change_percent"
        ),
        integrated_delta_bits=_finite_csv(value, "integrated_capacity_upper_bound_delta_bits"),
        integrated_relative_percent=_optional_finite_csv(
            value, "integrated_capacity_upper_bound_relative_change_percent"
        ),
    )


def _parse_metrics(value: Any, source: str) -> _Metrics:
    if source != "sensitivity.csv":
        value = _exact_object(value, _METRIC_FIELDS, "baseline")
    row_count = _positive_int(_item(value, "row_count"), f"{source} row_count")
    sampled_aos = _utc_timestamp(_item(value, "sampled_aos_utc"), f"{source} sampled_aos_utc")
    sampled_los = _utc_timestamp(_item(value, "sampled_los_utc"), f"{source} sampled_los_utc")
    if sampled_los < sampled_aos:
        raise ValueError(f"{source} sampled_los_utc must not precede sampled_aos_utc")
    finite = _finite_csv if source == "sensitivity.csv" else _finite_json_field
    duration = finite(value, "sampled_duration_s")
    elevation = finite(value, "maximum_elevation_deg")
    minimum_range = finite(value, "minimum_range_m")
    cn0 = finite(value, "maximum_carrier_to_noise_density_db_hz")
    maximum = finite(value, "maximum_shannon_capacity_upper_bound_bps")
    integrated = finite(value, "integrated_shannon_capacity_upper_bound_bits")
    if duration < 0.0:
        raise ValueError(f"{source} sampled_duration_s must be non-negative")
    if not 0.0 <= elevation <= 90.0:
        raise ValueError(f"{source} maximum_elevation_deg must be between 0 and 90")
    for name, number in (
        ("minimum_range_m", minimum_range),
        ("maximum_shannon_capacity_upper_bound_bps", maximum),
        ("integrated_shannon_capacity_upper_bound_bits", integrated),
    ):
        if number < 0.0:
            raise ValueError(f"{source} {name} must be non-negative")
    return _Metrics(
        row_count,
        sampled_aos,
        sampled_los,
        duration,
        elevation,
        minimum_range,
        cn0,
        maximum,
        integrated,
    )


def _validate_consistency(summary: _Summary, rows: tuple[_Row, ...]) -> None:
    if len(rows) != summary.case_count:
        raise ValueError("sensitivity-summary.json case_count does not match sensitivity.csv")
    expected = tuple((sweep, value) for sweep in summary.sweeps for value in sweep.values)
    for index, (row, (sweep, value)) in enumerate(zip(rows, expected, strict=True)):
        if row.parameter != sweep.parameter or row.case_value != value:
            raise ValueError(f"sensitivity.csv row {index + 1} violates sweep/value ordering")
        if row.unit != sweep.unit:
            raise ValueError(f"sensitivity.csv row {index + 1} unit does not match summary")
        baseline_value = summary.baselines[sweep.parameter]
        if row.baseline_value != baseline_value:
            raise ValueError(
                f"sensitivity.csv row {index + 1} baseline_value does not match summary"
            )
        nominal = value == baseline_value
        if row.is_nominal != nominal:
            raise ValueError(f"sensitivity.csv row {index + 1} nominal flag is inconsistent")
        if nominal and row.metrics != summary.baseline:
            raise ValueError(
                f"sensitivity.csv row {index + 1} nominal metrics do not match baseline"
            )
        _change(
            row.maximum_delta_bps,
            row.maximum_relative_percent,
            row.metrics.maximum_shannon_capacity_upper_bound_bps,
            summary.baseline.maximum_shannon_capacity_upper_bound_bps,
            index,
            "maximum_capacity_upper_bound_delta_bps",
            "maximum_capacity_upper_bound_relative_change_percent",
        )
        _change(
            row.integrated_delta_bits,
            row.integrated_relative_percent,
            row.metrics.integrated_shannon_capacity_upper_bound_bits,
            summary.baseline.integrated_shannon_capacity_upper_bound_bits,
            index,
            "integrated_capacity_upper_bound_delta_bits",
            "integrated_capacity_upper_bound_relative_change_percent",
        )
    parameters = {sweep.parameter for sweep in summary.sweeps}
    if "time_window.step_s" not in parameters:
        raise ValueError("sensitivity overview requires a sampling sweep")
    if "time_window.minimum_elevation_deg" not in parameters:
        raise ValueError("sensitivity overview requires an elevation sweep")
    if not parameters & _RF_PARAMETERS:
        raise ValueError("sensitivity overview requires at least one RF sweep")


def _change(
    delta: float,
    relative: float | None,
    value: float,
    baseline: float,
    index: int,
    delta_field: str,
    relative_field: str,
) -> None:
    expected_delta = value - baseline
    serialized_operand_tolerance = max(abs(value), abs(baseline)) * 1e-11 + 1e-8
    if not isclose(delta, expected_delta, rel_tol=5e-11, abs_tol=serialized_operand_tolerance):
        raise ValueError(f"sensitivity.csv row {index + 1} {delta_field} is inconsistent")
    if baseline == 0.0:
        if relative is not None:
            raise ValueError(f"sensitivity.csv row {index + 1} {relative_field} must be empty")
    elif relative is None or not _close(relative, expected_delta / baseline * 100.0):
        raise ValueError(f"sensitivity.csv row {index + 1} {relative_field} is inconsistent")


def _close(left: float, right: float) -> bool:
    return isclose(left, right, rel_tol=5e-11, abs_tol=1e-8)


def _exact_object(value: Any, fields: frozenset[str], path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"sensitivity-summary.json {path} must be an object")  # noqa: TRY004
    actual = set(value)
    if actual != fields:
        missing = sorted(fields - actual)
        extra = sorted(actual - fields)
        detail = f"missing fields {missing}" if missing else f"unexpected fields {extra}"
        raise ValueError(f"sensitivity-summary.json {path} has {detail}")
    return value


def _hash_object(value: Any, path: str) -> str:
    item = _exact_object(value, frozenset(("sha256",)), path)
    return _hash(item["sha256"], f"{path}.sha256")


def _hash(value: Any, path: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError(f"sensitivity-summary.json {path} must be lowercase 64-hex")
    return value


def _string(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"sensitivity-summary.json {path} must be a nonempty string")
    return value


def _positive_int(value: Any, path: str) -> int:
    if isinstance(value, str):
        try:
            number = int(value)
        except ValueError as exc:
            raise ValueError(f"{path} must be a positive integer") from exc
        if str(number) != value:
            raise ValueError(f"{path} must be a positive integer")
    elif isinstance(value, int) and not isinstance(value, bool):
        number = value
    else:
        raise ValueError(f"{path} must be a positive integer")  # noqa: TRY004
    if number < 1:
        raise ValueError(f"{path} must be a positive integer")
    return number


def _finite_json(value: Any, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value):
        raise ValueError(f"sensitivity-summary.json {path} must be finite")
    return float(value)


def _finite_json_field(value: dict[str, Any], field: str) -> float:
    return _finite_json(value.get(field), f"baseline.{field}")


def _finite_csv(value: dict[str, Any], field: str) -> float:
    item = value.get(field)
    try:
        number = float(item) if isinstance(item, str) else float("nan")
    except ValueError as exc:
        raise ValueError(f"sensitivity.csv {field} must be finite") from exc
    if not isfinite(number):
        raise ValueError(f"sensitivity.csv {field} must be finite")
    return number


def _optional_finite_csv(value: dict[str, Any], field: str) -> float | None:
    return None if value.get(field) == "" else _finite_csv(value, field)


def _utc_timestamp(value: Any, path: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError(f"sensitivity-summary.json {path} must be UTC")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"sensitivity-summary.json {path} must be UTC") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError(f"sensitivity-summary.json {path} must be timezone-aware UTC")
    return parsed


def _item(value: dict[str, Any], field: str) -> Any:
    return value.get(field)


def _check_artifact_size(path: Path, maximum: int) -> None:
    if path.stat().st_size > maximum:
        raise ValueError(f"{path.name} exceeds {maximum} bytes")
