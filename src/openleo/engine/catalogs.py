"""Bounded, checksum-verified CelesTrak GP catalogs with explicit SGP4 semantics.

``load_catalogs`` reads every catalog of a scenario's space segment for the 2.0 engine. Its
errors start with the JSON pointer of the catalog (``/space_segment/catalogs/<N>``) or of the
selected NORAD id.

``load_catalog`` is OpenLEO v1.0.0's single-file loader, which orbit verification still uses
(``openleo.verify.orbit``) with an ``OrbitSource``. It keeps the v1 limits
(``V1_MAX_SATELLITES`` records and ``V1_MAX_CATALOG_BYTES`` per file) and v1's order (numeric
NORAD id); the 2.0 engine keeps catalog then row order.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from io import StringIO
from itertools import islice
from math import isfinite, tau
from pathlib import Path
from typing import TYPE_CHECKING

from sgp4.api import SGP4_ERRORS
from skyfield.api import EarthSatellite

from openleo._validation import _finite_number, _positive_number, _range, _string

if TYPE_CHECKING:
    from collections.abc import Sequence

    from openleo.scenario.model import SpaceSegment

REQUIRED_OMM_FIELDS = frozenset(
    (
        "OBJECT_NAME",
        "OBJECT_ID",
        "EPOCH",
        "MEAN_MOTION",
        "ECCENTRICITY",
        "INCLINATION",
        "RA_OF_ASC_NODE",
        "ARG_OF_PERICENTER",
        "MEAN_ANOMALY",
        "EPHEMERIS_TYPE",
        "CLASSIFICATION_TYPE",
        "NORAD_CAT_ID",
        "ELEMENT_SET_NO",
        "REV_AT_EPOCH",
        "BSTAR",
        "MEAN_MOTION_DOT",
        "MEAN_MOTION_DDOT",
    )
)
# 2.0 limits: per catalog file, and for the satellites a run simulates.
MAX_SATELLITES = 20_000
MAX_CATALOG_BYTES = 10_000_000
# v1 limits, kept by load_catalog below.
V1_MAX_SATELLITES = 128
V1_MAX_CATALOG_BYTES = 1_000_000


@dataclass(frozen=True, slots=True)
class Provenance:
    """Where an archived catalog file came from, and its expected SHA-256 (lowercase hex)."""

    source_url: str
    retrieved_at_utc: datetime
    terms_url: str
    sha256: str


@dataclass(frozen=True, slots=True)
class OrbitSource:
    """One archived GP CSV file and its provenance, as ``load_catalog`` reads it."""

    path: Path
    provenance: Provenance


@dataclass(frozen=True, slots=True)
class LoadedOrbit:
    satellite: EarthSatellite
    epoch_utc: datetime
    actual_sha256: str


def load_catalogs(space: SpaceSegment, timescale) -> tuple[LoadedOrbit, ...]:
    """Load every catalog in catalog order, then row order, and apply ``select_norad_ids``.

    Each file is checked against its SHA-256 before it is parsed. A NORAD id may appear only
    once across all catalogs, and every selected id must be in one of them.
    """
    orbits: list[LoadedOrbit] = []
    catalog_of: dict[int, int] = {}
    for index, source in enumerate(space.catalogs):
        pointer = f"/space_segment/catalogs/{index}"
        try:
            catalog = _read_catalog(
                source.path,
                source.sha256,
                timescale,
                max_bytes=MAX_CATALOG_BYTES,
                max_satellites=MAX_SATELLITES,
            )
        except ValueError as exc:
            raise ValueError(f"{pointer}: {exc}") from exc
        for orbit in catalog:
            norad_id = orbit.satellite.model.satnum
            if norad_id in catalog_of:
                first = catalog_of[norad_id]
                where = (
                    "earlier in the same catalog"
                    if first == index
                    else f"in /space_segment/catalogs/{first}"
                )
                raise ValueError(
                    f"{pointer}: duplicate NORAD_CAT_ID {norad_id}: it already appears {where}"
                )
            catalog_of[norad_id] = index
            orbits.append(orbit)

    if space.select_norad_ids is not None:
        for position, norad_id in enumerate(space.select_norad_ids):
            if norad_id not in catalog_of:
                raise ValueError(
                    f"/space_segment/select_norad_ids/{position}={norad_id} is not in any catalog"
                )
        selected = frozenset(space.select_norad_ids)
        orbits = [orbit for orbit in orbits if orbit.satellite.model.satnum in selected]
    if len(orbits) > MAX_SATELLITES:
        raise ValueError(
            f"/space_segment/catalogs: {len(orbits)} satellites to simulate; "
            f"the limit is {MAX_SATELLITES}"
        )
    return tuple(orbits)


def minimum_period_s(orbits: Sequence[LoadedOrbit]) -> float:
    """Shortest orbital period: 2π over the largest catalog (Kozai) mean motion, in seconds.

    This is the period CelesTrak publishes, 86,400 s / MEAN_MOTION (rev/day).
    """
    if not orbits:
        raise ValueError("minimum_period_s needs at least one orbit")
    fastest_rad_per_min = max(orbit.satellite.model.no_kozai for orbit in orbits)
    return tau / fastest_rad_per_min * 60.0


def load_catalog(source: OrbitSource, timescale) -> tuple[LoadedOrbit, ...]:
    """v1: load one to 128 records, in numeric NORAD order, without altering the archived file."""
    catalog = _read_catalog(
        source.path,
        source.provenance.sha256,
        timescale,
        max_bytes=V1_MAX_CATALOG_BYTES,
        max_satellites=V1_MAX_SATELLITES,
    )
    ids = tuple(orbit.satellite.model.satnum for orbit in catalog)
    if len(set(ids)) != len(ids):
        duplicate = next(value for index, value in enumerate(ids) if value in ids[:index])
        raise ValueError(f"{source.path}: invalid GP CSV: duplicate NORAD_CAT_ID {duplicate}")
    return tuple(sorted(catalog, key=lambda orbit: orbit.satellite.model.satnum))


def _read_catalog(
    path: Path, expected_sha256: str, timescale, *, max_bytes: int, max_satellites: int
) -> tuple[LoadedOrbit, ...]:
    """Validate one GP CSV file and return its records in row order.

    Every error is a ``ValueError`` that starts with the file path.
    """
    try:
        with path.open("rb") as stream:
            raw = stream.read(max_bytes + 1)
    except OSError as exc:
        raise ValueError(f"{path}: could not read GP CSV: {exc}") from exc
    if len(raw) > max_bytes:
        raise ValueError(f"{path}: GP CSV exceeds {max_bytes} bytes")
    actual_sha256 = sha256(raw).hexdigest()
    if actual_sha256 != expected_sha256:
        raise ValueError(
            f"{path}: SHA-256 mismatch: expected {expected_sha256}, got {actual_sha256}"
        )
    try:
        rows = _rows(raw.decode("utf-8"), max_satellites)
        return tuple(_orbit(row, timescale, actual_sha256) for row in rows)
    except UnicodeDecodeError as exc:
        raise ValueError(f"{path}: GP CSV must be UTF-8") from exc
    except (csv.Error, KeyError, TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{path}: invalid GP CSV: {exc}") from exc


def _rows(text: str, max_satellites: int) -> tuple[dict, ...]:
    reader = csv.DictReader(StringIO(text, newline=""), strict=True)
    headers = reader.fieldnames or []
    if len(headers) != len(set(headers)):
        raise ValueError("duplicate GP header fields")
    missing = REQUIRED_OMM_FIELDS - set(headers)
    if missing:
        raise ValueError(f"missing required GP fields: {', '.join(sorted(missing))}")
    rows = tuple(islice(reader, max_satellites + 1))
    if not rows:
        raise ValueError("expected at least one GP record")
    if len(rows) > max_satellites:
        raise ValueError(f"GP catalog exceeds {max_satellites} satellite records")
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
