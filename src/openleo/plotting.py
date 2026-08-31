"""Static rendering of completed pass artifacts."""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import datetime
from itertools import islice, pairwise
from math import isfinite, pi
from pathlib import Path
from typing import Any

_TRACE_FIELDS = (
    "timestamp_utc",
    "azimuth_deg",
    "elevation_deg",
    "doppler_hz",
    "carrier_to_noise_density_db_hz",
    "shannon_capacity_upper_bound_bps",
)
_LIMITATION = (
    "Frozen orbital geometry; synthetic RF inputs; free-space only; "
    "Shannon-Hartley upper bound, not throughput."
)
_MAX_SUMMARY_BYTES = 10_000_000
_MAX_TRACE_BYTES = 50_000_000
_MAX_TRACE_ROWS = 100_000


@dataclass(frozen=True)
class _Row:
    timestamp: datetime
    azimuth_deg: float
    elevation_deg: float
    doppler_hz: float
    cn0_db_hz: float
    capacity_bps: float


def render_pass_overview(run_directory: str | Path, output_path: str | Path) -> Path:
    """Render a completed schema-v1 run as SVG or PNG."""
    run = Path(run_directory)
    output = Path(output_path)
    suffix = output.suffix.lower()
    if suffix not in {".svg", ".png"}:
        raise ValueError("output_path must end in .svg or .png")

    summary = _read_summary(run / "summary.json")
    rows = _read_rows(run / "trace.csv")
    _validate_consistency(summary, rows)

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

    start = rows[0].timestamp
    minutes = tuple((row.timestamp - start).total_seconds() / 60.0 for row in rows)
    figure = plt.figure(figsize=(12, 7), facecolor="white", layout="none")
    try:
        grid = figure.add_gridspec(
            2,
            2,
            left=0.06,
            right=0.96,
            bottom=0.15,
            top=0.86,
            wspace=0.25,
            hspace=0.25,
        )
        polar = figure.add_subplot(grid[0, 0], projection="polar")
        doppler = figure.add_subplot(grid[0, 1])
        cn0 = figure.add_subplot(grid[1, 0])
        capacity = figure.add_subplot(grid[1, 1])
        polar.set_theta_zero_location("N")
        polar.set_theta_direction(-1)
        polar.plot(
            tuple(row.azimuth_deg * pi / 180.0 for row in rows),
            tuple(90.0 - row.elevation_deg for row in rows),
            color="#0072B2",
        )
        maximum = max(range(len(rows)), key=lambda index: rows[index].elevation_deg)
        for index, label in (
            (0, "sampled AOS"),
            (maximum, "maximum elevation"),
            (-1, "sampled LOS"),
        ):
            row = rows[index]
            polar.scatter(row.azimuth_deg * pi / 180.0, 90.0 - row.elevation_deg, label=label)
        polar.set_title("Sky track")
        polar.set_rlim(0.0, 90.0)
        polar.set_rgrids((20.0, 40.0, 60.0, 80.0), labels=("70°", "50°", "30°", "10°"))
        polar.legend(loc="lower left", bbox_to_anchor=(1.02, 0.0), fontsize="small")

        _line(
            doppler,
            minutes,
            tuple(row.doppler_hz / 1_000.0 for row in rows),
            "Nominal first-order Doppler (kHz)",
        )
        _line(cn0, minutes, tuple(row.cn0_db_hz for row in rows), "C/N₀ (dB-Hz)")
        _line(
            capacity,
            minutes,
            tuple(row.capacity_bps / 1_000_000.0 for row in rows),
            "Capacity upper bound (Mbit/s)",
        )
        figure.suptitle(str(summary["scenario_name"]), fontsize="large")
        figure.text(0.5, 0.01, _LIMITATION, ha="center", fontsize="small")
        output.parent.mkdir(parents=True, exist_ok=True)
        metadata = {
            "Title": str(summary["scenario_name"]),
            "Description": (
                f"scenario_sha256={summary['scenario_hash']}; "
                f"openleo={summary['openleo_version']}; matplotlib={matplotlib.__version__}"
            ),
            "Creator": "OpenLEO",
            "Date": None,
        }
        with matplotlib.rc_context(
            {"svg.fonttype": "none", "svg.hashsalt": "openleo-pass-overview"}
        ):
            figure.savefig(output, dpi=200 if suffix == ".png" else None, metadata=metadata)
    finally:
        plt.close(figure)
    return output


def _line(axis: Any, minutes: tuple[float, ...], values: tuple[float, ...], label: str) -> None:
    axis.plot(minutes, values, color="#D55E00")
    axis.set_xlabel("Minutes from sampled AOS")
    axis.set_ylabel(label)
    axis.grid()


