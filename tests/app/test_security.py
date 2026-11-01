"""The v1 app security checks, ported to the v2 API (tests/test_app.py, test_experiment_app.py).

Every protection of the v1 server is kept: loopback bind, exact Host and Origin, a session token,
60 matching requests per second, JSON-only bodies of at most 1 000 000 bytes with strict
framing, no browser-supplied paths or executables, and errors that never echo server paths.
"""

import json
import socket
import tempfile
from http.client import HTTPConnection
from pathlib import Path

import pytest
from app_client import EIRP_SPEC, Client, start, stop

RUN_ID = "0" * 64


def test_loopback_bind_and_security_headers(running_app, client):
    assert running_app.server_address[0] == "127.0.0.1"
    assert client.token
    status, headers, body = client.get("/api/scenario")
    assert status == 200
    assert headers["Content-Type"] == "application/json; charset=utf-8"
    assert headers["Cache-Control"] == "no-store"
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["X-Frame-Options"] == "DENY"
    assert "Python" not in headers.get("Server", "")
    assert json.loads(body)["scenario"]["schema"] == "openleo.scenario/2"
    status, headers, page = client.request("GET", "/")
    assert status == 200
    assert headers["Content-Type"] == "text/html; charset=utf-8"
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
    assert headers["Cache-Control"] == "no-store"
    assert b"the API is live at /api/capabilities" in page


@pytest.mark.parametrize(
    "path",
    [
        "/etc/passwd",
        "/../scenario.json",
        "/api/scenario?file=x",
        "/api/capabilities/",
        f"/api/runs/{RUN_ID}/../../scenario.json",
        f"/api/runs/{RUN_ID}/%2e%2e/secret",
        f"/api/runs/{RUN_ID}/results.json?file=secret",
        f"/api/runs/{RUN_ID}/tables/../results.json",
        f"/api/runs/{RUN_ID}/tables/Samples.olc",
        f"/api/runs/{RUN_ID}/tables/samples.csv",
        f"/api/runs/{RUN_ID}/inputs/scenario.json",
        f"/api/runs/{RUN_ID}/runs/0/results.json",
        f"/api/sweeps/{RUN_ID}/runs/00/results.json",
        "/api/runs/" + "0" * 63,
        "/_web/dist/index.html",
    ],
)
def test_unknown_urls_do_not_expose_files(client, path):
    status, headers, body = client.get(path)
    assert status == 404
    assert headers["Content-Type"] == "application/json; charset=utf-8"
    assert isinstance(json.loads(body)["error"], str)


@pytest.mark.parametrize("host", ["evil.example", "127.0.0.1", "localhost:1", "127.0.0.1:1"])
@pytest.mark.parametrize("method", ["GET", "POST"])
def test_untrusted_host_is_rejected_before_parsing(client, host, method):
    status, _, body = client.request(
        method,
        "/api/runs" if method == "POST" else "/",
        body="invalid json" if method == "POST" else None,
        headers={"Host": host},
    )
    assert status == 403
    assert "error" in json.loads(body)


@pytest.mark.parametrize("token", [None, "wrong", "\xe9"])
@pytest.mark.parametrize(
    "method, path",
    [
        ("POST", "/api/runs"),
        ("POST", "/api/sweeps"),
        ("POST", "/api/scenario/validate"),
        ("GET", "/api/scenario"),
        ("GET", f"/api/runs/{RUN_ID}"),
        ("GET", f"/api/runs/{RUN_ID}/results.json"),
        ("GET", f"/api/runs/{RUN_ID}/tables/samples.olc"),
        ("GET", f"/api/runs/{RUN_ID}/export.zip"),
        ("GET", f"/api/sweeps/{RUN_ID}"),
        ("GET", f"/api/sweeps/{RUN_ID}/runs/0/results.json"),
    ],
)
def test_missing_or_invalid_session_token_is_rejected(client, token, method, path):
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers = {**headers, "X-OpenLEO-Token": token}
    status, _, body = client.request(method, path, "{}" if method == "POST" else None, headers)
    assert status == 403
    assert "token" in json.loads(body)["error"]


