"""The loopback HTTP server of the v2 app: the v1 security model around ``openleo.app.api``.

Every request must carry exactly one ``Host`` naming this server (``127.0.0.1:<port>`` or
``localhost:<port>``) and, if it has an ``Origin``, exactly ``http://<that host>``; at most 60
such requests are served per second. Routes that need the session token (every POST and every
data GET, see ``api``) require exactly one matching ``X-OpenLEO-Token`` header. POST bodies must
be ``application/json`` with one decimal ``Content-Length`` of at most 1 000 000 bytes, no
``Transfer-Encoding``, and JSON without repeated keys or non-finite numbers. Every response is
``no-store``, ``nosniff`` and ``DENY``-framed, and every error is ``{"error": message}``. Request
lines are never logged (they may hold private values), and unexpected exceptions are logged by
type only.
"""

from __future__ import annotations

import hmac
import json
import shutil
import sys
import threading
import webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING, Any

from openleo._validation import _unique_object
from openleo.app.api import (
    DEFAULT_ARTIFACT_LIMIT_BYTES,
    MAX_BODY_BYTES,
    RATE_LIMIT_PER_S,
    Api,
    ApiError,
    Response,
    json_bytes,
)

if TYPE_CHECKING:
    from openleo.scenario.model import Scenario


class AppServer(ThreadingHTTPServer):
    """A threading HTTP server bound to IPv4 loopback that owns its ``Api``."""

    daemon_threads = True

    def __init__(self, port: int, api: Api) -> None:
        self.api = api
        self._recent: deque[float] = deque()
        self._rate_lock = threading.Lock()
        super().__init__(("127.0.0.1", port), _Handler)

    def admit(self, now: float) -> bool:
        """Count one matching request; False when 60 were served in the last second."""
        with self._rate_lock:
            while self._recent and now - self._recent[0] >= 1.0:
                self._recent.popleft()
            if len(self._recent) >= RATE_LIMIT_PER_S:
                return False
            self._recent.append(now)
            return True

    def server_close(self) -> None:
        try:
            super().server_close()
        finally:
            self.api.close()


