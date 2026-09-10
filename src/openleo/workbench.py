"""Portable research bundles and a self-contained browser workbench."""

from __future__ import annotations

import base64
import csv
import json
from hashlib import sha256
from importlib.metadata import version
from importlib.resources import files
from io import StringIO
from itertools import pairwise
from pathlib import Path
from string import Template

from openleo.adaptation import MODCODS
from openleo.constellation import parse_constellation
from openleo.input import (
    GROUND_STATION_KEYS,
    _finite_number,
    _ground_station,
    _non_negative_number,
    _object,
    _positive_number,
    _range,
    _sha256,
    _string,
    _url,
    _utc,
)
from openleo.network import ROUTE_MODELS, _ground_edges, _list, _position

MAX_ARTIFACT_BYTES = 64_000_000
ARTIFACT_NAMES = ("experiment.json", "links.csv", "routes.csv", "explorer.html")
LINK_FIELDS = (
    "timestamp_utc",
    "station_index",
    "satellite_index",
    "azimuth_deg",
    "elevation_deg",
    "range_m",
    "range_rate_mps",
    "doppler_hz",
    "delay_s",
    "cn0_db_hz",
    "snr_db",
    "esn0_db",
    "modcod",
    "rate_bps",
    "shannon_upper_bound_bps",
    "margin_db",
    "remaining_contact_s",
    "contact_truncated",
)
ROUTE_FIELDS = ("timestamp_utc", "model", "connected", "nodes", "delay_s", "bottleneck_bps")
DOCUMENT_KEYS = frozenset(
    (
        "schema_version",
        "kind",
        "scenario",
        "provenance",
        "models",
        "warnings",
        "limitations",
        "timestamps_utc",
        "satellites",
        "stations",
        "links",
        "statistics",
        "network",
    )
)


