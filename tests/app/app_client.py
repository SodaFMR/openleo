"""Helpers for the v2 app server tests: the pinned scenario, a sweep spec and a client."""

import json
import re
import time
from http.client import HTTPConnection
from pathlib import Path
from threading import Thread

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "examples" / "v2" / "iss_cartagena.json"
CATALOG = ROOT / "examples" / "data" / "iss_2026-08-30.csv"
EIRP_SPEC = {
    "schema": "openleo.sweep/2",
    "name": "eirp",
    "mode": "oat",
    "parameters": [{"path": "/radio/eirp_dbw", "values": [10.0, 4.0], "nominal": 10.0}],
}


def scenario_raw(**changes) -> dict:
    """The ISS example with its catalog next to it (``orbit.csv``) and a short window."""
    raw = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    raw["space_segment"]["catalogs"][0]["path"] = "orbit.csv"
    raw["time"]["stop_utc"] = "2026-08-30T06:20:00Z"
    raw.update(changes)
    return raw


class Client:
    """A minimal HTTP client for one running app; ``token`` is read from the served page."""

    def __init__(self, server):
        self.server = server
        self.port = server.server_port
        self._token = None

    def request(self, method="GET", path="/", body=None, headers=None):
        connection = HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            if isinstance(body, (dict, list)):
                body = json.dumps(body)
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    @property
    def token(self):
        if self._token is None:
            status, _, html = self.request("GET", "/")
            assert status == 200
            match = re.search(rb'<meta name="openleo-token" content="([^"]+)">', html)
            assert match is not None
            self._token = match[1].decode("ascii")
        return self._token

    def get(self, path, headers=None, *, token=True):
        base = {"X-OpenLEO-Token": self.token} if token else {}
        return self.request("GET", path, None, {**base, **(headers or {})})

    def get_json(self, path):
        status, _, body = self.get(path)
        return status, json.loads(body)

    def post(self, path, body, headers=None):
        defaults = {"Content-Type": "application/json", "X-OpenLEO-Token": self.token}
        return self.request("POST", path, body, {**defaults, **(headers or {})})

    def post_json(self, path, body, headers=None):
        status, _, response = self.post(path, body, headers)
        return status, json.loads(response)

    def scenario(self):
        status, document = self.get_json("/api/scenario")
        assert status == 200
        return document["scenario"]

    def wait(self, kind, identifier, timeout=60.0):
        """Poll a job until it is done or failed; return its last status."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status, document = self.get_json(f"/api/{kind}/{identifier}")
            assert status == 200, document
            if document["state"] in ("done", "failed"):
                return document
            time.sleep(0.02)
        raise AssertionError(f"job {identifier} did not finish in {timeout} s")


def start(server):
    thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    return thread


def stop(server, thread):
    server.shutdown()
    thread.join(timeout=5)
    server.server_close()
    assert not thread.is_alive()
