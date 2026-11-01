"""Sweeps: one mechanism for the v1 sensitivity, fidelity, propagation and experiment studies.

A sweep spec (``openleo.sweep/2``) names a base scenario and a way to vary it:

``oat``
    One at a time. Each parameter has a ``nominal`` that must equal the base scenario's value
    (the v1 sensitivity guarantee), and ``values``. The runs are the nominal run, ``oat:nominal``,
    then one run per parameter value that differs from its nominal. The baseline is the nominal.
``grid``
    The Cartesian product of the parameters' values, the first parameter varying slowest, as
    ``grid:0``, ``grid:1``, ... The baseline is the first combination (``grid:0``).
``variants``
    An explicit list of ``{id, label, patch, baseline}``. The baseline is the variant flagged
    ``"baseline": true`` (at most one), else the first.

Paths are RFC 6901 JSON Pointers into the normalised scenario document, and patches are the
RFC 6902 subset ``replace``, ``add`` and ``remove``. ``window`` clips every run to a part of the
base window, and ``replay`` asks for packet replays per run (``_run_replays``, the one seam to
the trusted backends). Every generated scenario is validated by the scenario parser, stored with
its own SHA-256 and linked to the base scenario's. ``max_runs`` (default and ceiling 512) bounds
the number of runs.

Run ids are ``oat:<pointer>=<value>``, ``grid:<i>`` and ``variant:<id>``, percent-encoded so they
are safe file names (and the names of the directories under ``runs/``).

Comparisons are deterministic: metric values of each run against the baseline run, as value,
baseline value, delta and ratio. They carry no probability and are never an uncertainty interval.

Errors are ``ValueError``; spec errors start with the JSON pointer of the offending value.
"""

# Every malformed document is a ValueError (design spec section 3.2), including a wrong JSON type.
# ruff: noqa: TRY004

from __future__ import annotations

import copy
import csv
import io
import itertools
import json
import math
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

from openleo._validation import MAX_SCENARIO_BYTES, _field, _read_scenario_bytes, _safe_repr
from openleo.engine import _assembly as assembly
from openleo.engine.run import run as run_engine
from openleo.engine.run import save_run
from openleo.results import Results, publish_atomically, write_manifest
from openleo.scenario import load_scenario, parse_scenario, scenario_document
from openleo.scenario.model import ROUTE_MODELS
from openleo.scenario.parse import _checked_pairs, _reject_repeated_keys

SWEEP_SCHEMA_ID = "openleo.sweep/2"
SWEEP_RESULT_KIND = "openleo.sweep"
SWEEP_RESULT_SCHEMA_VERSION = "2"
COMPARISON_LABEL = "deterministic comparison; not an uncertainty interval"
MAX_RUNS = 512
MAX_SPEC_BYTES = MAX_SCENARIO_BYTES
MAX_REPLAY_WINDOW_S = 3600.0
MAX_WINDOW_S = 31 * 86400.0  # the scenario window limit
TIME_BOUNDS = ("start_utc", "stop_utc")
MAX_RUN_ID_CHARS = 200
MAX_VARIANT_ID_CHARS = 64
MODES = ("oat", "grid", "variants")
PATCH_OPS = ("replace", "add", "remove")
COMPARISON_COLUMNS = ("run_id", "metric", "value", "baseline_value", "delta", "ratio")
NOMINAL_RUN = "oat:nominal"
LOAD_BOUNDS = (1.0, 1e9)
ACQUISITION_BOUNDS = (0.0, 60.0)
MAX_LOADS = 8
REPLAY_KINDS = ("link", "network")
NATIVE_REPLAY_EVIDENCE = "actual_ns3"
BASELINE_RULES = {
    "oat": "the nominal run, which reproduces the base scenario",
    "grid": "the first combination of the grid (grid:0)",
    "variants": 'the variant with "baseline": true, else the first variant',
}
LIMITATIONS = (
    COMPARISON_LABEL,
    (
        "Every difference is the effect of changing the declared inputs, one computation against "
        "another. No sampling, probability distribution or confidence interval is involved, so no "
        "difference is an uncertainty estimate."
    ),
    (
        "Runs share the base scenario's declared assumptions; a delta says how much the changed "
        "input moves a metric in this model, not how far the model is from the real system."
    ),
)
_MISSING_REPLAY_LIMITATION = (
    "Packet replay evidence is missing for {kinds}: no trusted backend was configured, so "
    "those replays were not run."
)
_LOAD_ONLY_KEYS = frozenset(("base_scenario_path", "spec_sha256", "source_bytes"))
_SPEC_KEYS = frozenset(("schema", "name", "base_scenario", "mode"))
_SPEC_OPTIONAL = frozenset(("parameters", "variants", "window", "replay", "max_runs"))
_PATCH_KEYS = frozenset(("op", "path", "value"))
_INDEX = re.compile(r"(?:0|[1-9][0-9]*)\Z")
_BAD_ESCAPE = re.compile(r"~(?![01])")


@dataclass(frozen=True)
class SweepRun:
    """One generated scenario: a filesystem-safe id, a label, the normalised document, its hash."""

    id: str
    label: str
    document: dict
    sha256: str


# --------------------------------------------------------------------------------- JSON Patch


def _tokens(pointer: Any, where: str) -> list[str]:
    if not isinstance(pointer, str) or not pointer.startswith("/"):
        raise ValueError(
            f"{where}={_safe_repr(pointer)} must be a JSON Pointer starting with '/' "
            "(RFC 6901); the empty pointer is not allowed"
        )
    parts = pointer[1:].split("/")
    for part in parts:
        if _BAD_ESCAPE.search(part):
            raise ValueError(
                f"{where}={_safe_repr(pointer)} has an invalid escape: '~' must be '~0' or '~1'"
            )
    return [part.replace("~1", "/").replace("~0", "~") for part in parts]


def _step(node: Any, token: str, where: str, *, creating: bool = False) -> Any:
    """The key or index ``token`` names in ``node``; ValueError (at ``where``) if impossible."""
    if isinstance(node, dict):
        return token
    if isinstance(node, list):
        if token == "-" and creating:
            return len(node)
        if not _INDEX.match(token) or int(token) > len(node) - (0 if creating else 1):
            raise ValueError(f"{where}: {token!r} is not a valid array index (length {len(node)})")
        return int(token)
    raise ValueError(f"{where}: a {_kind(node)} cannot hold {token!r}")