def _json(value) -> str:
    return json.dumps(
        value, allow_nan=False, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _embedded_json(value) -> str:
    return (
        _json(value)
        .replace("&", r"\u0026")
        .replace("<", r"\u003c")
        .replace(">", r"\u003e")
        .replace("\u2028", r"\u2028")
        .replace("\u2029", r"\u2029")
    )


def _integer(value, path: str, maximum: int, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{path} must be an integer in {minimum}..{maximum}")
    return value


def _validate_metadata(document) -> set[str]:
    models = _object(
        document["models"],
        frozenset(
            ("coordinates", "time", "radio_link", "adaptation", "contacts", "statistics", "network")
        ),
        "models",
    )
    for name, section in models.items():
        if not isinstance(section, dict) or not section:
            raise ValueError(f"models.{name} must be a non-empty object")
    coordinates = models["coordinates"]
    if (coordinates.get("frame"), coordinates.get("units"), coordinates.get("ellipsoid")) != (
        "ITRS",
        "metres",
        "WGS84",
    ):
        raise ValueError("models.coordinates must declare ITRS metres on WGS84")
    for field in ("equatorial_radius_m", "inverse_flattening"):
        _positive_number(coordinates[field], f"models.coordinates.{field}")
    table = _list(
        models["adaptation"]["modcod_table"], "models.adaptation.modcod_table", 1, len(MODCODS)
    )
    names = set()
    for entry in table:
        _object(
            entry, frozenset(("name", "efficiency_bits_per_symbol", "required_esn0_db")), "modcod"
        )
        name = _string(entry["name"], "modcod.name")
        if name in names:
            raise ValueError("models.adaptation.modcod_table names must be unique")
        names.add(name)
        _positive_number(entry["efficiency_bits_per_symbol"], "modcod.efficiency_bits_per_symbol")
        _finite_number(entry["required_esn0_db"], "modcod.required_esn0_db")
    provenance = document["provenance"]
    _sha256(provenance["scenario_sha256"], "provenance.scenario_sha256")
    if (
        "scenario_canonical_json" in provenance
        or provenance.get("scenario_hash_encoding") == "canonical JSON sorted compact UTF-8"
    ):
        canonical = provenance.get("scenario_canonical_json")
        if (
            not isinstance(canonical, str)
            or sha256(canonical.encode("utf-8")).hexdigest() != provenance["scenario_sha256"]
        ):
            raise ValueError("canonical scenario text must match scenario_sha256")
        parsed = json.loads(canonical, object_pairs_hook=_unique_object)
        parse_constellation(parsed, Path("experiment.json"), provenance["scenario_sha256"])
        if parsed != document["scenario"]:
            raise ValueError("canonical scenario text must describe this experiment scenario")
    orbit = provenance["orbit"]
    for field in ("source_url", "terms_url"):
        _url(orbit[field], f"provenance.orbit.{field}")
    _utc(orbit["retrieved_at_utc"], "provenance.orbit.retrieved_at_utc")
    _sha256(orbit["sha256"], "provenance.orbit.sha256")
    for package in ("openleo-link", "skyfield", "sgp4", "numpy"):
        _string(provenance["software"][package], f"provenance.software.{package}")
    for field in ("warnings", "limitations"):
        for message in _list(document[field], field, 0, 128):
            _string(message, field)
    return names


def _validate_geometry(satellites, stations, sample_count, scenario) -> None:
    identifiers = []
    for satellite in satellites:
        _object(
            satellite,
            frozenset(
                (
                    "name",
                    "norad_id",
                    "epoch_utc",
                    "maximum_absolute_element_age_days",
                    "positions_ecef_m",
                )
            ),
            "satellite",
        )
        _string(satellite["name"], "satellite.name")
        identifiers.append(_integer(satellite["norad_id"], "satellite.norad_id", 2**53 - 1, 1))
        _utc(satellite["epoch_utc"], "satellite.epoch_utc")
        _non_negative_number(
            satellite["maximum_absolute_element_age_days"], "satellite.element_age_days"
        )
        for position in _list(
            satellite["positions_ecef_m"], "positions_ecef_m", sample_count, sample_count
        ):
            _position(position, "satellite.positions_ecef_m")
    if identifiers != sorted(set(identifiers)):
        raise ValueError("satellites must have unique, numerically ordered NORAD IDs")
    if len(stations) != len(scenario.stations):
        raise ValueError("stations must match the scenario")
    for station, expected in zip(stations, scenario.stations, strict=True):
        _object(station, GROUND_STATION_KEYS | {"position_ecef_m"}, "station")
        fields = {key: station[key] for key in GROUND_STATION_KEYS}
        if _ground_station(fields) != expected:
            raise ValueError("station fields must match the scenario")
        _position(station["position_ecef_m"], "station.position_ecef_m")


def _validate_links(frames, satellite_count, station_count, timestamps, modcod_names, mask):
    ground_frames = []
    for index, frame in enumerate(frames):
        ground_frames.append(_ground_edges(frame, satellite_count, station_count))
        for link in frame:
            _object(link, frozenset(LINK_FIELDS[1:]), "link")
            _range(link["elevation_deg"], "link.elevation_deg", mask, 90.0)
            _range(link["azimuth_deg"], "link.azimuth_deg", 0.0, 360.0, upper_inclusive=False)
            _positive_number(link["range_m"], "link.range_m")
            for field in (
                "range_rate_mps",
                "doppler_hz",
                "cn0_db_hz",
                "snr_db",
                "esn0_db",
                "margin_db",
            ):
                _finite_number(link[field], f"link.{field}")
            _non_negative_number(link["shannon_upper_bound_bps"], "link.shannon_upper_bound_bps")
            _range(
                link["remaining_contact_s"],
                "link.remaining_contact_s",
                0.0,
                (timestamps[-1] - timestamps[index]).total_seconds(),
            )
            if not isinstance(link["contact_truncated"], bool):
                raise ValueError("link.contact_truncated must be a boolean")  # noqa: TRY004
            modcod, rate, margin = link["modcod"], link["rate_bps"], link["margin_db"]
            if modcod is None:
                if rate != 0.0 or margin >= 0.0:
                    raise ValueError("unlocked link must have zero rate and negative margin")
            elif (
                not isinstance(modcod, str)
                or modcod not in modcod_names
                or rate <= 0.0
                or margin < 0.0
            ):
                raise ValueError(
                    "locked link needs a declared MODCOD, positive rate and non-negative margin"
                )
    return ground_frames


def _validate_route(route, source, target, satellite_count, edges) -> None:
    if route is None:
        return
    _object(route, frozenset(("nodes", "delay_s", "bottleneck_bps")), "route")
    nodes = _list(route["nodes"], "route.nodes", 3, satellite_count + 2)
    for node in nodes:
        _integer(node, "route node", max(source, target, satellite_count - 1))
    if nodes[0] != source or nodes[-1] != target or len(set(nodes)) != len(nodes):
        raise ValueError("route.nodes must be acyclic and connect source to target")
    if any(node >= satellite_count for node in nodes[1:-1]):
        raise ValueError("route.nodes cannot use intermediate ground stations")
    if any(tuple(sorted(pair)) not in edges for pair in pairwise(nodes)):
        raise ValueError("route must follow links present in its snapshot and model")
    _positive_number(route["delay_s"], "route.delay_s")
    _positive_number(route["bottleneck_bps"], "route.bottleneck_bps")


def _validate_network(network, ground_frames, stations, satellite_count, sample_count, settings):
    _object(
        network,
        frozenset(("source_station_index", "target_station_index", "frames", "summary")),
        "network",
    )
    indices = tuple(
        _integer(network[field], f"network.{field}", len(stations) - 1)
        for field in ("source_station_index", "target_station_index")
    )
    if (stations[indices[0]]["name"], stations[indices[1]]["name"]) != (
        settings.source_station,
        settings.target_station,
    ):
        raise ValueError("network station indices must match the scenario endpoints")
    source, target = (satellite_count + index for index in indices)
    frames = _list(network["frames"], "network.frames", sample_count, sample_count)
    for frame, ground in zip(frames, ground_frames, strict=True):
        _object(frame, frozenset(("isl_edges", *ROUTE_MODELS)), "network frame")
        pairs = set()
        for edge in _list(
            frame["isl_edges"], "isl_edges", 0, satellite_count * (satellite_count - 1) // 2
        ):
            first, second, delay = _list(edge, "ISL edge", 3, 3)
            _integer(first, "ISL source", satellite_count - 1)
            _integer(second, "ISL target", satellite_count - 1)
            if first >= second or (first, second) in pairs:
                raise ValueError("ISL edges must be unique, ordered satellite pairs")
            pairs.add((first, second))
            _positive_number(delay, "ISL delay_s")
        for model in ROUTE_MODELS:
            edges = pairs | {
                (a, b)
                for a, b, _, rate in ground
                if b in (source, target) and (model == "fixed_capacity" or rate > 0.0)
            }
            _validate_route(frame[model], source, target, satellite_count, edges)
    summaries = _object(network["summary"], frozenset(ROUTE_MODELS), "network.summary")
    for model, summary in summaries.items():
        _object(
            summary,
            frozenset(("connected_sample_fraction", "integrated_bottleneck_bits", "route_changes")),
            f"network.summary.{model}",
        )
        _range(summary["connected_sample_fraction"], "connected_sample_fraction", 0.0, 1.0)
        _non_negative_number(summary["integrated_bottleneck_bits"], "integrated_bottleneck_bits")
        _integer(summary["route_changes"], "route_changes", sample_count - 1)


def _validate_statistics(records, stations, sample_count):
    for index, record in enumerate(_list(records, "statistics", len(stations), len(stations))):
        _object(
            record,
            frozenset(
                (
                    "name",
                    "station_index",
                    "visible_sample_fraction",
                    "usable_sample_fraction",
                    "best_link_integrated_bits",
                    "fixed_baseline_integrated_bits",
                    "handover_count",
                )
            ),
            "statistics",
        )
        if (
            _integer(record["station_index"], "statistics.station_index", len(stations) - 1)
            != index
            or record["name"] != stations[index]["name"]
        ):
            raise ValueError("statistics must correspond to each station in input order")
        visible = _range(record["visible_sample_fraction"], "visible_sample_fraction", 0.0, 1.0)
        _range(record["usable_sample_fraction"], "usable_sample_fraction", 0.0, visible)
        for field in ("best_link_integrated_bits", "fixed_baseline_integrated_bits"):
            _non_negative_number(record[field], field)
        _integer(record["handover_count"], "handover_count", sample_count - 1)


def _validate_document(document) -> None:
    """Check the renderer's bounded data contract without recomputing any model."""
    try:
        _object(document, DOCUMENT_KEYS, "experiment")
        if document["schema_version"] != "1" or document["kind"] != "openleo.constellation":
            raise ValueError("unsupported constellation experiment schema")
        timestamps = tuple(
            _utc(value, "timestamps_utc")
            for value in _list(document["timestamps_utc"], "timestamps_utc", 1, 1441)
        )
        if any(first >= second for first, second in pairwise(timestamps)):
            raise ValueError("timestamps_utc must be strictly increasing")
        count = len(timestamps)
        satellites = _list(document["satellites"], "satellites", 1, 128)
        stations = _list(document["stations"], "stations", 2, 16)
        if len(satellites) * len(stations) * count > 500_000:
            raise ValueError("experiment exceeds 500000 satellite-station-samples")
        scenario = parse_constellation(
            document["scenario"], Path("experiment.json"), document["provenance"]["scenario_sha256"]
        )
        modcod_names = _validate_metadata(document)
        _validate_geometry(satellites, stations, count, scenario)
        ground_frames = _validate_links(
            _list(document["links"], "links", count, count),
            len(satellites),
            len(stations),
            timestamps,
            modcod_names,
            scenario.time_window.minimum_elevation_deg,
        )
        _validate_network(
            document["network"], ground_frames, stations, len(satellites), count, scenario.network
        )
        _validate_statistics(document["statistics"], stations, count)
        if len(_json(document).encode("utf-8")) > MAX_ARTIFACT_BYTES:
            raise ValueError("experiment exceeds the portable artifact size limit")
    except (KeyError, TypeError, OverflowError, RecursionError, UnicodeError) as exc:
        raise ValueError(f"invalid constellation experiment: {exc}") from exc


def render_workbench(document: dict, *, live: bool = False, token: str = "") -> str:
    """Embed computed data, native browser code, and public-domain cartography."""
    _validate_document(document)
    assets = files("openleo").joinpath("_web")
    script = assets.joinpath("workbench.js").read_text(encoding="utf-8")
    style = assets.joinpath("workbench.css").read_text(encoding="utf-8")
    coastlines = json.loads(assets.joinpath("coastline.geojson").read_text(encoding="utf-8"))
    script_hash = base64.b64encode(sha256(script.encode("utf-8")).digest()).decode("ascii")
    style_hash = base64.b64encode(sha256(style.encode("utf-8")).digest()).decode("ascii")
    policy = (
        f"default-src 'none'; script-src 'sha256-{script_hash}'; "
        f"style-src 'sha256-{style_hash}'; img-src data: blob:; "
        "connect-src 'self'; base-uri 'none'; form-action 'none'; object-src 'none'"
    )
    return Template(assets.joinpath("workbench.html").read_text(encoding="utf-8")).substitute(
        experiment_json=_embedded_json(document),
        coastline_json=_embedded_json(coastlines),
        session_json=_embedded_json({"live": live, "token": token if live else ""}),
        javascript=script,
        stylesheet=style,
        csp=policy,
    )


def _csv_value(value):
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return format(value, ".15g")
    return value


def _csv(fields: tuple[str, ...], rows) -> str:
    file = StringIO(newline="")
    writer = csv.DictWriter(file, fieldnames=fields, lineterminator="\n", extrasaction="ignore")
    writer.writeheader()
    writer.writerows({key: _csv_value(value) for key, value in row.items()} for row in rows)
    return file.getvalue()


def links_csv(document: dict) -> str:
    return _csv(
        LINK_FIELDS,
        (
            {"timestamp_utc": timestamp, **link}
            for timestamp, frame in zip(document["timestamps_utc"], document["links"], strict=True)
            for link in frame
        ),
    )


def routes_csv(document: dict) -> str:
    return _csv(
        ROUTE_FIELDS,
        (
            {
                "timestamp_utc": timestamp,
                "model": model,
                "connected": route is not None,
                "nodes": " ".join(map(str, route["nodes"])) if route else "",
                "delay_s": route["delay_s"] if route else None,
                "bottleneck_bps": route["bottleneck_bps"] if route else 0.0,
            }
            for timestamp, frame in zip(
                document["timestamps_utc"], document["network"]["frames"], strict=True
            )
            for model in ("minimum_delay", "maximum_rate", "fixed_capacity")
            for route in (frame[model],)
        ),
    )


def write_workbench(document: dict, output_dir: str | Path) -> tuple[Path, ...]:
    """Write the complete portable experiment and checksums of its exact bytes."""
    _validate_document(document)
    contents = {
        "experiment.json": _json(document) + "\n",
        "links.csv": links_csv(document),
        "routes.csv": routes_csv(document),
        "explorer.html": render_workbench(document),
    }
    manifest = {
        "schema_version": "1",
        "kind": "openleo.bundle",
        "versions": {"openleo-link": version("openleo-link")},
        "files": {
            name: sha256(content.encode("utf-8")).hexdigest() for name, content in contents.items()
        },
    }
    all_contents = {**contents, "manifest.json": _json(manifest) + "\n"}
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    for name, content in all_contents.items():
        (directory / name).write_text(content, encoding="utf-8", newline="\n")
    return tuple(directory / name for name in all_contents)


def _read(path: Path, limit: int) -> bytes:
    try:
        with path.open("rb") as file:
            raw = file.read(limit + 1)
    except OSError as exc:
        raise ValueError(f"could not read {path.name}") from exc
    if len(raw) > limit:
        raise ValueError(f"{path.name} exceeds {limit} bytes")
    return raw


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def load_experiment(run_directory: str | Path) -> dict:
    """Read only the fixed bundle filenames and verify all artifact checksums."""
    directory = Path(run_directory)
    try:
        manifest = json.loads(
            _read(directory / "manifest.json", 1_000_000), object_pairs_hook=_unique_object
        )
        if (
            not isinstance(manifest, dict)
            or manifest.get("schema_version") != "1"
            or manifest.get("kind") != "openleo.bundle"
        ):
            raise ValueError("unsupported bundle manifest")
        checksums = manifest.get("files")
        if not isinstance(checksums, dict) or set(checksums) != set(ARTIFACT_NAMES):
            raise ValueError("manifest files must exactly match the fixed bundle files")
        contents = {name: _read(directory / name, MAX_ARTIFACT_BYTES) for name in ARTIFACT_NAMES}
        for name, raw in contents.items():
            if checksums[name] != sha256(raw).hexdigest():
                raise ValueError(f"{name} SHA-256 mismatch")
        document = json.loads(contents["experiment.json"], object_pairs_hook=_unique_object)
        _validate_document(document)
        return document
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("invalid JSON in experiment bundle") from exc