def test_capabilities_and_page_need_no_token(client):
    assert client.request("GET", "/api/capabilities")[0] == 200
    assert client.request("GET", "/")[0] == 200


@pytest.mark.parametrize("origin", ["null", "https://evil.example", "http://localhost:1"])
def test_untrusted_origin_is_rejected(client, origin):
    assert client.post("/api/runs", "{}", {"Origin": origin})[0] == 403
    assert client.get("/api/scenario", {"Origin": origin})[0] == 403


def test_matching_origin_is_accepted_and_local_alias_mismatch_is_rejected(client):
    payload = {"scenario": client.scenario()}
    origin = f"http://localhost:{client.port}"
    path = "/api/scenario/validate"
    assert client.post(path, payload, {"Origin": origin})[0] == 403
    assert (
        client.post(path, payload, {"Origin": origin, "Host": f"localhost:{client.port}"})[0] == 200
    )


@pytest.mark.parametrize("content_type", ["text/plain", "application/x-www-form-urlencoded", ""])
def test_non_json_content_type_is_rejected(client, content_type):
    assert client.post("/api/runs", "{}", {"Content-Type": content_type})[0] == 415


@pytest.mark.parametrize(
    "body",
    [
        "{",
        "[]",
        "null",
        "{}",
        '{"scenario":{},"unexpected":1}',
        '{"scenario":{},"scenario":{}}',
        '{"scenario":{"name":"a","name":"b"}}',
        '{"scenario":{"name":NaN}}',
        '{"scenario":{"name":Infinity}}',
        '{"scenario":{"name":1e999}}',
        b"\xff",
        "[" * 2000,
    ],
)
@pytest.mark.parametrize("path", ["/api/runs", "/api/sweeps", "/api/scenario/validate"])
def test_malformed_body_is_rejected_without_starting_a_job(running_app, client, body, path):
    status, _, response = client.post(path, body)
    assert status == 400
    assert isinstance(json.loads(response)["error"], str)
    assert running_app.api.jobs.count() == 0


@pytest.mark.parametrize(
    "catalog_change",
    [
        {"path": "/etc/passwd"},
        {"path": "https://evil.example/catalog.csv"},
        {"source_url": "https://evil.example/catalog.csv"},
        {"sha256": "0" * 64},
    ],
)
@pytest.mark.parametrize("path", ["/api/runs", "/api/scenario/validate", "/api/sweeps"])
def test_catalog_path_and_provenance_are_pinned(running_app, client, catalog_change, path):
    raw = client.scenario()
    raw["space_segment"]["catalogs"][0].update(catalog_change)
    payload = {"scenario": raw}
    if path == "/api/sweeps":
        payload["spec"] = EIRP_SPEC
    status, _, body = client.post(path, payload)
    assert status == 403
    assert "/etc/passwd" not in body.decode()
    assert "evil.example" not in body.decode()
    assert running_app.api.jobs.count() == 0


def test_invalid_scenario_is_rejected_with_its_pointer_without_leaking_paths(
    running_app, client, scenario
):
    raw = client.scenario()
    raw["time"]["step_s"] = 0
    status, document = client.post_json("/api/runs", {"scenario": raw})
    assert status == 400
    assert document["error"].startswith("/time/step_s")
    assert str(scenario.source_path.parent) not in document["error"]
    assert running_app.api.jobs.count() == 0


def test_v1_scenario_error_does_not_leak_the_server_path(client, scenario):
    status, document = client.post_json("/api/runs", {"scenario": {"orbit": {}}})
    assert status == 400
    assert "upgrade" in document["error"]
    assert str(scenario.source_path.parent) not in document["error"]


@pytest.mark.parametrize("port", [-1, 65536, True, 1.0, "8765"])
def test_invalid_port_is_rejected_before_any_work(port):
    from openleo.app import create_server

    with pytest.raises(ValueError, match="port"):
        create_server(None, port)


@pytest.mark.parametrize("limit", [0, -1, True, 1.5])
def test_invalid_artifact_limit_is_rejected(scenario, limit):
    from openleo.app import create_server

    with pytest.raises(ValueError, match="artifact_limit_bytes"):
        create_server(scenario, artifact_limit_bytes=limit)


