"""Scenario JSON loading and validation."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from json import JSONDecodeError
from math import isclose, isfinite
from numbers import Real
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from openleo.model import GroundStation, OrbitSource, Provenance, RadioLink, Scenario, TimeWindow

SCENARIO_KEYS = frozenset(("name", "orbit", "ground_station", "time_window", "radio_link"))
ORBIT_KEYS = frozenset(("path", "source_url", "retrieved_at_utc", "terms_url", "sha256"))
GROUND_STATION_KEYS = frozenset(("name", "latitude_deg", "longitude_deg", "height_m"))
TIME_WINDOW_KEYS = frozenset(("start_utc", "stop_utc", "step_s", "minimum_elevation_deg"))
RADIO_LINK_KEYS = frozenset(
    (
        "carrier_frequency_hz",
        "channel_bandwidth_hz",
        "eirp_dbw",
        "receiver_gain_dbi",
        "system_noise_temperature_k",
        "miscellaneous_loss_db",
    )
)
MAX_TIME_GRID_SAMPLES = 100_000


def load_scenario(path: str | Path) -> Scenario:
    scenario_path = Path(path)
    try:
        source_bytes = scenario_path.read_bytes()
        raw = json.loads(source_bytes.decode("utf-8"))
        return _scenario(raw, scenario_path, sha256(source_bytes).hexdigest())
    except UnicodeDecodeError as exc:
        raise ValueError(f"{scenario_path}: scenario JSON must be UTF-8") from exc
    except JSONDecodeError as exc:
        raise ValueError(f"{scenario_path}: invalid JSON: {exc.msg}") from exc
    except OSError as exc:
        raise ValueError(f"{scenario_path}: could not load scenario: {exc}") from exc
    except (KeyError, TypeError) as exc:
        raise ValueError(f"{scenario_path}: invalid scenario: {exc}") from exc
    except ValueError as exc:
        if str(exc).startswith(f"{scenario_path}:"):
            raise
        raise ValueError(f"{scenario_path}: invalid scenario: {exc}") from exc


def _scenario(raw: Any, scenario_path: Path, source_sha256: str) -> Scenario:
    data = _object(raw, SCENARIO_KEYS, "scenario")
    return Scenario(
        name=_string(data["name"], "name"),
        source_sha256=source_sha256,
        orbit=_orbit(data["orbit"], scenario_path),
        ground_station=_ground_station(data["ground_station"]),
        time_window=_time_window(data["time_window"]),
        radio_link=_radio_link(data["radio_link"]),
    )


def _orbit(raw: Any, scenario_path: Path) -> OrbitSource:
    data = _object(raw, ORBIT_KEYS, "orbit")
    return OrbitSource(
        path=(scenario_path.parent / _string(data["path"], "orbit.path")).resolve(),
        provenance=Provenance(
            source_url=_url(data["source_url"], "orbit.source_url"),
            retrieved_at_utc=_utc(data["retrieved_at_utc"], "orbit.retrieved_at_utc"),
            terms_url=_url(data["terms_url"], "orbit.terms_url"),
            sha256=_sha256(data["sha256"], "orbit.sha256"),
        ),
    )


def _ground_station(raw: Any) -> GroundStation:
    data = _object(raw, GROUND_STATION_KEYS, "ground_station")
    return GroundStation(
        name=_string(data["name"], "ground_station.name"),
        latitude_deg=_range(data["latitude_deg"], "ground_station.latitude_deg", -90.0, 90.0),
        longitude_deg=_range(data["longitude_deg"], "ground_station.longitude_deg", -180.0, 180.0),
        height_m=_finite_number(data["height_m"], "ground_station.height_m"),
    )


def _time_window(raw: Any) -> TimeWindow:
    data = _object(raw, TIME_WINDOW_KEYS, "time_window")
    start = _utc(data["start_utc"], "time_window.start_utc")
    stop = _utc(data["stop_utc"], "time_window.stop_utc")
    if start >= stop:
        raise ValueError("time_window.stop_utc must be after time_window.start_utc")
    step_s = _positive_number(data["step_s"], "time_window.step_s")
    if step_s > (stop - start).total_seconds():
        raise ValueError(
            f"time_window.step_s={_safe_repr(step_s)} must be no larger than "
            f"the window duration {(stop - start).total_seconds()}"
        )
    step = timedelta(seconds=step_s)
    represented_step_s = step.total_seconds()
    if represented_step_s <= 0.0 or not isclose(
        represented_step_s, step_s, rel_tol=0.0, abs_tol=1e-12
    ):
        raise ValueError(
            f"time_window.step_s={_safe_repr(step_s)} must be positive and exactly "
            "representable at microsecond resolution"
        )
    quotient, remainder = divmod(stop - start, step)
    sample_count = quotient + 1 + bool(remainder)
    if sample_count > MAX_TIME_GRID_SAMPLES:
        raise ValueError(
            f"time_window.step_s={_safe_repr(step_s)} produces {sample_count} inclusive "
            f"samples; limit is {MAX_TIME_GRID_SAMPLES}; increase time_window.step_s or "
            "shorten the window"
        )
    return TimeWindow(
        start_utc=start,
        stop_utc=stop,
        step_s=step_s,
        minimum_elevation_deg=_range(
            data["minimum_elevation_deg"],
            "time_window.minimum_elevation_deg",
            0.0,
            90.0,
            upper_inclusive=False,
        ),
    )


def _radio_link(raw: Any) -> RadioLink:
    data = _object(raw, RADIO_LINK_KEYS, "radio_link")
    return RadioLink(
        carrier_frequency_hz=_positive_number(
            data["carrier_frequency_hz"], "radio_link.carrier_frequency_hz"
        ),
        channel_bandwidth_hz=_positive_number(
            data["channel_bandwidth_hz"], "radio_link.channel_bandwidth_hz"
        ),
        eirp_dbw=_finite_number(data["eirp_dbw"], "radio_link.eirp_dbw"),
        receiver_gain_dbi=_finite_number(data["receiver_gain_dbi"], "radio_link.receiver_gain_dbi"),
        system_noise_temperature_k=_positive_number(
            data["system_noise_temperature_k"], "radio_link.system_noise_temperature_k"
        ),
        miscellaneous_loss_db=_non_negative_number(
            data["miscellaneous_loss_db"], "radio_link.miscellaneous_loss_db"
        ),
    )


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
    return name if path == "scenario" else f"{path}.{name}"


def _string(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{path} must be a non-empty string")
    return value


def _url(value: Any, path: str) -> str:
    valid = False
    if isinstance(value, str):
        try:
            parsed = urlsplit(value)
            valid = (
                bool(value.strip())
                and parsed.scheme in {"http", "https"}
                and bool(parsed.netloc)
                and parsed.username is None
                and parsed.password is None
                and not any(char.isspace() for char in value)
            )
        except ValueError:
            pass
    if not valid:
        raise ValueError(
            f"{path}={_safe_repr(value)} must be an absolute HTTP or HTTPS URL "
            "without credentials or whitespace"
        )
    return value


def _utc(value: Any, path: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError(f"{path} must be an ISO 8601 UTC timestamp ending in Z")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{path} must be a valid ISO 8601 UTC timestamp") from exc
    if parsed.tzinfo is not UTC or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise ValueError(f"{path} must be timezone-aware UTC")
    return parsed


def _sha256(value: Any, path: str) -> str:
    text = _string(value, path)
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise ValueError(f"{path} must be exactly 64 lowercase hexadecimal characters")
    return text


def _finite_number(value: Any, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real) or not isfinite(value):
        raise ValueError(f"{path}={_safe_repr(value)} must be finite")
    return float(value)


def _positive_number(value: Any, path: str) -> float:
    number = _finite_number(value, path)
    if number <= 0.0:
        raise ValueError(f"{path}={_safe_repr(value)} must be positive and finite")
    return number


def _non_negative_number(value: Any, path: str) -> float:
    number = _finite_number(value, path)
    if number < 0.0:
        raise ValueError(f"{path}={_safe_repr(value)} must be finite and non-negative")
    return number


def _range(
    value: Any,
    path: str,
    lower: float,
    upper: float,
    *,
    upper_inclusive: bool = True,
) -> float:
    number = _finite_number(value, path)
    too_high = number > upper or (number == upper and not upper_inclusive)
    if number < lower or too_high:
        bracket = "]" if upper_inclusive else ")"
        raise ValueError(f"{path}={_safe_repr(value)} must be in [{lower}, {upper}{bracket}")
    return number


def _safe_repr(value: Any, limit: int = 120) -> str:
    text = repr(value)
    return text if len(text) <= limit else f"{text[: limit - 3]}..."
