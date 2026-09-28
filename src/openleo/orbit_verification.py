"""Offline numerical verification against published Vallado TEME states.

Reference: https://celestrak.org/publications/AIAA/2006-6753/
"""

from __future__ import annotations

import csv
import json
import re
from hashlib import sha256
from importlib.metadata import version
from io import StringIO
from math import dist, isfinite
from pathlib import Path
from tempfile import TemporaryDirectory

from sgp4.api import Satrec
from sgp4.earth_gravity import wgs72
from sgp4.exporter import export_omm
from sgp4.io import twoline2rv, verify_checksum
from skyfield.api import load
from skyfield.sgp4lib import TEME

from openleo.catalog import _epoch, load_catalog
from openleo.input import _finite_number, _object, _positive_number, _string, _url
from openleo.model import OrbitSource, Provenance
from openleo.workbench import _read, _unique_object

_UNITS = {"position": "km", "velocity": "km/s", "elapsed_time": "s"}
_SOURCE_FIELDS = frozenset(
    (
        "publication_url",
        "terms_url",
        "archive_url",
        "archive_sha256",
        "tle_member",
        "tle_sha256",
        "ephemeris_member",
        "ephemeris_sha256",
        "verified_at_utc",
    )
)


def _reference(path):
    raw = _read(path, 100_000)
    data = json.loads(raw, object_pairs_hook=_unique_object)
    _object(
        data,
        frozenset(
            (
                "schema_version",
                "name",
                "frame",
                "units",
                "scenario_epoch_utc",
                "tle",
                "source",
                "tolerances",
                "states",
            )
        ),
        "reference",
    )
    if data["schema_version"] != "1":
        raise ValueError("unsupported orbit reference schema_version")
    _string(data["name"], "name")
    if data["frame"] != "TEME":
        raise ValueError("orbit reference frame must be TEME")
    if data["units"] != _UNITS:
        raise ValueError(f"orbit reference units must be {_UNITS}")
    _epoch(_string(data["scenario_epoch_utc"], "scenario_epoch_utc"))
    source = _object(data["source"], _SOURCE_FIELDS, "source")
    for key, value in source.items():
        _string(value, f"source.{key}")
        if key.endswith("_sha256") and not re.fullmatch(r"[0-9a-f]{64}", value):
            raise ValueError(f"source.{key} must be a lowercase SHA-256 digest")
        if key.endswith("_url"):
            _url(value, f"source.{key}")
    _epoch(source["verified_at_utc"])
    tolerances = _object(
        data["tolerances"], frozenset(("position_km", "velocity_km_s")), "tolerances"
    )
    for key, value in tolerances.items():
        _positive_number(value, f"tolerances.{key}")
    if not isinstance(data["states"], list) or not 1 <= len(data["states"]) <= 64:
        raise ValueError("states must contain one to 64 reference epochs")
    for state in data["states"]:
        _object(state, frozenset(("elapsed_s", "position_km", "velocity_km_s")), "state")
        _finite_number(state["elapsed_s"], "elapsed_s")
        for key in ("position_km", "velocity_km_s"):
            if not isinstance(state[key], list) or len(state[key]) != 3:
                raise ValueError(f"{key} must contain three components")
            for value in state[key]:
                _finite_number(value, key)
    return data, sha256(raw).hexdigest()


def _convert_tle(data):
    lines = data["tle"]
    if (
        not isinstance(lines, list)
        or len(lines) != 2
        or any(
            not isinstance(line, str) or len(line) != 69 or line[-1] not in "0123456789"
            for line in lines
        )
    ):
        raise ValueError("TLE must contain two 69-character lines")
    try:
        verify_checksum(*lines)
        parsed = twoline2rv(*lines, wgs72)
        fields = (
            "epochdays",
            "ndot",
            "nddot",
            "bstar",
            "inclo",
            "nodeo",
            "ecco",
            "argpo",
            "mo",
            "no_kozai",
        )
        if not all(isfinite(getattr(parsed, field)) for field in fields):
            raise ValueError("TLE elements must be finite")
        original = Satrec.twoline2rv(*lines)
        # This new OMM record is input data; published state literals are never derived here.
        row = export_omm(original, data["name"])
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"invalid TLE: {exc}") from exc
    if abs((_epoch(row["EPOCH"]) - _epoch(data["scenario_epoch_utc"])).total_seconds()) > 1e-6:
        raise ValueError("scenario_epoch_utc must match the TLE epoch within a microsecond")
    stream = StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=tuple(row), lineterminator="\n")
    writer.writeheader()
    writer.writerow(row)
    return original, row["EPOCH"], stream.getvalue().encode("utf-8")