@pytest.mark.parametrize(
    "headers, status",
    [
        ({"Content-Length": "1000001"}, 413),
        ({"Content-Length": "-1"}, 400),
        ({"Content-Length": "invalid"}, 400),
        ({"Transfer-Encoding": "chunked"}, 400),
    ],
)
def test_unsafe_body_framing_is_rejected_without_waiting_for_body(client, headers, status):
    assert client.post("/api/runs", "", headers)[0] == status


@pytest.mark.parametrize(
    "duplicate, expected_status",
    [
        ("Host", 403),
        ("Origin", 403),
        ("X-OpenLEO-Token", 403),
        ("Content-Length", 400),
        ("Content-Type", 415),
    ],
)
def test_duplicate_security_headers_are_rejected(client, duplicate, expected_status):
    headers = {
        "Host": f"127.0.0.1:{client.port}",
        "Origin": f"http://127.0.0.1:{client.port}",
        "Content-Type": "application/json",
        "X-OpenLEO-Token": client.token,
        "Content-Length": "2",
    }
    connection = HTTPConnection("127.0.0.1", client.port, timeout=5)
    try:
        connection.putrequest("POST", "/api/scenario/validate", skip_host=True)
        for key, value in headers.items():
            connection.putheader(key, value)
        connection.putheader(duplicate, headers[duplicate])
        connection.endheaders(b"{}")
        assert connection.getresponse().status == expected_status
    finally:
        connection.close()


def test_missing_content_length_is_rejected(client):
    connection = HTTPConnection("127.0.0.1", client.port, timeout=5)
    try:
        connection.putrequest("POST", "/api/runs")
        connection.putheader("Content-Type", "application/json")
        connection.putheader("X-OpenLEO-Token", client.token)
        connection.endheaders()
        assert connection.getresponse().status == 411
    finally:
        connection.close()


def test_slow_request_body_times_out_without_starting_a_job(running_app, client, monkeypatch):
    token = client.token
    monkeypatch.setattr(running_app.RequestHandlerClass, "timeout", 0.05)
    connection = HTTPConnection("127.0.0.1", client.port, timeout=5)
    try:
        connection.putrequest("POST", "/api/runs")
        connection.putheader("Content-Type", "application/json")
        connection.putheader("X-OpenLEO-Token", token)
        connection.putheader("Content-Length", "100")
        connection.endheaders(b"{")
        assert connection.getresponse().status == 408
    finally:
        connection.close()
    assert running_app.api.jobs.count() == 0


@pytest.mark.parametrize(
    "method, path",
    [
        ("GET", "/api/runs"),
        ("PUT", "/api/runs"),
        ("POST", "/api/scenario"),
        ("DELETE", f"/api/runs/{RUN_ID}"),
        ("POST", f"/api/runs/{RUN_ID}/results.json"),
        ("POST", "/"),
        ("PATCH", "/api/capabilities"),
    ],
)
def test_wrong_method_is_405_with_allow(client, method, path):
    status, headers, body = client.request(
        method, path, "{}", {"Content-Type": "application/json", "X-OpenLEO-Token": client.token}
    )
    assert status == 405
    assert headers["Allow"] in ("GET", "POST")
    assert isinstance(json.loads(body)["error"], str)


def test_head_and_unsupported_methods_get_json_errors(client):
    status, headers, body = client.request("HEAD", "/")
    assert status == 405 and body == b""
    status, headers, body = client.request("BREW", "/")
    assert status == 501
    assert headers["Content-Type"] == "application/json; charset=utf-8"
    assert isinstance(json.loads(body)["error"], str)


def _raw_exchange(port, data):
    with socket.create_connection(("127.0.0.1", port), timeout=5) as raw:
        raw.sendall(data)
        chunks = []
        while chunk := raw.recv(65536):
            chunks.append(chunk)
    return b"".join(chunks)