def _read_summary(path: Path) -> dict[str, str | int]:
    try:
        _check_artifact_size(path, _MAX_SUMMARY_BYTES)
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"could not read summary.json: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError("summary.json must be an object")  # noqa: TRY004
    schema_version = _summary_string(value, "schema_version")
    if schema_version != "1":
        raise ValueError("summary.json schema_version must be 1")
    scenario_hash = _summary_nested_string(value, "provenance", "scenario", "sha256")
    return {
        "scenario_name": _summary_string(value, "scenario_name"),
        "scenario_hash": scenario_hash,
        "openleo_version": _summary_nested_string(value, "versions", "openleo-link"),
        "row_count": _summary_int(value, "row_count"),
        "sampled_aos_utc": _summary_string(value, "sampled_aos_utc"),
        "sampled_los_utc": _summary_string(value, "sampled_los_utc"),
    }


def _summary_string(value: dict[str, Any], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise ValueError(f"summary.json missing required field: {key}")
    return item


def _summary_nested_string(value: dict[str, Any], *keys: str) -> str:
    item: Any = value
    for key in keys:
        if not isinstance(item, dict):
            break
        item = item.get(key)
    if not isinstance(item, str) or not item:
        raise ValueError(f"summary.json missing required field: {'.'.join(keys)}")
    return item


def _summary_int(value: dict[str, Any], key: str) -> int:
    item = value.get(key)
    if not isinstance(item, int) or isinstance(item, bool) or item < 1:
        raise ValueError(f"summary.json {key} must be a positive integer")
    return item


def _read_rows(path: Path) -> tuple[_Row, ...]:
    try:
        _check_artifact_size(path, _MAX_TRACE_BYTES)
        with path.open(encoding="utf-8", newline="") as file:
            reader = csv.DictReader(file)
            fields = reader.fieldnames or []
            for field in _TRACE_FIELDS:
                if field not in fields:
                    raise ValueError(f"trace.csv missing required field: {field}")
            raw_rows = tuple(islice(reader, _MAX_TRACE_ROWS + 1))
    except (OSError, UnicodeError, csv.Error) as exc:
        raise ValueError(f"could not read trace.csv: {exc}") from exc
    if len(raw_rows) > _MAX_TRACE_ROWS:
        raise ValueError(f"trace.csv exceeds {_MAX_TRACE_ROWS} rows")
    rows = tuple(_parse_row(row) for row in raw_rows)
    if not rows:
        raise ValueError("trace.csv must contain at least one row")
    if any(left.timestamp >= right.timestamp for left, right in pairwise(rows)):
        raise ValueError("trace.csv timestamps must be strictly increasing")
    return rows


def _check_artifact_size(path: Path, maximum: int) -> None:
    if path.stat().st_size > maximum:
        raise ValueError(f"{path.name} exceeds {maximum} bytes")


def _parse_row(value: dict[str, str | None]) -> _Row:
    azimuth = _finite_float(value.get("azimuth_deg"), "azimuth_deg")
    if not 0.0 <= azimuth <= 360.0:
        raise ValueError("trace.csv azimuth_deg must be between 0 and 360 inclusive")
    elevation = _finite_float(value.get("elevation_deg"), "elevation_deg")
    if not 0.0 <= elevation <= 90.0:
        raise ValueError("trace.csv elevation_deg must be between 0 and 90 inclusive")
    capacity = _finite_float(
        value.get("shannon_capacity_upper_bound_bps"), "shannon_capacity_upper_bound_bps"
    )
    if capacity < 0.0:
        raise ValueError("trace.csv shannon_capacity_upper_bound_bps must be non-negative")
    return _Row(
        timestamp=_utc_timestamp(value.get("timestamp_utc")),
        azimuth_deg=azimuth,
        elevation_deg=elevation,
        doppler_hz=_finite_float(value.get("doppler_hz"), "doppler_hz"),
        cn0_db_hz=_finite_float(
            value.get("carrier_to_noise_density_db_hz"), "carrier_to_noise_density_db_hz"
        ),
        capacity_bps=capacity,
    )


def _finite_float(value: str | None, field: str) -> float:
    try:
        number = float(value) if value is not None else float("nan")
    except ValueError as exc:
        raise ValueError(f"trace.csv {field} must be finite") from exc
    if not isfinite(number):
        raise ValueError(f"trace.csv {field} must be finite")
    return number


def _utc_timestamp(value: str | None) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("trace.csv timestamp_utc must be UTC")
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("trace.csv timestamp_utc must be UTC") from exc


def _validate_consistency(summary: dict[str, str | int], rows: tuple[_Row, ...]) -> None:
    if summary["row_count"] != len(rows):
        raise ValueError("summary.json row_count does not match trace.csv")
    if summary["sampled_aos_utc"] != rows[0].timestamp.isoformat().replace("+00:00", "Z"):
        raise ValueError("summary.json sampled_aos_utc does not match trace.csv")
    if summary["sampled_los_utc"] != rows[-1].timestamp.isoformat().replace("+00:00", "Z"):
        raise ValueError("summary.json sampled_los_utc does not match trace.csv")