def _kind(value: Any) -> str:
    return "null" if value is None else type(value).__name__


def apply_patch(document: dict, operations: Sequence[dict]) -> dict:
    """A copy of ``document`` with the RFC 6902 ``replace``, ``add`` and ``remove`` operations.

    Neither the document nor the values of the operations are aliased by the result. Errors are
    ValueError starting with ``/<operation index>/...``.
    """
    if not isinstance(operations, Sequence) or isinstance(operations, (str, bytes)):
        raise ValueError("/ must be an array of patch operations")
    result = copy.deepcopy(document)
    for index, operation in enumerate(operations):
        _apply_one(result, operation, f"/{index}")
    return result


def _check_operation(operation: Any, here: str) -> tuple[str, str, list[str]]:
    """The shape of one operation: (op, pointer, tokens). Nothing is resolved."""
    if not isinstance(operation, Mapping):
        raise ValueError(f"{here} must be a JSON object")
    for key in sorted(operation):
        if key not in _PATCH_KEYS:
            raise ValueError(f"{_field(here, key)} is not allowed")
    op = operation.get("op")
    if op not in PATCH_OPS:
        raise ValueError(f"{here}/op={_safe_repr(op)} must be one of {list(PATCH_OPS)}")
    if "path" not in operation:
        raise ValueError(f"{here}/path is required")
    pointer = operation["path"]
    tokens = _tokens(pointer, f"{here}/path")
    if op == "remove" and "value" in operation:
        raise ValueError(f"{here}/value is not allowed for remove")
    if op != "remove" and "value" not in operation:
        raise ValueError(f"{here}/value is required for {op}")
    return op, pointer, tokens


def _apply_one(root: Any, operation: Any, here: str) -> None:
    op, pointer, tokens = _check_operation(operation, here)
    where = f"{here}/path={_safe_repr(pointer)}"
    parent: Any = root
    for depth, token in enumerate(tokens[:-1]):
        parent = _child(parent, token, where, f"/{'/'.join(tokens[: depth + 1])}")
    last = tokens[-1]
    if op == "add":
        key = _step(parent, last, where, creating=True)
        value = copy.deepcopy(operation["value"])
        if isinstance(parent, dict):
            parent[key] = value
        else:
            parent.insert(key, value)
        return
    if isinstance(parent, dict) and last not in parent:
        raise ValueError(f"{where}: no member {last!r}")
    key = _step(parent, last, where)
    if op == "replace":
        parent[key] = copy.deepcopy(operation["value"])
    else:
        del parent[key]


def _child(node: Any, token: str, where: str, prefix: str) -> Any:
    if isinstance(node, dict):
        if token not in node:
            raise ValueError(f"{where}: no member {token!r} at {prefix}")
        return node[token]
    return node[_step(node, token, where)]


# ------------------------------------------------------------------------------------ the spec


def _read_json(path: Path) -> tuple[Any, bytes]:
    """The parsed JSON of a bounded file and the bytes it was parsed from."""
    try:
        source = _read_scenario_bytes(path)
    except OSError as exc:
        raise ValueError(f"{path}: could not load sweep: {exc}") from exc
    except ValueError as exc:
        raise ValueError(str(exc).replace("scenario JSON", "sweep JSON")) from exc
    try:
        text = source.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{path}: sweep JSON must be UTF-8") from exc

    def constant(name: str) -> Any:
        raise ValueError(f"{name} is not allowed")

    try:
        raw = json.loads(text, object_pairs_hook=_checked_pairs, parse_constant=constant)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path}: invalid JSON: {exc.msg}") from exc
    except RecursionError as exc:
        raise ValueError(f"{path}: invalid JSON: nested too deeply") from exc
    except ValueError as exc:
        raise ValueError(f"{path}: invalid JSON: {exc}") from exc
    _reject_repeated_keys(raw)
    return raw, source


def _only(raw: Mapping, pointer: str, required: frozenset, optional: frozenset = frozenset()):
    for key in sorted(required):
        if key not in raw:
            raise ValueError(f"{_field(pointer, key)} is required")
    for key in sorted(raw):
        if key not in required | optional:
            raise ValueError(f"{_field(pointer, key)} is not allowed")


def _object(raw: Any, pointer: str) -> Mapping:
    if not isinstance(raw, Mapping):
        raise ValueError(f"{pointer or '/'} must be a JSON object")
    return raw


def _text(value: Any, pointer: str, limit: int | None = None) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{pointer}={_safe_repr(value)} must be a non-empty string")
    if limit is not None and len(value) > limit:
        raise ValueError(f"{pointer}={_safe_repr(value)} must be at most {limit} characters")
    return value


def _number(value: Any, pointer: str, low: float, high: float | None, *, strict: bool = False):
    try:
        bounded = (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
            and (value > low if strict else value >= low)
            and (high is None or value <= high)
        )
    except OverflowError:  # an integer too large for a float
        bounded = False
    if not bounded:
        upper = "" if high is None else f" and at most {high:g}"
        lower = f"greater than {low:g}" if strict else f"at least {low:g}"
        raise ValueError(f"{pointer}={_safe_repr(value)} must be a finite number {lower}{upper}")
    return value


def _integer(value: Any, pointer: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f"{pointer}={_safe_repr(value)} must be an integer in {low}..{high}")
    return value


def _array(value: Any, pointer: str, low: int, high: int) -> list:
    if not isinstance(value, list):
        raise ValueError(f"{pointer} must be an array")
    if not low <= len(value) <= high:
        raise ValueError(f"{pointer} must hold {low} to {high} items, not {len(value)}")
    return value


def _numbers_equal(a: Any, b: Any) -> bool:
    """JSON equality in which 15 and 15.0 are the same number and a boolean is not a number."""
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a == b
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_numbers_equal(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_numbers_equal(x, y) for x, y in zip(a, b, strict=True))
    return type(a) is type(b) and a == b


