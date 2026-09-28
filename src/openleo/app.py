"""A bounded, loopback-only HTTP workbench using the Python standard library."""

from __future__ import annotations

import hmac
import json
import secrets
import sys
import webbrowser
from dataclasses import replace
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, HTTPServer
from io import BytesIO
from mimetypes import guess_type
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from openleo.constellation import (
    ConstellationScenario,
    parse_constellation,
    scenario_document,
    simulate_constellation,
)
from openleo.network import add_network
from openleo.workbench import _json, _unique_object, render_workbench


def _snapshot(
    scenario: ConstellationScenario,
    token: str,
    hash_encoding: str = "original UTF-8 file bytes",
    canonical_json: str | None = None,
) -> tuple[bytes, bytes]:
    document = add_network(simulate_constellation(scenario))
    document = {
        **document,
        "provenance": {
            **document["provenance"],
            "scenario_hash_encoding": hash_encoding,
            **({"scenario_canonical_json": canonical_json} if canonical_json is not None else {}),
        },
    }
    return (
        _json(document).encode("utf-8"),
        render_workbench(document, live=True, token=token).encode("utf-8"),
    )


def _input_error(error: Exception) -> str:
    message = str(error.__cause__ or error)
    for field in (
        "time_window",
        "radio_link",
        "adaptation",
        "propagation",
        "network",
        "stations",
        "ground_station",
        "name",
    ):
        if message.startswith(field):
            return f"Invalid {field}. Check its values and limits."
    return "Could not run this scenario. Check its fields and catalog."


def _study_artifacts(scenario, settings, packet_backend, network_backend):
    from openleo.experiment import parse_experiment_config, run_experiment

    config = parse_experiment_config(settings)
    with TemporaryDirectory(prefix="openleo-experiment-") as temporary:
        directory = Path(temporary) / "result"
        summary = run_experiment(
            scenario,
            config,
            directory,
            packet_backend=packet_backend,
            network_backend=network_backend,
        )
        paths = tuple(path for path in sorted(directory.rglob("*")) if path.is_file())
        if sum(path.stat().st_size for path in paths) > 64_000_000:
            raise ValueError("experiment exceeds the 64 MB browser artifact limit; use the CLI")
        contents = {path.relative_to(directory).as_posix(): path.read_bytes() for path in paths}
    identifier = sha256(contents["experiment-study.json"]).hexdigest()
    prefix = f"/api/study/{identifier}/"
    archive_data = BytesIO()
    with ZipFile(archive_data, "w", compression=ZIP_DEFLATED) as archive:
        for name, content in contents.items():
            info = ZipInfo(name, date_time=(2000, 1, 1, 0, 0, 0))
            info.external_attr = 0o100644 << 16
            archive.writestr(info, content, compress_type=ZIP_DEFLATED)
    artifacts = {
        prefix + name: (content, guess_type(name)[0] or "application/octet-stream")
        for name, content in contents.items()
    }
    artifacts = {**artifacts, prefix + "archive.zip": (archive_data.getvalue(), "application/zip")}
    response = {
        "summary": summary,
        "report_url": prefix + "index.html",
        "archive_url": prefix + "archive.zip",
    }
    return response, artifacts


