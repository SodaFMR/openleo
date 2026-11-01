"""The routes of the v2 app API (design spec section 10), independent of HTTP framing.

=====================================================  ======  =====  ==============================
Route                                                  Method  Token  Response
=====================================================  ======  =====  ==============================
``/``                                                  GET     no     ``_web/dist/index.html``
``/api/capabilities``                                  GET     no     versions, backends, limits
``/api/scenario``                                      GET     yes    scenario, its hash, JSON Schema
``/api/scenario/validate``                             POST    yes    ``{valid, scenario | errors}``
``/api/runs``                                          POST    yes    ``{scenario}`` -> 202 ``{id}``
``/api/runs/{id}``                                     GET     yes    ``{id, state, phase, error}``
``/api/runs/{id}/results.json``                        GET     yes    the run's ``results.json``
``/api/runs/{id}/tables/{name}.olc``                   GET     yes    one OLC table
``/api/runs/{id}/export.zip``                          GET     yes    the run directory as a ZIP
``/api/sweeps``                                        POST    yes    ``{spec[, scenario]}`` -> 202
``/api/sweeps/{id}``                                   GET     yes    status, as for runs
``/api/sweeps/{id}/results.json``                      GET     yes    the sweep's ``sweep.json``
``/api/sweeps/{id}/tables/comparisons.olc``            GET     yes    the comparisons as OLC
``/api/sweeps/{id}/export.zip``                        GET     yes    the sweep directory as a ZIP
``/api/sweeps/{id}/runs/{n}/results.json``             GET     yes    run ``n`` of ``sweep.json``
``/api/sweeps/{id}/runs/{n}/tables/{name}.olc``        GET     yes    a table of run ``n``
=====================================================  ======  =====  ==============================

Every scenario a request carries is validated by the scenario parser against the directory of
the scenario the server was started with, and its normalised ``space_segment`` must equal that
scenario's (403 otherwise): the browser can never name a file. Sweep specs carry no
``base_scenario``: the base is the submitted scenario, else the startup one. Backends are the
ones given at startup; no request field names an executable or an output path.

Job ids are the SHA-256 of the normalised request (``jobs.job_id``). Results live under a
server-owned temporary directory that ``close`` removes. Tables are served from the OLC files of
the run directory (the app always writes OLC); results are never re-read from CSV here. A job
whose files exceed the artifact limit fails and leaves nothing behind.
"""

from __future__ import annotations

import csv
import importlib.util
import json
import re
import secrets
import shutil
import tempfile
import threading
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

import numpy as np

from openleo import __version__
from openleo._validation import MAX_SCENARIO_BYTES
from openleo.app.jobs import JobQueue, QueueFull
from openleo.engine._assembly import canonical_sha256
from openleo.engine.run import PHASES, save_run
from openleo.engine.run import run as run_engine
from openleo.results import Column, Table, encode_table, publish_atomically, write_manifest
from openleo.results.io import FORMATS
from openleo.scenario import parse_scenario, scenario_document, scenario_json_schema
from openleo.scenario.parse import SCENARIO_SCHEMA_ID
from openleo.sweep import (
    MAX_RUNS,
    SWEEP_SCHEMA_ID,
    execute_sweep,
    expand_sweep,
    parse_sweep,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from openleo.scenario.model import Scenario

API_VERSION = 2
MAX_BODY_BYTES = MAX_SCENARIO_BYTES
DEFAULT_ARTIFACT_LIMIT_BYTES = 256 * 2**20
RATE_LIMIT_PER_S = 60
INDEX_PAGE = Path(__file__).resolve().parent.parent / "_web" / "dist" / "index.html"
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'; "
    "object-src 'none'"
)
ZIP_DATE_TIME = (2000, 1, 1, 0, 0, 0)
PINNED = "The catalog is pinned: space_segment must equal the scenario the app was started with."
_URL_KINDS = {"runs": "run", "sweeps": "sweep"}