def _unique(values: list, pointer: str) -> None:
    for index, value in enumerate(values):
        for earlier in range(index):
            if _numbers_equal(value, values[earlier]):
                raise ValueError(
                    f"{_field(pointer, str(index))} duplicates {_field(pointer, str(earlier))}"
                )


def _overlap(a: list[str], b: list[str]) -> bool:
    shortest = min(len(a), len(b))
    return a[:shortest] == b[:shortest]


def _parameters(raw: Any, mode: str) -> list[dict]:
    pointer = "/parameters"
    items = _array(raw, pointer, 1, MAX_RUNS)
    parameters: list[dict] = []
    seen: list[list[str]] = []
    for index, item in enumerate(items):
        here = _field(pointer, str(index))
        entry = _object(item, here)
        _only(entry, here, frozenset(("path", "values")), frozenset(("nominal",)))
        path_pointer = _field(here, "path")
        tokens = _tokens(entry["path"], path_pointer)
        for earlier, other in enumerate(seen):
            if _overlap(tokens, other):
                raise ValueError(
                    f"{path_pointer}={_safe_repr(entry['path'])} overlaps "
                    f"{_field(_field(pointer, str(earlier)), 'path')}"
                )
        seen.append(tokens)
        values = _array(entry["values"], _field(here, "values"), 1, MAX_RUNS)
        _unique(values, _field(here, "values"))
        parameter = {"path": entry["path"], "values": copy.deepcopy(values)}
        if mode == "grid":
            if "nominal" in entry:
                raise ValueError(f"{_field(here, 'nominal')} is not allowed for mode grid")
        else:
            if "nominal" not in entry:
                raise ValueError(f"{_field(here, 'nominal')} is required for mode oat")
            if all(_numbers_equal(value, entry["nominal"]) for value in values):
                raise ValueError(
                    f"{_field(here, 'values')} must hold a value other than the nominal"
                )
            parameter["nominal"] = copy.deepcopy(entry["nominal"])
        parameters.append(parameter)
    return parameters


def _variants(raw: Any) -> list[dict]:
    pointer = "/variants"
    items = _array(raw, pointer, 1, MAX_RUNS)
    variants: list[dict] = []
    flagged: int | None = None
    for index, item in enumerate(items):
        here = _field(pointer, str(index))
        entry = _object(item, here)
        _only(entry, here, frozenset(("id", "patch")), frozenset(("label", "baseline")))
        identifier = _text(entry["id"], _field(here, "id"), MAX_VARIANT_ID_CHARS)
        for earlier, other in enumerate(variants):
            if other["id"] == identifier:
                raise ValueError(
                    f"{_field(here, 'id')}={_safe_repr(identifier)} duplicates "
                    f"{_field(_field(pointer, str(earlier)), 'id')}"
                )
        label = entry.get("label", identifier)
        if not isinstance(label, str):
            raise ValueError(f"{_field(here, 'label')}={_safe_repr(label)} must be a string")
        baseline = entry.get("baseline", False)
        if not isinstance(baseline, bool):
            raise ValueError(
                f"{_field(here, 'baseline')}={_safe_repr(baseline)} must be true or false"
            )
        if baseline:
            if flagged is not None:
                raise ValueError(
                    f"{_field(here, 'baseline')}: only one variant may be the baseline "
                    f"(variant {flagged} is)"
                )
            flagged = index
        patch = entry["patch"]
        _array(patch, _field(here, "patch"), 0, 10_000)
        _check_patch_shape(patch, _field(here, "patch"))
        variants.append(
            {"id": identifier, "label": label, "patch": copy.deepcopy(patch), "baseline": baseline}
        )
    return variants


def _check_patch_shape(patch: list, pointer: str) -> None:
    """The shape of every operation; the pointers resolve against the base at expansion."""
    for index, operation in enumerate(patch):
        _check_operation(operation, _field(pointer, str(index)))


def _window(raw: Any) -> dict:
    entry = _object(raw, "/window")
    _only(entry, "/window", frozenset(("start_offset_s", "duration_s")))
    return {
        "start_offset_s": _number(
            entry["start_offset_s"], "/window/start_offset_s", 0.0, MAX_WINDOW_S
        ),
        "duration_s": _number(
            entry["duration_s"], "/window/duration_s", 0.0, MAX_WINDOW_S, strict=True
        ),
    }


def _loads(raw: Any, pointer: str) -> list[float]:
    values = _array(raw, pointer, 1, MAX_LOADS)
    for index, value in enumerate(values):
        _number(value, _field(pointer, str(index)), *LOAD_BOUNDS)
    _unique(values, pointer)
    return [float(value) for value in values]


def _packet_settings(entry: Mapping, here: str) -> dict:
    return {
        "packet_size_bytes": _integer(
            entry.get("packet_size_bytes", 512), _field(here, "packet_size_bytes"), 64, 1400
        ),
        "queue_packets": _integer(
            entry.get("queue_packets", 32), _field(here, "queue_packets"), 1, 10_000
        ),
        "seed": _integer(entry.get("seed", 1), _field(here, "seed"), 1, 2**31 - 1),
    }


def _link_replay(raw: Any) -> dict:
    here = "/replay/link"
    entry = _object(raw, here)
    packet = frozenset(("packet_size_bytes", "queue_packets", "seed"))
    _only(entry, here, frozenset(("station", "norad_id", "loads_bps")), packet)
    return {
        "station": _text(entry["station"], _field(here, "station")),
        "norad_id": _integer(entry["norad_id"], _field(here, "norad_id"), 1, 2**53 - 1),
        "loads_bps": _loads(entry["loads_bps"], _field(here, "loads_bps")),
        **_packet_settings(entry, here),
    }