def test_malformed_requests_get_json_errors(client):
    # An unsupported version is answered HTTP/0.9-style by the standard library: body only.
    response = _raw_exchange(client.port, b"GET / HTTP/9.9\r\nHost: x\r\n\r\n")
    assert json.loads(response) == {"error": "HTTP Version Not Supported."}
    headers = b"".join(b"X-%d: 1\r\n" % i for i in range(101))
    response = _raw_exchange(client.port, b"GET / HTTP/1.1\r\n" + headers + b"\r\n")
    head, body = response.split(b"\r\n\r\n", 1)
    assert head.split(b"\r\n", 1)[0] == b"HTTP/1.0 431 Request Header Fields Too Large"
    assert b"X-Content-Type-Options: nosniff" in head
    assert json.loads(body) == {"error": "Request Header Fields Too Large."}


def test_request_logs_do_not_echo_url_tokens(client, capsys):
    client.request("GET", "/unknown?token=secret-log-probe")
    assert "secret-log-probe" not in capsys.readouterr().err


def test_local_api_rate_limit_rejects_bursts_and_recovers(client, monkeypatch):
    monkeypatch.setattr("openleo.app.server.monotonic", lambda: 1_000_000.0)
    statuses = [client.request("GET", "/api/capabilities")[0] for _ in range(61)]
    assert statuses[:60] == [200] * 60
    assert statuses[-1] == 429
    monkeypatch.setattr("openleo.app.server.monotonic", lambda: 1_000_002.0)
    assert client.request("GET", "/api/capabilities")[0] == 200


def test_rate_limit_counts_only_matching_requests(client, monkeypatch):
    monkeypatch.setattr("openleo.app.server.monotonic", lambda: 1_000_000.0)
    for _ in range(70):
        assert client.request("GET", "/", headers={"Host": "evil.example"})[0] == 403
    assert client.request("GET", "/api/capabilities")[0] == 200


@pytest.mark.parametrize("open_browser", [True, False])
def test_run_app_prints_url_and_closes_socket_on_keyboard_interrupt(
    scenario_path, capsys, monkeypatch, open_browser
):
    from openleo.app import run_app

    addresses, roots, opened = [], [], []

    def interrupt(server):
        addresses.append(server.server_address)
        roots.append(server.api.root)
        raise KeyboardInterrupt

    monkeypatch.setattr("socketserver.BaseServer.serve_forever", interrupt)
    monkeypatch.setattr("openleo.app.server.webbrowser.open", opened.append)
    run_app(scenario_path, port=0, open_browser=open_browser)
    url = f"http://127.0.0.1:{addresses[0][1]}/"
    assert url in capsys.readouterr().out
    assert opened == ([url] if open_browser else [])
    with socket.socket() as probe:
        assert probe.connect_ex(addresses[0]) != 0
    assert not roots[0].exists()


def test_server_close_removes_the_results_directory(scenario):
    from openleo.app import create_server

    server = create_server(scenario)
    thread = start(server)
    client = Client(server)
    status, document = client.post_json("/api/runs", {"scenario": client.scenario()})
    assert status == 202
    assert client.wait("runs", document["id"])["state"] == "done"
    root = server.api.root
    assert any(root.iterdir())
    stop(server, thread)
    assert not root.exists()


def test_cli_app_serves_a_v2_scenario_with_trusted_backends(
    scenario_path, tmp_path, capsys, monkeypatch
):
    from openleo.cli import main

    seen = []

    def interrupt(server):
        seen.append((server.server_address[0], server.api.packet_backend))
        raise KeyboardInterrupt

    monkeypatch.setattr("socketserver.BaseServer.serve_forever", interrupt)
    backend = tmp_path / "ns3-link"
    arguments = ["app", str(scenario_path), "--port", "0", "--no-browser"]
    assert main([*arguments, "--packet-backend", str(backend)]) == 0
    assert seen == [("127.0.0.1", backend)]
    assert "workbench: http://127.0.0.1:" in capsys.readouterr().out
    legacy = tmp_path / "legacy.json"
    legacy.write_text('{"orbit": {}}', encoding="utf-8")
    assert main(["app", str(legacy), "--no-browser"]) == 2
    assert "scenario upgrade" in capsys.readouterr().err


