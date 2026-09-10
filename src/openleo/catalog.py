"""Bounded, checksum-verified CelesTrak GP catalogs with explicit SGP4 semantics."""

from __future__ import annotations

import csv
from datetime import UTC, datetime
from hashlib import sha256
from io import StringIO
from itertools import islice
from math import isfinite

from sgp4.api import SGP4_ERRORS
from skyfield.api import EarthSatellite

from openleo.input import _finite_number, _positive_number, _range, _string
from openleo.model import OrbitSource
from openleo.orbit import MAX_ORBIT_BYTES, REQUIRED_OMM_FIELDS, LoadedOrbit

MAX_SATELLITES = 128


def load_catalog(source: OrbitSource, timescale) -> tuple[LoadedOrbit, ...]:
    """Load one to 128 records without downloading or altering the archived file."""
    try:
        with source.path.open("rb") as stream:
            raw = stream.read(MAX_ORBIT_BYTES + 1)
    except OSError as exc:
        raise ValueError(f"{source.path}: could not read GP CSV: {exc}") from exc
    if len(raw) > MAX_ORBIT_BYTES:
        raise ValueError(f"{source.path}: GP CSV exceeds {MAX_ORBIT_BYTES} bytes")
    actual_sha256 = sha256(raw).hexdigest()
    if actual_sha256 != source.provenance.sha256:
        raise ValueError(
            f"{source.path}: SHA-256 mismatch: expected {source.provenance.sha256}, "
            f"got {actual_sha256}"
        )
    try:
        rows = _rows(raw.decode("utf-8"))
        catalog = tuple(_orbit(row, timescale, actual_sha256) for row in rows)
        ids = tuple(orbit.satellite.model.satnum for orbit in catalog)
        if len(set(ids)) != len(ids):
            duplicate = next(value for index, value in enumerate(ids) if value in ids[:index])
            raise ValueError(f"duplicate NORAD_CAT_ID {duplicate}")
        return tuple(sorted(catalog, key=lambda orbit: orbit.satellite.model.satnum))
    except UnicodeDecodeError as exc:
        raise ValueError(f"{source.path}: GP CSV must be UTF-8") from exc
    except (csv.Error, KeyError, TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{source.path}: invalid GP CSV: {exc}") from exc


def _rows(text: str) -> tuple[dict, ...]:
    reader = csv.DictReader(StringIO(text, newline=""), strict=True)
    headers = reader.fieldnames or []
    if len(headers) != len(set(headers)):
        raise ValueError("duplicate GP header fields")
    missing = REQUIRED_OMM_FIELDS - set(headers)
    if missing:
        raise ValueError(f"missing required GP fields: {', '.join(sorted(missing))}")
    rows = tuple(islice(reader, MAX_SATELLITES + 1))
    if not rows:
        raise ValueError("expected at least one GP record")
    if len(rows) > MAX_SATELLITES:
        raise ValueError(f"GP catalog exceeds {MAX_SATELLITES} satellite records")
    if any(None in row or None in row.values() for row in rows):
        raise ValueError("GP record columns do not match header fields")
    return rows


def _orbit(row: dict, timescale, checksum: str) -> LoadedOrbit:
    _string(row["OBJECT_NAME"], "OBJECT_NAME")
    _string(row["OBJECT_ID"], "OBJECT_ID")
    for field in ("NORAD_CAT_ID", "ELEMENT_SET_NO", "REV_AT_EPOCH", "EPHEMERIS_TYPE"):
        text = row[field]
        minimum = 1 if field == "NORAD_CAT_ID" else 0
        if not text or any(char not in "0123456789" for char in text):
            raise ValueError(f"{field} must be a non-negative integer")
        if not minimum <= int(text) <= 2_147_483_647:
            raise ValueError(f"{field} must be in [{minimum}, 2147483647]")
    if int(row["EPHEMERIS_TYPE"]) != 0:
        raise ValueError("EPHEMERIS_TYPE must be 0 for this CelesTrak GP model")
    if row["CLASSIFICATION_TYPE"] not in {"U", "C", "S"}:
        raise ValueError("CLASSIFICATION_TYPE must be U, C, or S")
    for field, expected in (
        ("TIME_SYSTEM", "UTC"),
        ("REF_FRAME", "TEME"),
        ("CENTER_NAME", "EARTH"),
        ("MEAN_ELEMENT_THEORY", "SGP4"),
    ):
        if field in row and row[field] != expected:
            raise ValueError(f"{field} must be {expected}")
    _positive_number(_number(row, "MEAN_MOTION"), "MEAN_MOTION")
    _range(_number(row, "ECCENTRICITY"), "ECCENTRICITY", 0.0, 1.0, upper_inclusive=False)
    _range(_number(row, "INCLINATION"), "INCLINATION", 0.0, 180.0)
    for field in ("RA_OF_ASC_NODE", "ARG_OF_PERICENTER", "MEAN_ANOMALY"):
        _range(_number(row, field), field, 0.0, 360.0, upper_inclusive=False)
    for field in ("BSTAR", "MEAN_MOTION_DOT", "MEAN_MOTION_DDOT"):
        _number(row, field)
    epoch = _epoch(row["EPOCH"])
    satellite = EarthSatellite.from_omm(
        timescale, {**row, "EPOCH": epoch.replace(tzinfo=None).isoformat(timespec="microseconds")}
    )
    if satellite.model.error:
        raise ValueError(f"SGP4 initialization failed: {SGP4_ERRORS[satellite.model.error]}")
    if not all(isfinite(getattr(satellite.model, field)) for field in ("a", "no_kozai", "ecco")):
        raise ValueError("SGP4 initialization produced non-finite elements")
    return LoadedOrbit(
        satellite=satellite,
        epoch_utc=satellite.epoch.utc_datetime().astimezone(UTC),
        actual_sha256=checksum,
    )


def _number(row: dict, field: str) -> float:
    try:
        number = float(row[field])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a finite number") from exc
    return _finite_number(number, field)


def _epoch(text: str) -> datetime:
    try:
        epoch = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError("EPOCH must be a valid ISO 8601 UTC timestamp") from exc
    if "T" not in text or (epoch.tzinfo is not None and epoch.utcoffset().total_seconds() != 0):
        raise ValueError("EPOCH must use UTC; naive GP epochs are interpreted as UTC")
    return epoch.replace(tzinfo=UTC)