def _network_replay(raw: Any) -> dict:
    here = "/replay/network"
    entry = _object(raw, here)
    optional = frozenset(
        (
            "routing_models",
            "packet_size_bytes",
            "queue_packets",
            "seed",
            "flows_per_direction",
            "acquisition_delay_s",
        )
    )
    _only(entry, here, frozenset(("loads_bps",)), optional)
    models = entry.get("routing_models")
    if models is not None:
        pointer = _field(here, "routing_models")
        models = _array(models, pointer, 1, len(ROUTE_MODELS))
        for index, model in enumerate(models):
            if model not in ROUTE_MODELS:
                raise ValueError(
                    f"{_field(pointer, str(index))}={_safe_repr(model)} must be one of "
                    f"{list(ROUTE_MODELS)}"
                )
        _unique(models, pointer)
        models = list(models)
    return {
        "routing_models": models,
        "loads_bps": _loads(entry["loads_bps"], _field(here, "loads_bps")),
        "flows_per_direction": _integer(
            entry.get("flows_per_direction", 1), _field(here, "flows_per_direction"), 1, 4
        ),
        "acquisition_delay_s": float(
            _number(
                entry.get("acquisition_delay_s", 0.0),
                _field(here, "acquisition_delay_s"),
                *ACQUISITION_BOUNDS,
            )
        ),
        **_packet_settings(entry, here),
    }


def _replay(raw: Any) -> dict:
    entry = _object(raw, "/replay")
    _only(entry, "/replay", frozenset(), frozenset(REPLAY_KINDS))
    link, network = entry.get("link"), entry.get("network")
    if link is None and network is None:
        raise ValueError("/replay must name a link or a network replay")
    return {
        "link": None if link is None else _link_replay(link),
        "network": None if network is None else _network_replay(network),
    }


def load_sweep(path: Path) -> dict:
    """Read and validate an ``openleo.sweep/2`` file.

    The result holds every key explicitly (absent optional blocks are ``None``, ``max_runs``
    defaults to 512, variants carry ``label`` and ``baseline``) and three keys that are not in
    the file: ``base_scenario_path``, the base scenario's absolute path (``base_scenario`` is
    relative to the sweep file), ``spec_sha256``, the canonical hash of the parsed file, and
    ``source_bytes``, the bytes that were parsed. The base scenario itself is read by
    ``run_sweep``. With a ``window``, patches under ``/time/start_utc`` and ``/time/stop_utc``
    are refused: the window decides them.
    """
    path = Path(path)
    raw, source = _read_json(path)
    spec = parse_sweep(raw, source)
    spec["base_scenario_path"] = str((path.parent / spec["base_scenario"]).resolve())
    return spec


def parse_sweep(raw: Any, source: bytes) -> dict:
    """Validate an already parsed sweep document: ``load_sweep`` without the file.

    ``source`` is recorded as ``source_bytes``. The result has no ``base_scenario_path``: the
    caller decides where the base scenario lives (``load_sweep`` resolves ``base_scenario``
    against the sweep file; the app server pins it to the scenario it was started with).
    """
    entry = _object(raw, "")
    if "schema" not in entry:
        raise ValueError("/schema is required")
    if entry["schema"] != SWEEP_SCHEMA_ID:
        raise ValueError(
            f"/schema={_safe_repr(entry['schema'])} must be exactly {SWEEP_SCHEMA_ID!r}"
        )
    _only(entry, "", _SPEC_KEYS, _SPEC_OPTIONAL)
    mode = entry["mode"]
    if mode not in MODES:
        raise ValueError(f"/mode={_safe_repr(mode)} must be one of {list(MODES)}")
    name = _text(entry["name"], "/name", 200)
    base = _text(entry["base_scenario"], "/base_scenario")
    parameters = variants = None
    if mode == "variants":
        if "parameters" in entry:
            raise ValueError("/parameters is not allowed for mode variants")
        if "variants" not in entry:
            raise ValueError("/variants is required for mode variants")
        variants = _variants(entry["variants"])
    else:
        if "variants" in entry:
            raise ValueError(f"/variants is not allowed for mode {mode}")
        if "parameters" not in entry:
            raise ValueError(f"/parameters is required for mode {mode}")
        parameters = _parameters(entry["parameters"], mode)
    window = None if entry.get("window") is None else _window(entry["window"])
    replay = None if entry.get("replay") is None else _replay(entry["replay"])
    if replay is not None and window is not None and window["duration_s"] > MAX_REPLAY_WINDOW_S:
        raise ValueError(
            f"/window/duration_s={_safe_repr(window['duration_s'])} must be at most "
            f"{MAX_REPLAY_WINDOW_S:g} when a replay is requested (the v1 experiment rule)"
        )
    maximum = _integer(entry.get("max_runs", MAX_RUNS), "/max_runs", 1, MAX_RUNS)
    if window is not None:
        _refuse_time_bounds(parameters, variants)
    return {
        "schema": SWEEP_SCHEMA_ID,
        "name": name,
        "base_scenario": base,
        "mode": mode,
        "parameters": parameters,
        "variants": variants,
        "window": window,
        "replay": replay,
        "max_runs": maximum,
        "spec_sha256": document_sha256(raw),
        "source_bytes": source,
    }


def spec_touches(spec: dict, pointer: str) -> bool:
    """Whether a parameter path or a variant patch path of a validated spec is at, under or
    above ``pointer`` (so a run could change the value there)."""
    target = _tokens(pointer, "pointer")
    paths = [parameter["path"] for parameter in spec.get("parameters") or ()]
    paths += [op["path"] for variant in spec.get("variants") or () for op in variant["patch"]]
    return any(_overlap(_tokens(path, "path"), target) for path in paths)


def _moves_time_bounds(pointer: str) -> bool:
    tokens = _tokens(pointer, "")
    return any(_overlap(tokens, ["time", bound]) for bound in TIME_BOUNDS)


def _refuse_time_bounds(parameters: list[dict] | None, variants: list[dict] | None) -> None:
    """With a ``window``, the run's start and stop belong to the window, not to a patch."""
    suffix = " cannot change /time/start_utc or /time/stop_utc when a window is set"
    for index, parameter in enumerate(parameters or ()):
        if _moves_time_bounds(parameter["path"]):
            where = _field(_field("/parameters", str(index)), "path")
            raise ValueError(f"{where}={_safe_repr(parameter['path'])}{suffix}")
    for index, variant in enumerate(variants or ()):
        for position, operation in enumerate(variant["patch"]):
            if _moves_time_bounds(operation["path"]):
                patch = _field(_field("/variants", str(index)), "patch")
                where = _field(_field(patch, str(position)), "path")
                raise ValueError(f"{where}={_safe_repr(operation['path'])}{suffix}")


