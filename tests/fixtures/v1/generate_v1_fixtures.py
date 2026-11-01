"""Freeze OpenLEO v1.0.0 numbers as gzip-JSON equivalence fixtures.

This script is NOT part of the 2.0 test suite and is never collected by pytest. It must
run inside an environment where the *v1.0.0* ``openleo`` package is installed (the read-only
checkout of tag ``v1.0.0``), because its only purpose is to record what v1 computes::

    cd /tmp/openleo-v1            # git worktree of tag v1.0.0
    uv sync --locked --no-dev --extra plot
    uv run --no-dev python <worktree>/tests/fixtures/v1/generate_v1_fixtures.py \\
        --output <worktree>/tests/fixtures/v1

See ``README.md`` next to this file. Floats are written exactly as ``json.dumps`` renders
them (``float.__repr__``, the shortest string that round-trips); nothing is rounded. Keys
are sorted and gzip headers carry ``mtime=0`` and no file name, so repeated runs on the same
interpreter and zlib produce identical bytes.
"""

from __future__ import annotations

import argparse
import contextlib
import gzip
import hashlib
import io
import json
import platform
import subprocess
import sys
import tempfile
import zlib
from dataclasses import asdict
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path
from typing import Any

import numpy as np
from skyfield.api import load, wgs84
from skyfield.framelib import itrs

import openleo
from openleo.cli import main as openleo_cli
from openleo.constellation import load_constellation, simulate_constellation
from openleo.input import load_scenario
from openleo.network import ROUTE_MODELS, add_network
from openleo.network_replay import NetworkReplayConfig, prepare_network_replay
from openleo.orbit import load_orbit
from openleo.packet_replay import ReplayConfig, prepare_packet_replay
from openleo.simulation import _timestamps, _validated_time_grid, simulate_scenario

EXPECTED_VERSION = "1.0.0"
EXPECTED_TAG = "v1.0.0"

KEPT_STATIONS = 3  # station indices 0-2
KEPT_SATELLITES = 10  # satellite indices 0-9

REPLAY_STATION = "Madrid"
REPLAY_SATELLITE_INDEX = 3  # NORAD id is looked up from this index
REPLAY_START_OFFSET_S = 0
REPLAY_DURATION_S = 300
REPLAY_PACKET_SIZE_BYTES = 1200
REPLAY_OFFERED_LOAD_BPS = 10_000
NETWORK_FLOWS_PER_DIRECTION = 2
NETWORK_ACQUISITION_DELAY_S = 0.1
REPLAY_FIXTURE = "iridium_global_hydrometeors"

# (fixture name, scenario path relative to the v1 checkout, kind)
FIXTURES = (
    ("iss_cartagena", "examples/scenarios/iss_cartagena.json", "pass"),
    ("iridium_global", "examples/constellations/iridium_global.json", "constellation"),
    (
        "iridium_global_reference",
        "examples/constellations/iridium_global_reference.json",
        "constellation",
    ),
    (
        "iridium_global_hydrometeors",
        "examples/constellations/iridium_global_hydrometeors.json",
        "constellation",
    ),
)

# CLI calculations whose output bytes are fingerprinted (command name -> argv tail).
CALC_COMMANDS = {
    "gases": "examples/atmosphere/p676_13_validation.json",
    "hydrometeors": "examples/atmosphere/hydrometeors_reference.json",
}


