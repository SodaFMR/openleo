"""Bounded, input-driven rain/cloud calculations and portable reference reports."""

from __future__ import annotations

import json
import re
from dataclasses import asdict
from hashlib import sha256
from html import escape
from importlib.metadata import version
from pathlib import Path
from tempfile import TemporaryDirectory

from openleo.hydrometeors import (
    CLOUD_SOURCE,
    RAIN_SOURCE,
    cloud_slant_attenuation,
    rain_specific_attenuation,
)
from openleo.input import _object, _string
from openleo.workbench import _csv, _json, _read, _unique_object

_RAIN_INPUTS = ("frequency_hz", "rain_rate_mm_h", "elevation_deg", "polarization_tilt_deg")
_CLOUD_INPUTS = ("frequency_hz", "liquid_water_kg_m2", "elevation_deg")
_RAIN_FIELDS = ("id", *_RAIN_INPUTS, "k", "alpha", "specific_attenuation_db_per_km")
_CLOUD_FIELDS = ("id", *_CLOUD_INPUTS, "mass_absorption_db_per_kg_m2", "attenuation_db")
_LIMITATIONS = [
    "Declared reference or user-supplied hydrometeor inputs, not collected local weather.",
    "Rain results are specific attenuation in dB/km, not Earth-space path loss or availability.",
    "Cloud slant loss requires an explicitly supplied integrated liquid-water column; no maps or exceedance probability are inferred.",
    "These independent calculations do not modify constellation C/N0, receiver noise or packet results.",
]


def _calculate(path: Path) -> dict:
    raw = _read(path, 1_000_000)
    try:
        data = json.loads(raw, object_pairs_hook=_unique_object)
        _object(
            data, frozenset(("schema_version", "name", "rain_cases", "cloud_cases")), "hydrometeors"
        )
        if data["schema_version"] != "1":
            raise ValueError("unsupported hydrometeor schema_version")
        name = _string(data["name"], "name")
        groups = (
            ("rain_cases", _RAIN_INPUTS, rain_specific_attenuation),
            ("cloud_cases", _CLOUD_INPUTS, cloud_slant_attenuation),
        )
        calculated, identifiers = {}, set()
        for key, inputs, function in groups:
            cases = data[key]
            if not isinstance(cases, list) or len(cases) > 32:
                raise ValueError(f"{key} must be a list of at most 32 cases")
            rows = []
            for case in cases:
                _object(case, frozenset(("id", *inputs)), key)
                identifier = case["id"]
                if not isinstance(identifier, str) or not re.fullmatch(
                    r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", identifier
                ):
                    raise ValueError("case id must be a portable alphanumeric identifier")
                if identifier in identifiers:
                    raise ValueError("hydrometeor case ids must be unique")
                identifiers.add(identifier)
                result = function(**{field: case[field] for field in inputs})
                rows.append({**case, **asdict(result)})
            calculated[key] = rows
        if not identifiers:
            raise ValueError("at least one rain or cloud case is required")
        return {
            "schema_version": "1",
            "kind": "openleo.hydrometeors",
            "name": name,
            "provenance": {
                "configuration_sha256": sha256(raw).hexdigest(),
                "software": {"openleo-link": version("openleo-link")},
            },
            "models": {"rain": dict(RAIN_SOURCE), "cloud": dict(CLOUD_SOURCE)},
            **calculated,
            "limitations": list(_LIMITATIONS),
        }
    except (UnicodeError, RecursionError, TypeError, KeyError, OverflowError) as exc:
        raise ValueError("invalid hydrometeor configuration") from exc


def _table(rows, fields, empty_message):
    if not rows:
        return f"<p>{escape(empty_message)}</p>"
    headings = "".join(f'<th scope="col">{escape(field)}</th>' for field in fields)
    body = "".join(
        "<tr>"
        + "".join(
            "<td>"
            + escape(
                format(row[field], ".12g")
                if isinstance(row[field], (int, float))
                else str(row[field])
            )
            + "</td>"
            for field in fields
        )
        + "</tr>"
        for row in rows
    )
    return f'<div class="table"><table><thead><tr>{headings}</tr></thead><tbody>{body}</tbody></table></div>'


def _report(result):
    name = escape(result["name"])
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; script-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'\">"
        f"<title>{name} · OpenLEO</title>"
        "<style>body{font:15px system-ui,sans-serif;color:#24313e;background:#f5f6f7;margin:2rem auto;max-width:1200px;padding:0 1rem}h1{font-size:1.5rem}h2{font-size:1.1rem}section{background:white;border:1px solid #cdd3d8;padding:1rem;margin:1rem 0}.table{overflow-x:auto}table{border-collapse:collapse;white-space:nowrap}th,td{padding:.6rem;text-align:right;border-bottom:1px solid #dde2e6}th{background:#eef1f4}th:first-child,td:first-child{text-align:left}code{overflow-wrap:anywhere}li{margin:.5rem 0}p{line-height:1.5}</style></head><body>"
        f"<h1>{name}</h1><p>Input-driven ITU reference calculations. Rain and cloud results are different physical quantities and are not combined.</p>"
        "<section><h2>Specific rain attenuation (dB/km)</h2>"
        + _table(result["rain_cases"], _RAIN_FIELDS, "No rain cases were requested.")
        + "</section><section><h2>Cloud slant attenuation (dB)</h2>"
        + _table(result["cloud_cases"], _CLOUD_FIELDS, "No cloud cases were requested.")
        + "</section><section><h2>Sources and interpretation</h2><ul>"
        + "".join(f"<li>{escape(text)}</li>" for text in result["limitations"])
        + "</ul><p>Configuration SHA-256: <code>"
        + result["provenance"]["configuration_sha256"]
        + '</code></p><p>Full source versions and fingerprints: <a href="hydrometeors.json">JSON results</a>.</p></section></body></html>\n'
    )


def run_hydrometeors(config_path: str | Path, output_dir: str | Path) -> dict:
    """Calculate declared cases and atomically publish a new bounded result bundle."""
    result = _calculate(Path(config_path))
    directory = Path(output_dir)
    if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
        raise ValueError("output must be a directory, not a file or symlink")
    if directory.exists() and any(directory.iterdir()):
        raise ValueError("refusing to replace a nonempty output directory")
    contents = {
        "rain.csv": _csv(_RAIN_FIELDS, result["rain_cases"]),
        "cloud.csv": _csv(_CLOUD_FIELDS, result["cloud_cases"]),
        "hydrometeors.json": _json(result) + "\n",
        "index.html": _report(result),
    }
    manifest = {
        "schema_version": "1",
        "kind": "openleo.hydrometeors-bundle",
        "files": {
            name: sha256(content.encode("utf-8")).hexdigest() for name, content in contents.items()
        },
    }
    directory.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=f".{directory.name}-", dir=directory.parent) as temp:
        stage = Path(temp) / "result"
        stage.mkdir()
        for name, content in {**contents, "manifest.json": _json(manifest) + "\n"}.items():
            (stage / name).write_text(content, encoding="utf-8", newline="\n")
        if directory.is_symlink():
            raise ValueError("output cannot be a symlink")
        if directory.exists():
            directory.rmdir()  # Refuses nonempty directories, including concurrent writes.
        stage.replace(directory)
    return result