# ------------------------------------------------------------------------------- expansion


def document_sha256(document: Any) -> str:
    """SHA-256 of a document's canonical JSON (sorted keys, compact, UTF-8)."""
    return assembly.canonical_sha256(document)


def _run_id(text: str, pointer: str) -> str:
    identifier = quote(text, safe="")
    if len(identifier) > MAX_RUN_ID_CHARS:
        raise ValueError(
            f"{pointer}: run id {_safe_repr(text, 60)} exceeds {MAX_RUN_ID_CHARS} characters once "
            "encoded; use mode variants with short ids"
        )
    return identifier


def _value_text(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _utc(text: str) -> datetime:
    return datetime.fromisoformat(text).astimezone(UTC)


def _utc_text(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _clip(document: dict, window: dict | None, *, replay: bool) -> dict:
    """The document with ``time`` clipped to ``window`` (offsets from the base start)."""
    time_block = document["time"]
    start, stop = _utc(time_block["start_utc"]), _utc(time_block["stop_utc"])
    base_s = (stop - start).total_seconds()
    if window is None:
        if replay and base_s > MAX_REPLAY_WINDOW_S:
            raise ValueError(
                f"/window: a replay needs a window of at most {MAX_REPLAY_WINDOW_S:g} s, and "
                f"the base window is {base_s:g} s; set window.duration_s"
            )
        return document
    offset, duration = window["start_offset_s"], window["duration_s"]
    for key, value in (("start_offset_s", offset), ("duration_s", duration)):
        micro = timedelta(seconds=value)
        if not math.isclose(micro.total_seconds(), value, rel_tol=0.0, abs_tol=1e-12):
            raise ValueError(
                f"/window/{key}={_safe_repr(value)} must be exactly representable at "
                "microsecond resolution"
            )
    if offset + duration > base_s:
        raise ValueError(
            f"/window: start_offset_s + duration_s = {offset + duration:g} s ends after the base "
            f"window ({base_s:g} s)"
        )
    if duration < time_block["step_s"]:
        raise ValueError(
            f"/window/duration_s={_safe_repr(duration)} must be at least the base step_s "
            f"{time_block['step_s']:g}"
        )
    if replay and duration > MAX_REPLAY_WINDOW_S:
        raise ValueError(
            f"/window/duration_s={_safe_repr(duration)} must be at most "
            f"{MAX_REPLAY_WINDOW_S:g} when a replay is requested"
        )
    clipped = copy.deepcopy(document)
    new_start = start + timedelta(seconds=offset)
    clipped["time"]["start_utc"] = _utc_text(new_start)
    clipped["time"]["stop_utc"] = _utc_text(new_start + timedelta(seconds=duration))
    return clipped


def _value_at(document: Any, pointer: str, where: str) -> Any:
    node = document
    tokens = _tokens(pointer, where)
    for depth, token in enumerate(tokens):
        prefix = f"/{'/'.join(tokens[:depth])}" if depth else "document root"
        if isinstance(node, dict):
            if token not in node:
                raise ValueError(f"{where}={_safe_repr(pointer)}: no member {token!r} at {prefix}")
            node = node[token]
        else:
            node = node[_step(node, token, f"{where}={_safe_repr(pointer)}")]
    return node


def _replace(document: dict, pointer: str, value: Any) -> dict:
    return apply_patch(document, [{"op": "replace", "path": pointer, "value": value}])


def _check_extent(document: dict, extent: dict, run_id: str, where: str) -> None:
    """A generated run keeps the window rules: inside the base window, short with a replay."""
    start, stop = _utc(document["time"]["start_utc"]), _utc(document["time"]["stop_utc"])
    if extent["windowed"] and (start < extent["start"] or stop > extent["stop"]):
        raise ValueError(
            f"{where}: run {run_id} spans {_utc_text(start)} to {_utc_text(stop)}, outside the "
            f"base window {_utc_text(extent['start'])} to {_utc_text(extent['stop'])}"
        )
    length = (stop - start).total_seconds()
    if extent["replay"] and length > MAX_REPLAY_WINDOW_S:
        raise ValueError(
            f"{where}: run {run_id} spans {length:g} s, more than the {MAX_REPLAY_WINDOW_S:g} s "
            "allowed when a replay is requested"
        )


def _generated(
    base_path: Path, patched: dict, run_id: str, label: str, where: str, extent: dict
) -> SweepRun:
    """Validate ``patched`` as a scenario and keep its normalised document and hash."""
    digest = document_sha256(patched)
    try:
        scenario = parse_scenario(patched, base_path, digest)
    except ValueError as exc:
        raise ValueError(f"{where}: generated scenario {run_id} is invalid: {exc}") from exc
    normalised = scenario_document(scenario)
    _check_extent(normalised, extent, run_id, where)
    return SweepRun(run_id, label, normalised, document_sha256(normalised))


def expand_sweep(spec: dict, base_document: dict) -> tuple[SweepRun, ...]:
    """The runs of a validated sweep over ``base_document`` (a normalised scenario document).

    Every run's scenario is validated with the scenario parser and carries its own SHA-256.
    The baseline run is ``baseline_run_id(spec, runs)``.
    """
    base_path = Path(spec.get("base_scenario_path", spec["base_scenario"]))
    replay = spec.get("replay")
    clipped = _clip(base_document, spec.get("window"), replay=replay is not None)
    extent = {
        "windowed": spec.get("window") is not None,
        "replay": replay is not None,
        "start": _utc(base_document["time"]["start_utc"]),
        "stop": _utc(base_document["time"]["stop_utc"]),
    }
    mode, maximum = spec["mode"], spec.get("max_runs", MAX_RUNS)
    runs: list[SweepRun] = []
    if mode == "oat":
        runs = _expand_oat(spec, base_document, clipped, base_path, maximum, extent)
    elif mode == "grid":
        runs = _expand_grid(spec, base_document, clipped, base_path, maximum, extent)
    else:
        runs = _expand_variants(spec, clipped, base_path, maximum, extent)
    _check_portable_ids(runs, "/variants" if mode == "variants" else "/parameters")
    if replay is not None:
        for run in runs:
            _check_replay(replay, run)
    return tuple(runs)


def _check_portable_ids(runs: Sequence[SweepRun], pointer: str) -> None:
    """Refuse run ids whose directories would collide or be renamed on macOS or Windows."""
    seen: dict[str, str] = {}
    for run in runs:
        if run.id.endswith("."):
            raise ValueError(
                f"{pointer}: run id {_safe_repr(run.id, 60)} ends with '.', which Windows strips "
                "from directory names; change the value or variant id"
            )
        key = run.id.casefold()
        if key in seen:
            raise ValueError(
                f"{pointer}: run ids {_safe_repr(seen[key], 60)} and {_safe_repr(run.id, 60)} "
                "differ only by case, so their directories collide on case-insensitive file "
                "systems; rename one"
            )
        seen[key] = run.id


def _too_many(pointer: str, what: str, count: int, maximum: int) -> ValueError:
    return ValueError(f"{pointer}: the {what} has {count} runs, at most {maximum} are allowed")


def _expand_oat(spec, base_document, clipped, base_path, maximum, extent) -> list[SweepRun]:
    parameters = spec["parameters"]
    count = 1 + sum(
        sum(not _numbers_equal(v, p["nominal"]) for v in p["values"]) for p in parameters
    )
    if count > maximum:
        raise _too_many("/parameters", "oat sweep", count, maximum)
    for index, parameter in enumerate(parameters):
        here = _field("/parameters", str(index))
        current = _value_at(base_document, parameter["path"], _field(here, "path"))
        if not _numbers_equal(current, parameter["nominal"]):
            raise ValueError(
                f"{_field(here, 'nominal')}={_safe_repr(parameter['nominal'])} must equal the "
                f"base scenario's value {_safe_repr(current)} at {parameter['path']}"
            )
    runs = [
        _generated(
            base_path,
            clipped,
            _run_id(NOMINAL_RUN, "/parameters"),
            "nominal",
            "/parameters",
            extent,
        )
    ]
    for index, parameter in enumerate(parameters):
        here = _field("/parameters", str(index))
        for position, value in enumerate(parameter["values"]):
            if _numbers_equal(value, parameter["nominal"]):
                continue
            where = _field(_field(here, "values"), str(position))
            patched = _replace(clipped, parameter["path"], value)
            text = f"{parameter['path']} = {_value_text(value)}"
            run_id = _run_id(f"oat:{parameter['path']}={_value_text(value)}", where)
            runs.append(_generated(base_path, patched, run_id, text, where, extent))
    return runs


def _expand_grid(spec, base_document, clipped, base_path, maximum, extent) -> list[SweepRun]:
    parameters = spec["parameters"]
    count = math.prod(len(p["values"]) for p in parameters)
    if count > maximum:
        raise _too_many("/parameters", "grid", count, maximum)
    for index, parameter in enumerate(parameters):
        _value_at(
            base_document, parameter["path"], _field(_field("/parameters", str(index)), "path")
        )
    runs = []
    for number, combination in enumerate(itertools.product(*(p["values"] for p in parameters))):
        patched = clipped
        for parameter, value in zip(parameters, combination, strict=True):
            patched = _replace(patched, parameter["path"], value)
        label = ", ".join(
            f"{p['path']} = {_value_text(v)}" for p, v in zip(parameters, combination, strict=True)
        )
        run_id = _run_id(f"grid:{number}", "/parameters")
        runs.append(_generated(base_path, patched, run_id, label, "/parameters", extent))
    return runs


def _expand_variants(spec, clipped, base_path, maximum, extent) -> list[SweepRun]:
    variants = spec["variants"]
    if len(variants) > maximum:
        raise _too_many("/variants", "variants sweep", len(variants), maximum)
    runs = []
    for index, variant in enumerate(variants):
        here = _field("/variants", str(index))
        try:
            patched = apply_patch(clipped, variant["patch"])
        except ValueError as exc:
            raise ValueError(f"{_field(here, 'patch')}{exc}") from exc
        run_id = _run_id(f"variant:{variant['id']}", _field(here, "id"))
        runs.append(_generated(base_path, patched, run_id, variant["label"], here, extent))
    return runs


def baseline_run_id(spec: dict, runs: Sequence[SweepRun]) -> str:
    """The id of the run every comparison is against (module docstring for the rules)."""
    if spec["mode"] == "variants":
        for variant, run in zip(spec["variants"], runs, strict=True):
            if variant.get("baseline"):
                return run.id
    return runs[0].id


def _check_replay(replay: dict, run: SweepRun) -> None:
    document = run.document
    stations = [station["name"] for station in document["ground_segment"]["stations"]]
    link, network = replay.get("link"), replay.get("network")
    if link is not None and link["station"] not in stations:
        raise ValueError(
            f"/replay/link/station={_safe_repr(link['station'])}: run {run.id} has no such "
            f"ground station (it has {stations})"
        )
    if network is not None:
        declared = document["network"]
        if declared is None:
            raise ValueError(f"/replay/network: run {run.id} has no network to replay")
        for index, model in enumerate(network["routing_models"] or ()):
            if model not in declared["route_models"]:
                raise ValueError(
                    f"/replay/network/routing_models/{index}={_safe_repr(model)}: run {run.id} "
                    f"does not list it in network.route_models {declared['route_models']}"
                )


# ----------------------------------------------------------------------------- comparisons


def flatten_metrics(results: Results) -> dict[str, float]:
    """Every pair, station and route metric of a run under the convergence gate's ids.

    ``pair/<station>/<norad>/<metric>``, ``station/<station>/<metric>`` and
    ``route/<source>-><target>/<model>/<metric>``. Never-visible means are NaN.
    """
    tables = results.tables
    metrics: dict[str, float] = {}
    keys = {"station_index", "satellite_index", "norad_id", "name", "pair_index", "model_index"}
    keys |= {"source", "target", "model"}
    stations = tables["station_metrics"]
    names = dict(zip(stations["station_index"].tolist(), stations["name"], strict=True))
    pairs = tables["pair_metrics"]
    for column in pairs.columns:
        if column.name in keys:
            continue
        for station, norad, value in zip(
            pairs["station_index"].tolist(),
            pairs["norad_id"].tolist(),
            pairs[column.name].tolist(),
            strict=True,
        ):
            metrics[f"pair/{names[station]}/{norad}/{column.name}"] = float(value)
    for column in stations.columns:
        if column.name in keys:
            continue
        for name, value in zip(stations["name"], stations[column.name].tolist(), strict=True):
            metrics[f"station/{name}/{column.name}"] = float(value)
    if "route_metrics" in tables:
        routes = tables["route_metrics"]
        for column in routes.columns:
            if column.name in keys:
                continue
            for source, target, model, value in zip(
                routes["source"],
                routes["target"],
                routes["model"],
                routes[column.name].tolist(),
                strict=True,
            ):
                metrics[f"route/{source}->{target}/{model}/{column.name}"] = float(value)
    return metrics


def number_text(value: float) -> str:
    """A metric number for the comparisons CSV: empty for NaN, an integer when it is one."""
    if math.isnan(value):
        return ""
    if math.isfinite(value) and value == int(value) and abs(value) < 1e15:
        return str(int(value))
    return repr(float(value))


def comparison_rows(
    runs: Sequence[tuple[str, Mapping[str, float]]], baseline_id: str
) -> list[dict[str, str]]:
    """Rows ``run_id, metric, value, baseline_value, delta, ratio``, as CSV cell text.

    Every metric of every run gets a row, zero effects included. ``baseline_value`` is the
    metric of the baseline run (empty when it has none). ``delta`` is value - baseline and
    ``ratio`` value / baseline; both are empty when either number is missing, and ``ratio`` is
    empty when the baseline is 0.
    """
    by_id = dict(runs)
    if baseline_id not in by_id:
        raise ValueError(f"baseline run {baseline_id!r} is not among the runs")
    baseline = by_id[baseline_id]
    rows = []
    for run_id, metrics in runs:
        for metric, value in metrics.items():
            reference = baseline.get(metric, math.nan)
            usable = not (math.isnan(value) or math.isnan(reference))
            delta = value - reference if usable else math.nan
            ratio = value / reference if usable and reference != 0.0 else math.nan
            rows.append(
                {
                    "run_id": run_id,
                    "metric": metric,
                    "value": number_text(value),
                    "baseline_value": number_text(reference),
                    "delta": number_text(delta),
                    "ratio": number_text(ratio),
                }
            )
    return rows


def _comparisons_csv(rows: Sequence[Mapping[str, str]]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=COMPARISON_COLUMNS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")


# ----------------------------------------------------------------------------------- replays


def _run_replays(
    run_dir: Path,
    replay_spec: dict,
    packet_backend: Path | None,
    network_backend: Path | None,
) -> dict:
    """The one seam to the trusted packet backends: replay one saved run.

    ``run_dir`` is the run's results directory (``results.json``, ``tables/``, ``inputs/``) and
    is left unchanged. ``replay_spec`` is the normalised ``replay`` block holding only the kinds
    whose backend is available (the others are ``None``). Every replay covers the run's whole
    window: one link replay per offered load, and one network replay of the first network pair
    per routing model (``routing_models``, else all the run's ``network.route_models``) and
    load. Each is published under ``replays/<run id>/link/<load>`` or
    ``replays/<run id>/network/<model>/<load>``, next to ``runs/``. The result, stored under the
    run's ``replay`` key in ``sweep.json``, is ``{"evidence": "actual_ns3", "replays": [{"kind",
    "routing_model", "offered_load_bps", "directory", "summary_file", "summary"}]}``; every
    summary carries an ``aggregate`` accounting of both directions (``reproduce`` checks its
    ``unresolved_packets`` and ``send_error_packets``). A failing replay raises ValueError.
    """
    from openleo.replay import (
        NetworkReplayConfig,
        ReplayConfig,
        ReplaySource,
        run_network_replay,
        run_packet_replay,
    )

    run_dir = Path(run_dir)
    source = ReplaySource.from_results(run_dir)
    root = run_dir.parent.parent
    base = f"replays/{run_dir.name}"
    replays = []
    link = replay_spec.get("link")
    if link is not None:
        for load in link["loads_bps"]:
            directory = f"{base}/link/{number_text(load)}"
            config = ReplayConfig(
                station_name=link["station"],
                norad_id=link["norad_id"],
                start_offset_s=0,
                duration_s=source.window_s,
                offered_load_bps=load,
                packet_size_bytes=link["packet_size_bytes"],
                queue_packets=link["queue_packets"],
                seed=link["seed"],
            )
            summary = run_packet_replay(run_dir, config, packet_backend, root / directory)
            replays.append(
                {
                    "kind": "link",
                    "routing_model": None,
                    "offered_load_bps": load,
                    "directory": directory,
                    "summary_file": "packet-summary.json",
                    "summary": summary,
                }
            )
    network = replay_spec.get("network")
    if network is not None:
        for model in network["routing_models"] or source.route_models:
            for load in network["loads_bps"]:
                directory = f"{base}/network/{model}/{number_text(load)}"
                config = NetworkReplayConfig(
                    routing_model=model,
                    start_offset_s=0,
                    duration_s=source.window_s,
                    offered_load_bps=load,
                    packet_size_bytes=network["packet_size_bytes"],
                    queue_packets=network["queue_packets"],
                    flows_per_direction=network["flows_per_direction"],
                    acquisition_delay_s=network["acquisition_delay_s"],
                    seed=network["seed"],
                )
                summary = run_network_replay(run_dir, config, network_backend, root / directory)
                replays.append(
                    {
                        "kind": "network",
                        "routing_model": model,
                        "offered_load_bps": load,
                        "directory": directory,
                        "summary_file": "network-summary.json",
                        "summary": summary,
                    }
                )
    return {"evidence": NATIVE_REPLAY_EVIDENCE, "replays": replays}


def _replay_evidence(entries: Sequence[dict], missing: Sequence[str]) -> str:
    """``actual`` when every run returned native replays and no kind lacked its backend,
    ``partial`` when some did, ``missing`` when none did."""
    native = [
        isinstance(entry["replay"], dict)
        and entry["replay"].get("evidence") == NATIVE_REPLAY_EVIDENCE
        and bool(entry["replay"].get("replays"))
        for entry in entries
    ]
    if native and all(native) and not missing:
        return "actual"
    return "partial" if any(native) else "missing"


def _replay_plan(replay: dict | None, packet_backend, network_backend) -> tuple[dict | None, list]:
    """The replay spec restricted to the kinds that have a backend, and the kinds that do not."""
    if replay is None:
        return None, []
    backends = {"link": packet_backend, "network": network_backend}
    active = {kind: replay[kind] if backends[kind] is not None else None for kind in REPLAY_KINDS}
    missing = [kind for kind in REPLAY_KINDS if replay[kind] is not None and active[kind] is None]
    if all(value is None for value in active.values()):
        return None, missing
    return active, missing


# --------------------------------------------------------------------------------- running


def _spec_document(spec: dict) -> dict:
    return {key: value for key, value in spec.items() if key not in _LOAD_ONLY_KEYS}


def run_sweep(
    spec_path: Path,
    output_dir: Path,
    *,
    workers: int = 1,
    packet_backend: Path | None = None,
    network_backend: Path | None = None,
) -> dict:
    """Run every scenario of a sweep and publish ``output_dir`` atomically; return sweep.json.

    ``<out>/runs/<id>/`` holds each run's results v2 (archived inputs included),
    ``comparisons.csv`` the deterministic comparisons, ``sweep.json`` the spec, hashes, baseline
    rule and labels, and ``manifest.json`` the SHA-256 of every file. A replay block is run per
    run through the trusted backends; a kind without its backend is recorded as missing, never
    silently dropped. A failing run publishes nothing. Raises FileExistsError if ``output_dir``
    exists.
    """
    spec_path, output_dir = Path(spec_path), Path(output_dir)
    spec, base, runs = prepare_sweep(spec_path)
    document: dict = {}

    def build(stage: Path) -> None:
        document.update(
            execute_sweep(spec, base, runs, stage, workers, packet_backend, network_backend)
        )

    publish_atomically(output_dir, lambda stage: (build(stage), write_manifest(stage)))
    return document


def prepare_sweep(spec_path: Path):
    spec = load_sweep(spec_path)
    base = load_scenario(spec["base_scenario_path"])
    return spec, base, expand_sweep(spec, scenario_document(base))


def execute_sweep(
    spec: dict,
    base,
    runs: Sequence[SweepRun],
    stage: Path,
    workers: int,
    packet_backend: Path | None,
    network_backend: Path | None,
    *,
    progress: Callable[[str], None] | None = None,
    extra_formats: tuple[str, ...] = (),
) -> dict:
    """Run and save every run into ``stage`` and write ``comparisons.csv`` and ``sweep.json``.

    ``progress`` receives ``run <i>/<n>: <phase>`` as each engine phase of each run starts.
    ``extra_formats`` are written for every run besides the formats its scenario asks for.
    """
    base_document = scenario_document(base)
    plan, missing = _replay_plan(spec["replay"], packet_backend, network_backend)
    entries, all_metrics = [], []
    for number, run in enumerate(runs, start=1):
        scenario = parse_scenario(run.document, base.source_path, run.sha256)
        options: dict[str, Any] = {"workers": workers}
        if progress is not None:
            prefix = f"run {number}/{len(runs)}: "
            options["progress"] = lambda phase, prefix=prefix: progress(prefix + phase)
        try:
            results = run_engine(scenario, **options)
            formats = (*scenario.outputs.formats, *extra_formats) if extra_formats else None
            directory = save_run(scenario, results, stage / "runs" / run.id, formats)
            replay = (
                _run_replays(directory, plan, packet_backend, network_backend) if plan else None
            )
        except ValueError as exc:
            raise ValueError(f"run {run.id}: {exc}") from exc
        metrics = flatten_metrics(results)
        all_metrics.append((run.id, metrics))
        entries.append(
            {
                "id": run.id,
                "label": run.label,
                "directory": f"runs/{run.id}",
                "scenario_sha256": run.sha256,
                "base_sha256": document_sha256(base_document),
                "metric_count": len(metrics),
                "replay": replay,
            }
        )
    baseline = baseline_run_id(spec, runs)
    rows = comparison_rows(all_metrics, baseline)
    (stage / "comparisons.csv").write_bytes(_comparisons_csv(rows))
    limitations = list(LIMITATIONS)
    if missing:
        limitations.append(_MISSING_REPLAY_LIMITATION.format(kinds=" and ".join(missing)))
    requested = spec["replay"]
    if requested is None:
        replay_block = None
    else:
        replay_block = {"evidence": _replay_evidence(entries, missing), "missing": missing}
    document = {
        "kind": SWEEP_RESULT_KIND,
        "schema_version": SWEEP_RESULT_SCHEMA_VERSION,
        "name": spec["name"],
        "mode": spec["mode"],
        "comparison_label": COMPARISON_LABEL,
        "spec": _spec_document(spec),
        "spec_sha256": spec["spec_sha256"],
        "base": {
            "source_sha256": base.source_sha256,
            "normalised_sha256": document_sha256(base_document),
        },
        "baseline_run_id": baseline,
        "baseline_rule": BASELINE_RULES[spec["mode"]],
        "runs": entries,
        "comparisons": {
            "file": "comparisons.csv",
            "columns": list(COMPARISON_COLUMNS),
            "rows": len(rows),
            "label": COMPARISON_LABEL,
        },
        "replay": replay_block,
        "software": assembly.software(),
        "limitations": limitations,
    }
    document = assembly.json_safe(document)
    text = json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
    (stage / "sweep.json").write_bytes((text + "\n").encode("utf-8"))
    return document


__all__ = [
    "COMPARISON_LABEL",
    "MAX_RUNS",
    "SWEEP_SCHEMA_ID",
    "SweepRun",
    "apply_patch",
    "baseline_run_id",
    "comparison_rows",
    "document_sha256",
    "expand_sweep",
    "flatten_metrics",
    "load_sweep",
    "number_text",
    "parse_sweep",
    "run_sweep",
    "spec_touches",
]