def _utc_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _canonical_json(value: Any) -> bytes:
    """Deterministic JSON: sorted keys, compact separators, ``repr`` floats, no NaN."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def _gzip_bytes(payload: bytes) -> bytes:
    buffer = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=buffer, compresslevel=9, mtime=0) as gz:
        gz.write(payload)
    return buffer.getvalue()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _v1_root() -> Path:
    root = Path(openleo.__file__).resolve().parents[2]
    if not (root / "examples").is_dir() or not (root / "pyproject.toml").is_file():
        raise SystemExit(f"{root} does not look like the v1.0.0 source checkout")
    return root


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _installed_version() -> str:
    # v1.0.0 exposes no ``openleo.__version__``; the distribution metadata is the source.
    return getattr(openleo, "__version__", None) or metadata.version("openleo-link")


def _file_sha256(path: Path) -> str:
    return _sha256(path.read_bytes())


def _constellation_fixture(scenario_path: Path, *, with_replays: bool) -> dict[str, Any]:
    scenario = load_constellation(scenario_path)
    document = add_network(simulate_constellation(scenario))

    satellites = [
        {
            "index": index,
            "norad_id": satellite["norad_id"],
            "positions_ecef_m": satellite["positions_ecef_m"],
        }
        for index, satellite in enumerate(document["satellites"])
        if index < KEPT_SATELLITES
    ]
    stations = [
        {"index": index, "name": station["name"], "position_ecef_m": station["position_ecef_m"]}
        for index, station in enumerate(document["stations"])
        if index < KEPT_STATIONS
    ]
    links = [
        {"time_index": time_index, **link}
        for time_index, frame in enumerate(document["links"])
        for link in frame
        if link["station_index"] < KEPT_STATIONS and link["satellite_index"] < KEPT_SATELLITES
    ]
    # One entry per (frame, model). A disconnected sample keeps its slot, with null fields.
    routes = []
    for time_index, frame in enumerate(document["network"]["frames"]):
        for model in ROUTE_MODELS:
            route = frame[model]
            routes.append(
                {
                    "time_index": time_index,
                    "model": model,
                    "nodes": route["nodes"] if route else None,
                    "delay_s": route["delay_s"] if route else None,
                    "bottleneck_bps": route["bottleneck_bps"] if route else None,
                }
            )

    fixture: dict[str, Any] = {
        "scenario": document["scenario"],
        "timestamps_utc": document["timestamps_utc"],
        "satellites": satellites,
        "stations": stations,
        "links": links,
        "station_statistics": document["statistics"],
        "network_summary": document["network"]["summary"],
        "routes": routes,
        "packet_replay_trace": None,
        "network_replay_prepared": None,
    }
    if with_replays:
        norad_id = document["satellites"][REPLAY_SATELLITE_INDEX]["norad_id"]
        fixture["packet_replay_trace"] = prepare_packet_replay(
            document,
            ReplayConfig(
                station_name=REPLAY_STATION,
                norad_id=norad_id,
                start_offset_s=REPLAY_START_OFFSET_S,
                duration_s=REPLAY_DURATION_S,
                offered_load_bps=REPLAY_OFFERED_LOAD_BPS,
                packet_size_bytes=REPLAY_PACKET_SIZE_BYTES,
            ),
        )
        fixture["network_replay_prepared"] = {
            model: prepare_network_replay(
                document,
                NetworkReplayConfig(
                    routing_model=model,
                    start_offset_s=REPLAY_START_OFFSET_S,
                    duration_s=REPLAY_DURATION_S,
                    offered_load_bps=REPLAY_OFFERED_LOAD_BPS,
                    packet_size_bytes=REPLAY_PACKET_SIZE_BYTES,
                    flows_per_direction=NETWORK_FLOWS_PER_DIRECTION,
                    acquisition_delay_s=NETWORK_ACQUISITION_DELAY_S,
                ),
            )
            for model in ROUTE_MODELS
        }
    return fixture


def _pass_fixture(scenario_path: Path) -> dict[str, Any]:
    """Single-pass scenario: v1 has no network or statistics layer for it, so those are null."""
    scenario = load_scenario(scenario_path)
    result = simulate_scenario(scenario)

    step, count = _validated_time_grid(scenario)
    timestamps = tuple(_timestamps(scenario, step, count))
    timescale = load.timescale(builtin=True)
    orbit = load_orbit(scenario.orbit, timescale)
    state = orbit.satellite.at(timescale.from_datetimes(timestamps))
    site = wgs84.latlon(
        scenario.ground_station.latitude_deg,
        scenario.ground_station.longitude_deg,
        elevation_m=scenario.ground_station.height_m,
    )

    index_of = {timestamp: index for index, timestamp in enumerate(timestamps)}
    trace_rows = []
    links = []
    for row in result.rows:
        fields = asdict(row)
        fields["timestamp_utc"] = _utc_text(row.timestamp_utc)
        trace_rows.append(fields)
        link = {key: value for key, value in fields.items() if key != "timestamp_utc"}
        links.append(
            {
                "time_index": index_of[row.timestamp_utc],
                "station_index": 0,
                "satellite_index": 0,
                **link,
            }
        )

    summary = asdict(result.summary)
    for key, value in summary.items():
        if isinstance(value, datetime):
            summary[key] = _utc_text(value)
    summary["warnings"] = list(summary["warnings"])

    return {
        "scenario": json.loads(scenario_path.read_text(encoding="utf-8")),
        "timestamps_utc": [_utc_text(timestamp) for timestamp in timestamps],
        "satellites": [
            {
                "index": 0,
                "norad_id": int(orbit.satellite.model.satnum),
                "positions_ecef_m": state.frame_xyz(itrs).m.T.tolist(),
            }
        ],
        "stations": [
            {
                "index": 0,
                "name": scenario.ground_station.name,
                "position_ecef_m": site.itrs_xyz.m.tolist(),
            }
        ],
        "links": links,
        "station_statistics": None,
        "network_summary": None,
        "routes": None,
        "packet_replay_trace": None,
        "network_replay_prepared": None,
        "trace_rows": trace_rows,
        "summary": summary,
    }


def _calc_output_hashes(root: Path) -> tuple[dict[str, dict[str, str]], dict[str, str]]:
    """Run the v1 ``gases`` and ``hydrometeors`` commands; hash every file they write."""
    hashes: dict[str, dict[str, str]] = {}
    commands: dict[str, str] = {}
    for command, config in CALC_COMMANDS.items():
        with tempfile.TemporaryDirectory(prefix=f"openleo-v1-{command}-") as temp:
            output = Path(temp) / "out"
            with contextlib.redirect_stdout(io.StringIO()):
                status = openleo_cli([command, str(root / config), "--output", str(output)])
            if status != 0:
                raise SystemExit(f"openleo {command} failed with status {status}")
            hashes[command] = {
                path.relative_to(output).as_posix(): _file_sha256(path)
                for path in sorted(output.rglob("*"))
                if path.is_file()
            }
        commands[command] = f"openleo {command} {config} --output <DIRECTORY>"
    return hashes, commands


def generate(output: Path) -> dict[str, Any]:
    version = _installed_version()
    if version != EXPECTED_VERSION:
        raise SystemExit(f"expected openleo {EXPECTED_VERSION}, found {version}")
    root = _v1_root()
    commit = _git(root, "rev-parse", "HEAD")
    tag = _git(root, "describe", "--tags", "--exact-match", "HEAD")
    if tag != EXPECTED_TAG:
        raise SystemExit(f"checkout must be tag {EXPECTED_TAG}, found {tag}")
    if _git(root, "status", "--porcelain", "--untracked-files=no"):
        raise SystemExit("the v1.0.0 checkout has tracked modifications")

    output.mkdir(parents=True, exist_ok=True)
    gzip_sha256: dict[str, str] = {}
    json_sha256: dict[str, str] = {}
    sources: dict[str, dict[str, str]] = {}
    for name, relative, kind in FIXTURES:
        path = root / relative
        fixture = (
            _pass_fixture(path)
            if kind == "pass"
            else _constellation_fixture(path, with_replays=name == REPLAY_FIXTURE)
        )
        payload = _canonical_json(fixture)
        compressed = _gzip_bytes(payload)
        file_name = f"{name}.json.gz"
        (output / file_name).write_bytes(compressed)
        gzip_sha256[file_name] = _sha256(compressed)
        json_sha256[file_name] = _sha256(payload)
        sources[file_name] = {"scenario": relative, "scenario_sha256": _file_sha256(path)}
        print(f"{file_name}: {len(compressed)} bytes gzip, {len(payload)} bytes json")

    calc_outputs, calc_commands = _calc_output_hashes(root)
    manifest = {
        "tag": tag,
        "commit": commit,
        "openleo_link": version,
        "python": platform.python_version(),
        "numpy": np.__version__,
        "skyfield": metadata.version("skyfield"),
        "sgp4": metadata.version("sgp4"),
        "zlib": zlib.ZLIB_RUNTIME_VERSION,
        "fixtures": gzip_sha256,
        "fixture_json_sha256": json_sha256,
        "fixture_sources": sources,
        "calc_commands": calc_commands,
        "calc_outputs": calc_outputs,
    }
    (output / "metadata.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(f"metadata.json: {output / 'metadata.json'}")
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", required=True, type=Path, metavar="DIR")
    args = parser.parse_args(argv)
    generate(args.output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