_JOB = re.compile(r"/api/(runs|sweeps)/([0-9a-f]{64})(?:/(.*))?")
_TABLE = re.compile(r"tables/([a-z][a-z0-9_]*)\.olc")
_SWEEP_RUN = re.compile(r"runs/(0|[1-9][0-9]{0,3})/(results\.json|tables/([a-z][a-z0-9_]*)\.olc)")
_POINTER = re.compile(r"/[^=\s:]*")
_COMPARISON_COLUMNS = (
    Column("run_id", "str", "", "sweep run id"),
    Column("metric", "str", "", "flattened metric name"),
    Column("value", "f64", "", "metric value of the run"),
    Column("baseline_value", "f64", "", "metric value of the baseline run"),
    Column("delta", "f64", "", "value - baseline_value"),
    Column("ratio", "f64", "", "value / baseline_value"),
)


class ApiError(Exception):
    """A request the API refuses: an HTTP status and a message safe to show the browser."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass(frozen=True)
class Response:
    status: int
    body: bytes = b""
    content_type: str = "application/json"
    file: Path | None = None
    headers: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class Route:
    methods: tuple[str, ...]
    token: bool
    handler: Callable[[Any], Response]


def json_bytes(document: Any) -> bytes:
    """Compact UTF-8 JSON; ValueError for NaN or an infinity at any depth."""
    text = json.dumps(document, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    return text.encode("utf-8")


def _json(document: Any, status: int = 200) -> Response:
    return Response(status, json_bytes(document))


def error_pointer(message: str) -> str | None:
    """The RFC 6901 pointer a parser message starts with ("" for the root), else None."""
    if message.startswith("/ "):
        return ""
    match = _POINTER.match(message)
    return None if match is None else match[0]


def _envelope(payload: Any, required: set[str], optional: frozenset = frozenset()) -> dict:
    if not isinstance(payload, dict) or not required <= set(payload) <= required | optional:
        keys = ", ".join(sorted(required)) + "".join(f"[, {key}]" for key in sorted(optional))
        raise ApiError(400, f"The request body must be a JSON object with exactly: {keys}.")
    return payload


def _index_page(token: str) -> bytes:
    page = INDEX_PAGE.read_text(encoding="utf-8")
    if "<head>" not in page:
        raise ValueError(f"{INDEX_PAGE.name} has no <head> tag for the session token")
    meta = f'<meta name="openleo-token" content="{token}">'
    return page.replace("<head>", "<head>\n" + meta, 1).encode("utf-8")


def _comparisons_table(path: Path) -> Table:
    with path.open(encoding="utf-8", newline="") as file:
        rows = list(csv.DictReader(file))
    data: dict[str, Any] = {}
    for column in _COMPARISON_COLUMNS:
        cells = [row[column.name] for row in rows]
        if column.dtype == "str":
            data[column.name] = tuple(cells)
        else:
            data[column.name] = np.array([float(c) if c else np.nan for c in cells])
    return Table("comparisons", _COMPARISON_COLUMNS, data)


class Api:
    """The API state of one server: the pinned scenario, the session token and the jobs."""

    def __init__(
        self,
        scenario: Scenario,
        *,
        packet_backend: Path | None,
        network_backend: Path | None,
        artifact_limit_bytes: int,
    ) -> None:
        self.scenario = scenario
        self.document = scenario_document(scenario)
        self.packet_backend = packet_backend
        self.network_backend = network_backend
        self.artifact_limit_bytes = artifact_limit_bytes
        self.token = secrets.token_urlsafe(32)
        self._index = _index_page(self.token)
        self._scenario_body = json_bytes(
            {
                "scenario": self.document,
                "source_sha256": scenario.source_sha256,
                "schema": scenario_json_schema(),
            }
        )
        self._export_lock = threading.Lock()
        self.root = Path(tempfile.mkdtemp(prefix="openleo-app-"))
        directory = scenario.source_path.parent
        redactions = [
            (str(self.root), "<results>"),
            (str(scenario.source_path), scenario.source_path.name),
            (str(directory), "."),
        ]
        for catalog, entry in zip(
            scenario.space_segment.catalogs, self.document["space_segment"]["catalogs"], strict=True
        ):
            redactions.append((str(catalog.path), entry["path"]))
        # A filesystem root is never redacted: replacing "/" would mangle every JSON pointer.
        redactions = [(p, shown) for p, shown in redactions if Path(p).parent != Path(p)]
        self._redactions = sorted(redactions, key=lambda pair: len(pair[0]), reverse=True)
        self.jobs = JobQueue(self._execute)
        self._capabilities = json_bytes(self._capability_document())
        self._routes = {
            "/": Route(("GET",), False, self._get_index),
            "/api/capabilities": Route(
                ("GET",), False, lambda _: Response(200, self._capabilities)
            ),
            "/api/scenario": Route(("GET",), True, lambda _: Response(200, self._scenario_body)),
            "/api/scenario/validate": Route(("POST",), True, self._post_validate),
            "/api/runs": Route(("POST",), True, self._post_run),
            "/api/sweeps": Route(("POST",), True, self._post_sweep),
        }

    def close(self) -> None:
        self.jobs.close()
        shutil.rmtree(self.root, ignore_errors=True)

    def public(self, message: str) -> str:
        """``message`` with the server's directories replaced by names the browser knows."""
        for private, shown in self._redactions:
            message = message.replace(private, shown)
        return message

    # ------------------------------------------------------------------------------ routing

    def route(self, path: str) -> Route | None:
        if path in self._routes:
            return self._routes[path]
        match = _JOB.fullmatch(path)
        if match is None:
            return None
        kind, identifier, rest = _URL_KINDS[match[1]], match[2], match[3]
        if rest is None:
            return Route(("GET",), True, lambda _: self._get_status(kind, identifier))
        if rest == "results.json":
            name = "results.json" if kind == "run" else "sweep.json"
            return self._artifact(kind, identifier, lambda d: (d / name, "application/json"))
        if rest == "export.zip":
            return self._artifact(kind, identifier, lambda d: (self._export(d), "application/zip"))
        table = _TABLE.fullmatch(rest)
        if table is not None:
            return self._artifact(kind, identifier, lambda d: self._table(kind, d, table[1]))
        run = _SWEEP_RUN.fullmatch(rest) if kind == "sweep" else None
        if run is not None:
            return self._artifact(kind, identifier, lambda d: self._sweep_run(d, run))
        return None

    def _artifact(self, kind: str, identifier: str, locate: Callable) -> Route:
        def handler(_: Any) -> Response:
            path, content_type = locate(self._finished(kind, identifier))
            return Response(200, content_type=content_type, file=path)

        return Route(("GET",), True, handler)

    # ------------------------------------------------------------------------------ handlers

    def _get_index(self, _: Any) -> Response:
        headers = (("Content-Security-Policy", CONTENT_SECURITY_POLICY),)
        return Response(200, self._index, "text/html", headers=headers)

    def _capability_document(self) -> dict:
        return {
            "api_version": API_VERSION,
            "openleo": __version__,
            "scenario_schema": SCENARIO_SCHEMA_ID,
            "sweep_schema": SWEEP_SCHEMA_ID,
            "backends": {
                "link_replay": self.packet_backend is not None,
                "network_replay": self.network_backend is not None,
            },
            "extras": {
                "plot": importlib.util.find_spec("matplotlib") is not None,
                "parquet": importlib.util.find_spec("pyarrow") is not None,
            },
            "formats": list(FORMATS),
            "phases": list(PHASES),
            "limits": {
                "request_bytes": MAX_BODY_BYTES,
                "artifact_bytes": self.artifact_limit_bytes,
                "requests_per_second": RATE_LIMIT_PER_S,
                "pending_jobs": self.jobs.max_pending,
                "sweep_runs": MAX_RUNS,
            },
        }

    def _post_validate(self, payload: Any) -> Response:
        raw = _envelope(payload, {"scenario"})["scenario"]
        try:
            document, _ = self._submitted(raw)
        except ApiError as exc:
            if exc.status != 400:
                raise
            errors = [{"pointer": error_pointer(exc.message), "message": exc.message}]
            return _json({"valid": False, "errors": errors})
        return _json({"valid": True, "scenario": document, "sha256": canonical_sha256(document)})

    def _post_run(self, payload: Any) -> Response:
        raw = _envelope(payload, {"scenario"})["scenario"]
        document, _ = self._submitted(raw)
        return self._submit("run", {"scenario": document})

    def _post_sweep(self, payload: Any) -> Response:
        payload = _envelope(payload, {"spec"}, frozenset({"scenario"}))
        document, _ = self._submitted(payload.get("scenario", self.document))
        raw = payload["spec"]
        if isinstance(raw, Mapping) and "base_scenario" in raw:
            raise ApiError(400, "/base_scenario is not allowed: the base is the app's scenario.")
        try:
            spec = self._spec(raw)
            expand_sweep(spec, document)
        except ValueError as exc:
            raise ApiError(400, self.public(str(exc))) from None
        normalised = {
            key: value
            for key, value in spec.items()
            if value is not None
            and key not in ("base_scenario", "base_scenario_path", "spec_sha256", "source_bytes")
        }
        return self._submit("sweep", {"scenario": document, "spec": normalised})

    def _submit(self, kind: str, payload: dict) -> Response:
        try:
            identifier = self.jobs.submit(kind, payload)
        except QueueFull:
            raise ApiError(503, "The job queue is full; retry when a job has finished.") from None
        return _json({"id": identifier}, 202)

    def _get_status(self, kind: str, identifier: str) -> Response:
        if self.jobs.kind(identifier) != kind:
            raise ApiError(404, f"Unknown {kind}.")
        return _json(self.jobs.status(identifier))

    # ------------------------------------------------------------------------- validation

    def _parse(self, document: Any) -> Scenario:
        """Parse against the startup scenario's directory; the hash is the canonical JSON's."""
        return parse_scenario(document, self.scenario.source_path, canonical_sha256(document))

    def _submitted(self, raw: Any) -> tuple[dict, Scenario]:
        """A browser scenario: (its normalised document, the scenario), the catalog pinned."""
        try:
            canonical_sha256(raw)
            scenario = self._parse(raw)
        except ValueError as exc:
            raise ApiError(400, self.public(str(exc))) from None
        document = scenario_document(scenario)
        if document["space_segment"] != self.document["space_segment"]:
            raise ApiError(403, PINNED)
        return document, replace(scenario, source_sha256=canonical_sha256(document))

    def _spec(self, raw: Any) -> dict:
        """A sweep spec over the app's scenario; ``base_scenario`` is set here, never sent."""
        if isinstance(raw, Mapping):
            raw = {**raw, "base_scenario": self.scenario.source_path.name}
        spec = parse_sweep(raw, json_bytes(raw))
        spec["base_scenario_path"] = str(self.scenario.source_path)
        return spec

    # --------------------------------------------------------------------------- execution

    def _formats(self, scenario: Scenario) -> tuple[str, ...]:
        return tuple(f for f in FORMATS if f in scenario.outputs.formats or f == "olc")

    def _execute(self, kind: str, payload: dict) -> Path:
        """The job queue's ``run_fn``: publish one run or sweep under the results directory."""
        directory = Path(tempfile.mkdtemp(prefix=f"{kind}-", dir=self.root))
        result = directory / "result"
        try:
            if kind == "run":
                scenario = self._parse(payload["scenario"])
                results = run_engine(scenario, progress=self.jobs.report)
                save_run(scenario, results, result, self._formats(scenario))
            else:
                self._execute_sweep(payload, result)
            self._check_size(result)
        except ValueError as exc:
            shutil.rmtree(directory, ignore_errors=True)
            raise ValueError(self.public(str(exc))) from None
        except BaseException:
            shutil.rmtree(directory, ignore_errors=True)
            raise
        return result

    def _execute_sweep(self, payload: dict, result: Path) -> None:
        base = self._parse(payload["scenario"])
        spec = self._spec(payload["spec"])
        runs = expand_sweep(spec, payload["scenario"])

        def build(stage: Path) -> None:
            execute_sweep(
                spec,
                base,
                runs,
                stage,
                1,
                self.packet_backend,
                self.network_backend,
                progress=self.jobs.report,
                extra_formats=("olc",),
            )
            write_manifest(stage)

        publish_atomically(result, build)
        table = _comparisons_table(result / "comparisons.csv")
        (result.parent / "comparisons.olc").write_bytes(encode_table(table))

    def _check_size(self, result: Path) -> None:
        total = sum(p.stat().st_size for p in result.parent.rglob("*") if p.is_file())
        if total > self.artifact_limit_bytes:
            raise ValueError(
                f"the results take {total} bytes, more than the artifact limit of "
                f"{self.artifact_limit_bytes} bytes; run this with the CLI instead"
            )

    # --------------------------------------------------------------------------- artifacts

    def _finished(self, kind: str, identifier: str) -> Path:
        if self.jobs.kind(identifier) != kind:
            raise ApiError(404, f"Unknown {kind}.")
        found = self.jobs.result(identifier)
        if found is None:
            state = self.jobs.status(identifier)["state"]
            raise ApiError(409, f"The {kind} is not finished: its state is {state}.")
        return found[1]

    def _table(self, kind: str, result: Path, name: str) -> tuple[Path, str]:
        if kind == "sweep":
            path = result.parent / "comparisons.olc" if name == "comparisons" else None
        else:
            path = result / "tables" / f"{name}.olc"
        if path is None or not path.is_file():
            raise ApiError(404, "Unknown table.")
        return path, "application/octet-stream"

    def _sweep_run(self, result: Path, match: re.Match) -> tuple[Path, str]:
        runs = json.loads((result / "sweep.json").read_bytes())["runs"]
        index = int(match[1])
        if index >= len(runs):
            raise ApiError(404, "Unknown sweep run.")
        directory = (result / runs[index]["directory"]).resolve()
        if not directory.is_relative_to(result.resolve()):  # pragma: no cover - our own output
            raise ApiError(404, "Unknown sweep run.")
        if match[3] is None:
            return directory / "results.json", "application/json"
        return self._table("run", directory, match[3])

    def _export(self, result: Path) -> Path:
        """The result directory as a ZIP with sorted entries and fixed metadata (built once)."""
        archive = result.parent / "export.zip"
        with self._export_lock:
            if not archive.exists():
                partial = result.parent / "export.zip.partial"
                files = sorted(
                    (p.relative_to(result).as_posix(), p) for p in result.rglob("*") if p.is_file()
                )
                with ZipFile(partial, "w", compression=ZIP_DEFLATED) as zipped:
                    for name, path in files:
                        info = ZipInfo(name, date_time=ZIP_DATE_TIME)
                        info.compress_type = ZIP_DEFLATED
                        info.create_system = 3
                        info.external_attr = 0o100644 << 16
                        info.file_size = path.stat().st_size
                        with path.open("rb") as source, zipped.open(info, "w") as target:
                            shutil.copyfileobj(source, target)
                partial.replace(archive)
        return archive


__all__ = ["DEFAULT_ARTIFACT_LIMIT_BYTES", "MAX_BODY_BYTES", "Api", "ApiError", "Response"]