def create_server(
    scenario: ConstellationScenario,
    port: int = 0,
    *,
    packet_backend: str | Path | None = None,
    network_backend: str | Path | None = None,
) -> HTTPServer:
    """Compute the initial experiment and bind exclusively to IPv4 loopback."""
    if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
        raise ValueError("port must be an integer between 0 and 65535")
    token = secrets.token_urlsafe(32)
    pinned_orbit = scenario_document(scenario)["orbit"]
    current = _snapshot(scenario, token)
    study_files = {}
    recent_requests = ()

    class Handler(BaseHTTPRequestHandler):
        timeout = 5

        def log_message(self, format, *args):
            # Request lines, including query strings, may contain private values.
            pass

        def log_request(self, code="-", size="-"):
            print(f"OpenLEO HTTP {code}", file=sys.stderr)

        def _reply(self, status: int, content: bytes, content_type="application/json"):
            self.send_response(status)
            encoding = (
                "; charset=utf-8"
                if content_type.startswith("text/") or content_type == "application/json"
                else ""
            )
            self.send_header("Content-Type", content_type + encoding)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.end_headers()
            self.wfile.write(content)

        def _error(self, status: int, message: str):
            self._reply(status, _json({"error": message}).encode("utf-8"))

        def parse_request(self):
            nonlocal recent_requests
            if not super().parse_request():
                return False
            hosts = self.headers.get_all("Host", [])
            allowed = {
                f"127.0.0.1:{self.server.server_port}",
                f"localhost:{self.server.server_port}",
            }
            if len(hosts) != 1 or hosts[0] not in allowed:
                self._error(403, "A matching local Host header is required.")
                return False
            origins = self.headers.get_all("Origin", [])
            if origins and origins != [f"http://{hosts[0]}"]:
                self._error(403, "The request origin must match this local application.")
                return False
            now = monotonic()
            recent_requests = tuple(when for when in recent_requests if now - when < 1.0)
            if len(recent_requests) >= 60:
                self._error(429, "Too many requests; retry after one second.")
                return False
            recent_requests = (*recent_requests, now)
            return True

        def do_GET(self):
            if self.path == "/":
                self._reply(200, current[1], "text/html")
            elif self.path == "/api/experiment":
                self._reply(200, current[0])
            elif self.path == "/api/capabilities":
                self._reply(
                    200,
                    _json(
                        {
                            "link_replay": packet_backend is not None,
                            "network_replay": network_backend is not None,
                        }
                    ).encode("utf-8"),
                )
            elif self.path in study_files:
                content, content_type = study_files[self.path]
                self._reply(200, content, content_type)
            else:
                self._error(404, "Unknown endpoint.")

        def do_POST(self):
            nonlocal current, study_files
            if self.path not in ("/api/simulate", "/api/study"):
                self._error(404, "Unknown endpoint.")
                return
            tokens = self.headers.get_all("X-OpenLEO-Token", [])
            if len(tokens) != 1 or not hmac.compare_digest(
                tokens[0].encode("utf-8"), token.encode("ascii")
            ):
                self._error(403, "A valid session token is required.")
                return
            if (
                len(self.headers.get_all("Content-Type", [])) != 1
                or self.headers.get_content_type() != "application/json"
            ):
                self._error(415, "Content-Type must be application/json.")
                return
            if self.headers.get_all("Transfer-Encoding"):
                self._error(400, "Transfer-Encoding is not supported.")
                return
            lengths = self.headers.get_all("Content-Length", [])
            if not lengths:
                self._error(411, "Content-Length is required.")
                return
            if (
                len(lengths) != 1
                or not lengths[0].isascii()
                or not lengths[0].isdecimal()
                or len(lengths[0]) > 9
            ):
                self._error(400, "Invalid Content-Length.")
                return
            length = int(lengths[0])
            if length > 1_000_000:
                self._error(413, "The scenario request exceeds 1000000 bytes.")
                return
            try:
                body = self.rfile.read(length)
                if len(body) != length:
                    raise ValueError("incomplete request")
                payload = json.loads(body.decode("utf-8"), object_pairs_hook=_unique_object)
                required = {"scenario", "settings"} if self.path == "/api/study" else {"scenario"}
                if not isinstance(payload, dict) or set(payload) != required:
                    raise ValueError("invalid envelope")
                raw = payload["scenario"]
                _json(raw).encode("utf-8")  # Reject non-finite values at any depth.
                if not isinstance(raw, dict) or raw.get("orbit") != pinned_orbit:
                    self._error(400, "The catalog path and provenance cannot be changed here.")
                    return
                updated = parse_constellation(raw, scenario.source_path, scenario.source_sha256)
                canonical = _json(scenario_document(updated)).encode("utf-8")
                updated = replace(updated, source_sha256=sha256(canonical).hexdigest())
                if self.path == "/api/study":
                    result, next_files = _study_artifacts(
                        updated, payload["settings"], packet_backend, network_backend
                    )
                    study_files = next_files
                    self._reply(200, _json(result).encode("utf-8"))
                    return
                next_snapshot = _snapshot(
                    updated, token, "canonical JSON sorted compact UTF-8", canonical.decode("utf-8")
                )
            except TimeoutError:
                self._error(408, "Timed out while reading the request body.")
                return
            except (ValueError, TypeError, KeyError, OSError, RecursionError, OverflowError) as exc:
                self._error(400, _input_error(exc))
                return
            except Exception as exc:  # noqa: BLE001 — never expose internal paths or values over HTTP.
                print(f"OpenLEO simulation failed: {type(exc).__name__}.", file=sys.stderr)
                self._error(500, "The simulation failed; the previous experiment is preserved.")
                return
            current = next_snapshot
            self._reply(200, current[0])

    return HTTPServer(("127.0.0.1", port), Handler)


def run_app(
    scenario: ConstellationScenario,
    port: int = 8765,
    open_browser: bool = True,
    *,
    packet_backend: str | Path | None = None,
    network_backend: str | Path | None = None,
) -> None:
    """Serve the local workbench until Ctrl-C, then release its listening socket."""
    with create_server(
        scenario, port, packet_backend=packet_backend, network_backend=network_backend
    ) as server:
        url = f"http://127.0.0.1:{server.server_port}/"
        print(f"workbench: {url}", flush=True)
        if open_browser:
            webbrowser.open(url)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