def test_unexpected_request_failure_is_500_without_details(
    running_app, client, capsys, monkeypatch
):
    def fail(*args):
        raise RuntimeError("/private/path secret-error-probe")

    monkeypatch.setattr(running_app.api, "_submitted", fail)
    status, document = client.post_json("/api/runs", {"scenario": {}})
    assert status == 500
    assert "secret-error-probe" not in json.dumps(document)
    logs = capsys.readouterr().err
    assert "RuntimeError" in logs and "secret-error-probe" not in logs


def test_incomplete_body_is_rejected(client):
    with socket.create_connection(("127.0.0.1", client.port), timeout=5) as raw:
        raw.sendall(
            b"POST /api/runs HTTP/1.0\r\n"
            + f"Host: 127.0.0.1:{client.port}\r\n".encode()
            + f"X-OpenLEO-Token: {client.token}\r\n".encode()
            + b"Content-Type: application/json\r\nContent-Length: 10\r\n\r\n{}"
        )
        raw.shutdown(socket.SHUT_WR)
        response = raw.recv(65536)
    assert response.startswith(b"HTTP/1.0 400 ")


def test_failed_bind_releases_the_results_directory(scenario, monkeypatch):
    from openleo.app import create_server

    roots = []
    real = tempfile.mkdtemp

    def recording(*args, **kwargs):
        roots.append(Path(real(*args, **kwargs)))
        return str(roots[-1])

    monkeypatch.setattr("openleo.app.api.tempfile.mkdtemp", recording)
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        with pytest.raises(OSError):
            create_server(scenario, occupied.getsockname()[1])
    assert roots and not roots[0].exists()


def test_page_without_head_is_refused_at_startup(scenario, tmp_path, monkeypatch):
    from openleo.app import create_server

    page = tmp_path / "index.html"
    page.write_text("<html><body>no head</body></html>", encoding="utf-8")
    monkeypatch.setattr("openleo.app.api.INDEX_PAGE", page)
    with pytest.raises(ValueError, match="head"):
        create_server(scenario)


def test_closing_mid_job_cancels_it_and_removes_every_result(scenario):
    import time

    from openleo.app import create_server

    server = create_server(scenario)
    thread = start(server)
    client = Client(server)
    values = [float(v) for v in range(1, 40)]
    spec = {
        "schema": "openleo.sweep/2",
        "name": "long",
        "mode": "oat",
        "parameters": [{"path": "/radio/eirp_dbw", "values": values, "nominal": 10.0}],
    }
    identifier = client.post_json("/api/sweeps", {"spec": spec})[1]["id"]
    deadline = time.monotonic() + 30
    while client.get_json(f"/api/sweeps/{identifier}")[1]["phase"] is None:
        assert time.monotonic() < deadline
        time.sleep(0.01)
    root, worker = server.api.root, server.api.jobs.worker
    started = time.monotonic()
    stop(server, thread)
    assert time.monotonic() - started < 10
    assert not worker.is_alive()
    assert not root.exists()
    assert server.api.jobs.status(identifier)["state"] == "failed"
    time.sleep(0.5)
    assert not root.exists()


def test_client_hang_ups_are_logged_by_type_only(running_app, capsys):
    for error in (BrokenPipeError("/secret/pipe"), ConnectionResetError("/secret/reset")):
        try:
            raise error
        except OSError:
            running_app.handle_error(None, ("127.0.0.1", 1))
    err = capsys.readouterr().err
    assert "BrokenPipeError" in err and "ConnectionResetError" in err
    assert "Traceback" not in err and "secret" not in err


def test_run_app_passes_the_artifact_limit(scenario_path, monkeypatch):
    from openleo.app import run_app

    limits = []

    def interrupt(server):
        limits.append(server.api.artifact_limit_bytes)
        raise KeyboardInterrupt

    monkeypatch.setattr("socketserver.BaseServer.serve_forever", interrupt)
    run_app(scenario_path, port=0, open_browser=False, artifact_limit_bytes=2**20)
    assert limits == [2**20]
