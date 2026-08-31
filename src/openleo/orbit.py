"""Frozen GP orbit loading."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from io import StringIO

from skyfield.api import EarthSatellite

from openleo.model import OrbitSource

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


@dataclass(frozen=True, slots=True)
class LoadedOrbit:
    satellite: EarthSatellite
    epoch_utc: datetime
    actual_sha256: str


def load_orbit(source: OrbitSource, timescale) -> LoadedOrbit:
    try:
        raw = source.path.read_bytes()
    except OSError as exc:
        raise ValueError(f"{source.path}: could not read GP CSV: {exc}") from exc

    actual_sha256 = sha256(raw).hexdigest()
    if actual_sha256 != source.provenance.sha256:
        raise ValueError(
            f"{source.path}: SHA-256 mismatch: expected {source.provenance.sha256}, "
            f"got {actual_sha256}"
        )

    try:
        text = raw.decode("utf-8")
        rows = list(csv.DictReader(StringIO(text, newline="")))
        if len(rows) != 1:
            raise ValueError(f"expected exactly one GP record, got {len(rows)}")
        row = rows[0]
        missing = REQUIRED_OMM_FIELDS - set(row)
        if missing:
            raise ValueError(f"missing required GP fields: {', '.join(sorted(missing))}")
        satellite = EarthSatellite.from_omm(timescale, row)
    except UnicodeDecodeError as exc:
        raise ValueError(f"{source.path}: GP CSV must be UTF-8") from exc
    except (KeyError, TypeError, ValueError) as exc:
        message = str(exc) or exc.__class__.__name__
        if str(source.path) in message:
            raise
        raise ValueError(f"{source.path}: invalid GP CSV: {message}") from exc

    return LoadedOrbit(
        satellite=satellite,
        epoch_utc=satellite.epoch.utc_datetime().astimezone(UTC),
        actual_sha256=actual_sha256,
    )
