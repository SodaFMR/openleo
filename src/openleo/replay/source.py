"""Link, edge and route state of a saved run at any offset, for the packet replays.

``ReplaySource.from_results`` loads a results directory (verifying its manifest), checks that the
archived ``inputs/`` are the ones ``results.json`` records, and rebuilds the engine's geometry
and link evaluator from them. It then answers, at any offset ``t`` in seconds after
``time.start_utc``:

* ``link_state(station, satellite, t)``: ``(rate_bps, delay_s)``, or None when the pair is not
  visible. Visibility is the pair's passes ``[aos, los)`` from the ``passes`` table, and the
  rate that of the ``rate_segments`` row holding ``t`` (0 when none does), as the engine's
  routing reads them (``openleo.engine.routing.link_state_from_pairs``). The window stop holds
  no time; there a pass truncated by the window end is visible at the rate of the segment that
  ends there. The delay is the link evaluator's at ``t``, as the engine's routing evaluates it
  at its breakpoints: with events on at most ``tolerance_s`` before a root-found LOS (the link
  is still above the mask there); with events off at ``t`` itself, so on grid samples it is
  v1's sampled delay. With events off, visibility is held between grid samples (v1), so a pair
  can count as visible at an off-grid ``t`` where the satellite is already below the mask and
  the link budget is undefined; only there the delay is held from the grid sample at or before
  ``t``.
* ``route(pair_index, model, t)``: the node sequence of the ``routes`` row holding ``t``, or
  None while disconnected.
* ``edge_state(a, b, t, fixed)``: an inter-satellite link (``a < b < N``) is
  ``(isl.capacity_bps, r/c)`` when the satellites' range ``r`` satisfies the engine's rule
  (``0 < r <= isl.max_range_m`` with a clear WGS84 line of sight), from positions at ``t``,
  as the engine's routing builds its graph at a breakpoint; a ground link
  (satellite ``a``, station node ``b = N + k``) is ``link_state(k, a, t)`` with the rate
  replaced by ``fixed_capacity_bps`` when ``fixed``.
* ``breakpoints(start, stop, ...)``: the times in ``[start, stop]`` where those states may
  change: the grid plus the pass and rate-segment boundaries of one pair, or, for a route pair,
  the engine's route breakpoints (the grid plus the boundaries of every pair whose station is a
  route endpoint) and the ``routes`` interval boundaries.

Errors are ``ValueError``.
"""

from __future__ import annotations

import copy
import json
import math
from bisect import bisect_right
from collections import defaultdict
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray

from openleo.engine._assembly import canonical_sha256
from openleo.engine.catalogs import load_catalogs
from openleo.engine.evaluator import LinkEvaluator
from openleo.engine.geometry import Geometry
from openleo.engine.routing import _clear_segment, _scaled
from openleo.engine.timegrid import build_grid, builtin_timescale
from openleo.physics.link import propagation_delay_s
from openleo.results import load_results, manifest_digest, verify_manifest
from openleo.results.manifest import read_manifest, sha256_file
from openleo.scenario import load_scenario
from openleo.scenario.model import ROUTE_MODELS

_HASH_KEYS = ("source_sha256", "normalised_sha256")


def _index(value: Any, count: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int | np.integer) or not 0 <= value < count:
        raise ValueError(f"{name}={value!r} must be an integer in 0..{count - 1}")
    return int(value)