def _load_adapter(data, csv_bytes, directory):
    path = Path(directory) / "reference.csv"
    path.write_bytes(csv_bytes)
    source = OrbitSource(
        path,
        Provenance(
            source_url=data["source"]["archive_url"],
            retrieved_at_utc=_epoch(data["source"]["verified_at_utc"]),
            terms_url=data["source"]["terms_url"],
            sha256=sha256(csv_bytes).hexdigest(),
        ),
    )
    return load_catalog(source, load.timescale(builtin=True))[0].satellite


def _compare(satellite, state, tolerances):
    time = satellite.epoch + state["elapsed_s"] / 86400.0
    propagated = satellite.at(time)
    if propagated.message:
        raise ValueError(f"SGP4 propagation failed: {propagated.message}")
    position, velocity = propagated.frame_xyz_and_velocity(TEME)
    values = {"position_km": position.km.tolist(), "velocity_km_s": velocity.km_per_s.tolist()}
    for key, vector in values.items():
        for value in vector:
            _finite_number(value, f"propagated {key}")
    errors = {
        key: _finite_number(dist(values[key], state[key]), f"{key} residual") for key in values
    }
    return {
        "elapsed_s": state["elapsed_s"],
        **values,
        "reference_position_km": list(state["position_km"]),
        "reference_velocity_km_s": list(state["velocity_km_s"]),
        "position_error_km": errors["position_km"],
        "velocity_error_km_s": errors["velocity_km_s"],
        "passed": all(errors[key] <= tolerances[key] for key in errors),
    }


def verify_orbit_reference(config_path: str | Path) -> dict:
    """Check a bounded local fixture through OpenLEO's production catalog loader."""
    try:
        data, checksum = _reference(Path(config_path))
        original, omm_epoch, csv_bytes = _convert_tle(data)
        with TemporaryDirectory(prefix="openleo-orbit-reference-") as directory:
            satellite = _load_adapter(data, csv_bytes, directory)
        epochs = [_compare(satellite, state, data["tolerances"]) for state in data["states"]]
        epoch_shift_s = (
            (satellite.model.jdsatepoch - original.jdsatepoch)
            + (satellite.model.jdsatepochF - original.jdsatepochF)
        ) * 86400.0
        return {
            "schema_version": "1",
            "kind": "openleo.orbit-verification",
            "name": data["name"],
            "fixture_sha256": checksum,
            "source": dict(data["source"]),
            "converted_omm_sha256": sha256(csv_bytes).hexdigest(),
            "adapter": "openleo.catalog.load_catalog -> Skyfield TEME",
            "frame": "TEME",
            "units": dict(_UNITS),
            "time_convention": {
                "origin": "loaded SGP4 epoch",
                "elapsed_time_scale": "uniform 86400 s days",
                "published_scenario_epoch_utc": data["scenario_epoch_utc"],
                "converted_omm_epoch_utc": omm_epoch + "Z",
                "omm_epoch_shift_s": epoch_shift_s,
                "epoch_precision": "OMM microseconds; split Julian dates during propagation",
            },
            "tolerances": dict(data["tolerances"]),
            "epochs": epochs,
            "passed": all(epoch["passed"] for epoch in epochs),
            "software": {
                name: version(name) for name in ("openleo-link", "skyfield", "sgp4", "numpy")
            },
            "limitations": [
                "Numerical agreement with published SGP4 output, not independent physical truth.",
                "Both adapters share Vallado SGP4 theory and implementation lineage.",
                "Elapsed times start at the loaded epoch; the reported OMM epoch shift is not tested as absolute UTC accuracy.",
                "Fixture states are rounded to 1e-8 km and 1e-9 km/s per component.",
                "Source hashes were verified when building the fixture; no runtime source download.",
                "One near-Earth moderate-drag case; no deep-space, GCRS/ITRS, observation, Doppler or RF validation.",
            ],
        }
    except (UnicodeError, RecursionError, TypeError, KeyError, OverflowError) as exc:
        raise ValueError(f"invalid orbit reference: {exc}") from exc