class _Handler(BaseHTTPRequestHandler):
    timeout = 5
    server: AppServer

    def version_string(self) -> str:
        return "OpenLEO"

    def log_message(self, format: str, *args: Any) -> None:
        # Request lines, including query strings, may contain private values.
        pass

    def log_request(self, code: Any = "-", size: Any = "-") -> None:
        print(f"OpenLEO HTTP {code}", file=sys.stderr)

    def _reply(self, response: Response) -> None:
        self.send_response(response.status)
        content_type = response.content_type
        if content_type.startswith("text/") or content_type == "application/json":
            content_type += "; charset=utf-8"
        length = response.file.stat().st_size if response.file else len(response.body)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        for name, value in response.headers:
            self.send_header(name, value)
        self.end_headers()
        if getattr(self, "command", None) == "HEAD":
            return
        if response.file is None:
            self.wfile.write(response.body)
        else:
            with response.file.open("rb") as source:
                shutil.copyfileobj(source, self.wfile)

    def _error(self, status: int, message: str, headers: tuple = ()) -> None:
        self._reply(Response(status, json_bytes({"error": message}), headers=headers))

    def send_error(self, code: int, message: str | None = None, explain: str | None = None):
        """The standard library's framing errors, as JSON (never echoing the request)."""
        self.close_connection = True
        reason = self.responses.get(code, ("Error",))[0]
        self._error(code, f"{reason}.")

    def parse_request(self) -> bool:
        if not super().parse_request():
            return False
        hosts = self.headers.get_all("Host", [])
        port = self.server.server_port
        if len(hosts) != 1 or hosts[0] not in {f"127.0.0.1:{port}", f"localhost:{port}"}:
            self._error(403, "A matching local Host header is required.")
            return False
        origins = self.headers.get_all("Origin", [])
        if origins and origins != [f"http://{hosts[0]}"]:
            self._error(403, "The request origin must match this local application.")
            return False
        if not self.server.admit(monotonic()):
            self._error(429, "Too many requests; retry after one second.")
            return False
        return True

    def _token_ok(self) -> bool:
        tokens = self.headers.get_all("X-OpenLEO-Token", [])
        return len(tokens) == 1 and hmac.compare_digest(
            tokens[0].encode("utf-8", "surrogateescape"), self.server.api.token.encode("ascii")
        )

    def _read_json(self) -> Any:
        if (
            len(self.headers.get_all("Content-Type", [])) != 1
            or self.headers.get_content_type() != "application/json"
        ):
            raise ApiError(415, "Content-Type must be application/json.")
        if self.headers.get_all("Transfer-Encoding"):
            raise ApiError(400, "Transfer-Encoding is not supported.")
        lengths = self.headers.get_all("Content-Length", [])
        if not lengths:
            raise ApiError(411, "Content-Length is required.")
        if (
            len(lengths) != 1
            or not lengths[0].isascii()
            or not lengths[0].isdecimal()
            or len(lengths[0]) > 9
        ):
            raise ApiError(400, "Invalid Content-Length.")
        length = int(lengths[0])
        if length > MAX_BODY_BYTES:
            raise ApiError(413, f"The request exceeds {MAX_BODY_BYTES} bytes.")
        try:
            body = self.rfile.read(length)
        except TimeoutError:
            raise ApiError(408, "Timed out while reading the request body.") from None
        if len(body) != length:
            raise ApiError(400, "The request body is incomplete.")
        try:
            payload = json.loads(body.decode("utf-8"), object_pairs_hook=_unique_object)
            json_bytes(payload)  # rejects NaN and infinities at any depth
        except (ValueError, RecursionError):
            raise ApiError(400, "The request body is not valid JSON.") from None
        return payload

    def _dispatch(self) -> None:
        method = self.command
        route = self.server.api.route(self.path)
        if route is None:
            self._error(404, "Unknown endpoint.")
            return
        if method not in route.methods:
            allow = ", ".join(route.methods)
            self._error(405, f"Use {allow} here.", (("Allow", allow),))
            return
        if route.token and not self._token_ok():
            self._error(403, "A valid session token is required.")
            return
        try:
            payload = self._read_json() if method == "POST" else None
            response = route.handler(payload)
        except ApiError as exc:
            self._error(exc.status, exc.message)
            return
        except Exception as exc:  # noqa: BLE001 - never expose internal paths or values over HTTP
            print(f"OpenLEO request failed: {type(exc).__name__}.", file=sys.stderr)
            self._error(500, "The request failed; see the server log.")
            return
        self._reply(response)

    do_GET = do_POST = do_HEAD = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _dispatch


def create_server(
    scenario: Scenario,
    port: int = 0,
    *,
    packet_backend: Path | None = None,
    network_backend: Path | None = None,
    artifact_limit_bytes: int = DEFAULT_ARTIFACT_LIMIT_BYTES,
) -> AppServer:
    """Bind the v2 API to ``127.0.0.1:port`` (0 picks a free port); nothing runs until asked.

    The backends are trusted executables chosen by whoever starts the server; requests can only
    ask for replays through them. ``server_close`` stops the job worker and removes every
    result.
    """
    if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
        raise ValueError("port must be an integer between 0 and 65535")
    if (
        isinstance(artifact_limit_bytes, bool)
        or not isinstance(artifact_limit_bytes, int)
        or artifact_limit_bytes < 1
    ):
        raise ValueError("artifact_limit_bytes must be a positive integer")
    api = Api(
        scenario,
        packet_backend=None if packet_backend is None else Path(packet_backend),
        network_backend=None if network_backend is None else Path(network_backend),
        artifact_limit_bytes=artifact_limit_bytes,
    )
    try:
        return AppServer(port, api)
    except BaseException:
        api.close()
        raise


def run_app(
    scenario_path: Path,
    *,
    port: int = 8765,
    open_browser: bool = True,
    packet_backend: Path | None = None,
    network_backend: Path | None = None,
) -> None:
    """Load a scenario, serve the app until Ctrl-C, then release the socket and the results."""
    from openleo.scenario import load_scenario

    scenario = load_scenario(scenario_path)
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
