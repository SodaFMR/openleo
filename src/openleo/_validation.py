"""Shared boundary validators for OpenLEO scenario and study documents.

Private to the package: the v1 parsers, the v2 scenario parser and the physics kernels all
check untrusted JSON through these helpers, so each bound has exactly one implementation.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from math import isfinite
from numbers import Real
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

MAX_SCENARIO_BYTES = 1_000_000


def _read_scenario_bytes(path: Path) -> bytes:
    with path.open("rb") as scenario_file:
        source_bytes = scenario_file.read(MAX_SCENARIO_BYTES + 1)
    if len(source_bytes) > MAX_SCENARIO_BYTES:
        raise ValueError(f"{path}: scenario JSON exceeds {MAX_SCENARIO_BYTES} bytes")
    return source_bytes


def _read_bounded(path: Path, limit: int) -> bytes:
    """At most ``limit`` bytes of a file; ValueError naming the file if it is longer."""
    try:
        with path.open("rb") as file:
            raw = file.read(limit + 1)
    except OSError as exc:
        raise ValueError(f"could not read {path.name}") from exc
    if len(raw) > limit:
        raise ValueError(f"{path.name} exceeds {limit} bytes")
    return raw


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """``object_pairs_hook`` that rejects repeated JSON keys."""
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _object(
    raw: Any, expected: frozenset[str], path: str, optional: frozenset[str] = frozenset()
) -> Mapping[str, Any]:
    if not isinstance(raw, Mapping):
        raise TypeError(f"{path} must be a JSON object")
    actual = set(raw)
    missing = expected - actual
    unknown = actual - expected - optional
    if missing:
        raise ValueError(f"{_field(path, min(missing))} is required")
    if unknown:
        raise ValueError(f"{_field(path, min(unknown))} is not allowed")
    return raw


def _field(path: str, name: str) -> str:
    """Name a child of ``path``: dotted for the v1 ``scenario`` root, a JSON pointer otherwise.

    A path that is empty or starts with ``/`` is an RFC 6901 pointer (the empty string is the
    document root), and ``name`` is escaped as one reference token.
    """
    if path == "scenario":
        return name
    if path == "" or path.startswith("/"):
        return f"{path}/{name.replace('~', '~0').replace('/', '~1')}"
    return f"{path}.{name}"


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