def _verified_inputs(directory: Path, metadata: dict) -> Path:
    """The archived scenario path, once it and the catalogs match ``results.json``."""
    manifest = read_manifest(directory)
    if "inputs/scenario.json" not in manifest:
        raise ValueError(f"{directory} has no archived inputs/scenario.json to replay from")
    path = directory / "inputs" / "scenario.json"
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        block = metadata["scenario"]
        recorded = {key: value for key, value in block.items() if key not in _HASH_KEYS}
        catalogs = metadata["provenance"]["catalogs"]
        archived = document["space_segment"]["catalogs"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ValueError(f"{directory}: unreadable archived scenario or provenance: {exc}") from exc
    if not isinstance(archived, list) or len(archived) != len(catalogs):
        raise ValueError("inputs/scenario.json does not list the catalogs results.json records")
    restored = copy.deepcopy(document)
    for entry, record in zip(restored["space_segment"]["catalogs"], catalogs, strict=True):
        if entry.get("path") != record.get("archived_path"):
            raise ValueError(
                f"inputs/scenario.json catalog path {entry.get('path')!r} is not the archived "
                f"path {record.get('archived_path')!r} recorded in results.json"
            )
        entry["path"] = record["path"]
    if restored != recorded or canonical_sha256(restored) != block.get("normalised_sha256"):
        raise ValueError(
            "inputs/scenario.json does not match the scenario recorded in results.json "
            "(normalised_sha256)"
        )
    for record in catalogs:
        name = f"inputs/{record['archived_path']}"
        if name not in manifest:
            raise ValueError(f"{directory} has no archived catalog {name}")
        actual = sha256_file(directory / name)
        if actual != record["sha256"]:
            raise ValueError(
                f"archived catalog {name}: sha256 {actual} differs from the provenance "
                f"sha256 {record['sha256']} in results.json"
            )
    return path


class ReplaySource:
    """The state of one saved run at any offset (module docstring). Build it with
    ``from_results``."""

    def __init__(self, directory: Path, results, scenario, grid, orbits, geometry) -> None:
        self.directory = directory
        self.results = results
        self.scenario = scenario
        self.geometry = geometry
        self.evaluator = LinkEvaluator(scenario, geometry)
        self.manifest_sha256 = manifest_digest(directory)
        self.start_utc: datetime = grid.start_utc
        self.grid_s: NDArray[np.float64] = grid.offsets_s
        self.window_s = float(grid.offsets_s[-1])
        self.station_names = tuple(s.name for s in scenario.ground_segment.stations)
        self.norad_ids = tuple(int(o.satellite.model.satnum) for o in orbits)
        self.satellite_count = len(self.norad_ids)
        self.events_enabled = bool(scenario.time.events.enabled)
        self.tolerance_s = float(scenario.time.events.tolerance_s)
        default = scenario.ground_segment.default_minimum_elevation_deg
        self._masks = tuple(
            default if s.minimum_elevation_deg is None else s.minimum_elevation_deg
            for s in scenario.ground_segment.stations
        )
        network = scenario.network
        self.network = network
        if network is None:
            self.route_pairs: tuple[tuple[int, int], ...] = ()
            self.route_models: tuple[str, ...] = ()
            self.fixed_capacity_bps = None
            self.isl_capacity_bps = None
        else:
            self.route_pairs = tuple(
                (self.station_names.index(p.source), self.station_names.index(p.target))
                for p in network.pairs
            )
            self.route_models = tuple(network.route_models)
            self.fixed_capacity_bps = network.fixed_capacity_bps
            self.isl_capacity_bps = float(network.isl.capacity_bps)
        self._check_tables()
        self._passes = self._pass_tracks()
        self._segments = self._segment_tracks()
        self._routes = self._route_tracks()
        self._delays: dict[tuple[int, int, float], float] = {}
        self._positions: dict[tuple[int, float], list[float]] = {}

    @classmethod
    def from_results(cls, directory: Path) -> ReplaySource:
        """Load and verify a results directory and rebuild the engine from its inputs."""
        directory = Path(directory)
        results = load_results(directory)
        metadata = results.metadata
        if metadata.get("kind") != "openleo.results" or metadata.get("schema_version") != "2":
            raise ValueError(f"{directory} is not an OpenLEO results v2 directory")
        scenario = load_scenario(_verified_inputs(directory, metadata))
        events = metadata.get("events")
        if not isinstance(events, dict) or events.get("enabled") != scenario.time.events.enabled:
            raise ValueError("results.json events.enabled differs from the archived scenario")
        timescale = builtin_timescale()
        grid = build_grid(scenario.time)
        orbits = load_catalogs(scenario.space_segment, timescale)
        geometry = Geometry(orbits, scenario.ground_segment.stations, timescale, grid.start_utc)
        return cls(directory, results, scenario, grid, orbits, geometry)

    # --- tables ---------------------------------------------------------------------------------

    def _check_tables(self) -> None:
        tables = self.results.tables
        needed = ["satellites", "stations", "passes", "rate_segments"]
        if self.network is not None:
            needed.append("routes")
        missing = [name for name in needed if name not in tables]
        if missing:
            raise ValueError(f"results lack the tables {missing} a replay reads")
        satellites = tables["satellites"]
        if tuple(int(n) for n in satellites["norad_id"].tolist()) != self.norad_ids:
            raise ValueError("the satellites table differs from the archived catalogs")
        if tuple(tables["stations"]["name"]) != self.station_names:
            raise ValueError("the stations table differs from the archived scenario")

    def _pass_tracks(self) -> dict:
        table = self.results.tables["passes"]
        tracks: dict[tuple[int, int], list] = defaultdict(list)
        for station, satellite, start, end, truncated in zip(
            table["station_index"].tolist(),
            table["satellite_index"].tolist(),
            table["aos_offset_s"].tolist(),
            table["los_offset_s"].tolist(),
            table["truncated_end"].tolist(),
            strict=True,
        ):
            tracks[(station, satellite)].append((start, end, bool(truncated)))
        return {key: sorted(value) for key, value in tracks.items()}

    def _segment_tracks(self) -> dict:
        table = self.results.tables["rate_segments"]
        tracks: dict[tuple[int, int], list] = defaultdict(list)
        for station, satellite, start, end, rate in zip(
            table["station_index"].tolist(),
            table["satellite_index"].tolist(),
            table["start_offset_s"].tolist(),
            table["end_offset_s"].tolist(),
            table["rate_bps"].tolist(),
            strict=True,
        ):
            tracks[(station, satellite)].append((start, end, rate))
        return {
            key: tuple(map(list, zip(*sorted(value), strict=True))) for key, value in tracks.items()
        }

    def _route_tracks(self) -> dict:
        if self.network is None:
            return {}
        table = self.results.tables["routes"]
        tracks: dict[tuple[int, str], list] = defaultdict(list)
        for pair, model, start, end, connected, nodes in zip(
            table["pair_index"].tolist(),
            table["model"],
            table["start_offset_s"].tolist(),
            table["end_offset_s"].tolist(),
            table["connected"].tolist(),
            table["nodes"],
            strict=True,
        ):
            path = tuple(int(node) for node in nodes.split()) if connected else None
            tracks[(pair, model)].append((start, end, path))
        return {
            key: tuple(map(list, zip(*sorted(value, key=lambda r: r[0]), strict=True)))
            for key, value in tracks.items()
        }

    # --- queries --------------------------------------------------------------------------------

    def _time(self, t_s: Any) -> float:
        try:
            t = float(t_s)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"t_s={t_s!r} must be a number of seconds") from exc
        if not (math.isfinite(t) and 0.0 <= t <= self.window_s):
            raise ValueError(f"t_s={t_s!r} is outside the results window [0, {self.window_s!r}] s")
        return t

    def _grid_sample(self, t: float) -> float:
        """The grid sample at or before t."""
        return float(self.grid_s[bisect_right(self.grid_s, t) - 1])

    def _pass_at(self, station: int, satellite: int, t: float):
        passes = self._passes.get((station, satellite), ())
        index = bisect_right(passes, (t, math.inf, True)) - 1
        if index < 0:
            return None
        start, end, truncated = passes[index]
        if t < end or (t == end == self.window_s and truncated):
            return start, end, truncated
        return None

    def link_state(
        self, station_index: int, satellite_index: int, t_s: float
    ) -> tuple[float, float] | None:
        """(rate_bps, delay_s) of a pair at t_s, or None when it is not visible."""
        station = _index(station_index, len(self.station_names), "station_index")
        satellite = _index(satellite_index, self.satellite_count, "satellite_index")
        t = self._time(t_s)
        found = self._pass_at(station, satellite, t)
        if found is None:
            return None
        start, end, truncated = found
        rate = 0.0
        track = self._segments.get((station, satellite))
        if track is not None:
            starts, ends, rates = track
            k = bisect_right(starts, t) - 1
            if k >= 0 and (t < ends[k] or (t == ends[k] == self.window_s and starts[k] < t)):
                rate = rates[k]
        if self.events_enabled:
            last = end if truncated else max(start, end - self.tolerance_s)
            query = min(t, last)
        else:
            query = t
            if t != self._grid_sample(t):
                elevation = self.geometry.elevation_deg(station, satellite, np.array([t]))[0]
                if not elevation >= self._masks[station]:
                    query = self._grid_sample(t)
        return rate, self._delay(station, satellite, query)

    def _delay(self, station: int, satellite: int, t: float) -> float:
        key = (station, satellite, t)
        if key not in self._delays:
            values = self.evaluator.evaluate(station, satellite, np.array([t]))
            self._delays[key] = float(values["delay_s"][0])
        return self._delays[key]

    def _check_route(self, pair_index: Any, model: Any) -> tuple[int, str | None]:
        if self.network is None:
            raise ValueError("these results have no network: there are no routes to replay")
        pair = _index(pair_index, len(self.route_pairs), "pair_index")
        if model is not None and model not in self.route_models:
            raise ValueError(
                f"model={model!r} is not among the results' network.route_models "
                f"{list(self.route_models)}"
            )
        return pair, model

    def route(self, pair_index: int, model: str, t_s: float) -> tuple[int, ...] | None:
        """The route's node sequence at t_s, or None while the pair is disconnected."""
        pair, model = self._check_route(pair_index, model)
        if model is None:
            raise ValueError("model must name one of the results' network.route_models")
        t = self._time(t_s)
        track = self._routes.get((pair, model))
        if track is None:
            return None
        starts, ends, paths = track
        k = bisect_right(starts, t) - 1
        if k < 0 or t >= ends[k]:
            return None
        return paths[k]

    def _position(self, satellite: int, t: float) -> list[float]:
        key = (satellite, t)
        if key not in self._positions:
            self._positions[key] = self.geometry.satellite_ecef(satellite, [t])[0][0].tolist()
        return self._positions[key]

    def edge_state(self, a: int, b: int, t_s: float, fixed: bool) -> tuple[float, float] | None:
        """(capacity_bps, delay_s) of the undirected edge a-b (a < b) at t_s, or None."""
        count = self.satellite_count
        nodes = count + len(self.station_names)
        a = _index(a, nodes, "edge node a")
        b = _index(b, nodes, "edge node b")
        if a >= b:
            raise ValueError(f"edge ({a}, {b}) must name its nodes in increasing order")
        if a >= count:
            raise ValueError(f"edge ({a}, {b}) must join at least one satellite")
        if self.network is None:
            raise ValueError("these results have no network: there are no edges to replay")
        t = self._time(t_s)
        if b < count:
            first, second = self._position(a, t), self._position(b, t)
            range_m = math.dist(first, second)
            if 0.0 < range_m <= self.network.isl.max_range_m and _clear_segment(
                _scaled(first), _scaled(second)
            ):
                return self.isl_capacity_bps, propagation_delay_s(range_m)
            return None
        state = self.link_state(b - count, a, t)
        if state is None:
            return None
        if fixed:
            if self.fixed_capacity_bps is None:
                raise ValueError("these results declare no network.fixed_capacity_bps")
            return float(self.fixed_capacity_bps), state[1]
        return state

    def breakpoints(
        self,
        start_s: float,
        stop_s: float,
        *,
        station_index: int | None = None,
        satellite_index: int | None = None,
        pair_index: int | None = None,
        model: str | None = None,
    ) -> NDArray[np.float64]:
        """Sorted unique times in [start_s, stop_s] where the selected state may change."""
        start, stop = self._time(start_s), self._time(stop_s)
        if start > stop:
            raise ValueError(f"start_s={start_s!r} must not exceed stop_s={stop_s!r}")
        pair_selected = station_index is not None or satellite_index is not None
        if pair_selected and (station_index is None or satellite_index is None):
            raise ValueError("a link selection needs both station_index and satellite_index")
        if pair_selected and (pair_index is not None or model is not None):
            raise ValueError("select either a link or a route pair, not both")
        if model is not None and pair_index is None:
            raise ValueError("a route model needs its pair_index")
        times = [self.grid_s]
        if pair_selected:
            station = _index(station_index, len(self.station_names), "station_index")
            satellite = _index(satellite_index, self.satellite_count, "satellite_index")
            keys = [(station, satellite)]
        elif pair_index is not None:
            pair, model = self._check_route(pair_index, model)
            endpoints = {s for p in self.route_pairs for s in p}
            keys = [(s, n) for s in sorted(endpoints) for n in range(self.satellite_count)]
            routes = [
                track
                for (index, name), track in self._routes.items()
                if index == pair and model in (None, name)
            ]
            times += [np.array(track[0] + track[1], dtype=np.float64) for track in routes]
        else:
            keys = sorted(set(self._passes) | set(self._segments))
        for key in keys:
            bounds = [t for p in self._passes.get(key, ()) for t in p[:2]]
            segments = self._segments.get(key)
            if segments is not None:
                bounds += segments[0] + segments[1]
            times.append(np.array(bounds, dtype=np.float64))
        union = np.unique(np.concatenate(times))
        return union[(union >= start) & (union <= stop)]

    # --- provenance -----------------------------------------------------------------------------

    def provenance(self) -> dict[str, Any]:
        """The hashes that bind a replay to these results; ValueError if the directory changed
        since ``from_results`` loaded it."""
        verify_manifest(self.directory)
        if manifest_digest(self.directory) != self.manifest_sha256:
            raise ValueError("the results directory changed while the replay was being prepared")
        metadata = self.results.metadata
        return {
            "results": {
                "manifest_sha256": self.manifest_sha256,
                "results_json_sha256": sha256(
                    (self.directory / "results.json").read_bytes()
                ).hexdigest(),
                "schema_version": metadata["schema_version"],
                "events_enabled": self.events_enabled,
            },
            "scenario": {key: metadata["scenario"][key] for key in _HASH_KEYS},
            "catalogs": [
                {"archived_path": c["archived_path"], "sha256": c["sha256"]}
                for c in metadata["provenance"]["catalogs"]
            ],
        }


__all__ = ["ROUTE_MODELS", "ReplaySource"]
