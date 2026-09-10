import json
import re
import socket
from hashlib import sha256
from http.client import HTTPConnection
from pathlib import Path
from threading import Thread

import pytest


@pytest.fixture
def scenario(tmp_path):
    from openleo.constellation import load_constellation

    original = json.loads(Path("examples/scenarios/iss_cartagena.json").read_text())
    (tmp_path / "orbit.csv").write_bytes(Path("examples/data/iss_2026-08-30.csv").read_bytes())
    payload = {
        "name": "local-test",
        "orbit": {**original["orbit"], "path": "orbit.csv"},
        "stations": [
            original["ground_station"],
            {**original["ground_station"], "name": "Neighbor", "longitude_deg": -0.95},
        ],
        "time_window": {
            **original["time_window"],
            "start_utc": "2026-08-30T06:17:00Z",
            "stop_utc": "2026-08-30T06:17:20Z",
        },
        "radio_link": original["radio_link"],
        "adaptation": {
            "symbol_rate_baud": 500_000,
            "rolloff": 0.2,
            "implementation_margin_db": 1,
            "hysteresis_db": 0.5,
        },
        "network": {
            "source_station": "Cartagena",
            "target_station": "Neighbor",
            "isl_max_range_m": 5_000_000,
            "isl_capacity_bps": 10_000_000,
            "fixed_capacity_bps": 1_000_000,
        },
    }
    path = tmp_path / "scenario.json"
    path.write_text(json.dumps(payload))
    return load_constellation(path)


@pytest.fixture
def running_app(scenario):
    from openleo.app import create_server

    server = create_server(scenario)
    thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
        assert not thread.is_alive()


def request(server, method="GET", path="/api/experiment", body=None, headers=None):
    connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def session(server):
    status, _, html = request(server, path="/")
    assert status == 200
    match = re.search(rb'<script[^>]*id="session-data"[^>]*>(.*?)</script>', html, re.DOTALL)
    assert match is not None
    return json.loads(match[1])


def post(server, body, headers=None):
    defaults = {
        "Content-Type": "application/json",
        "X-OpenLEO-Token": session(server)["token"],
    }
    return request(server, "POST", "/api/simulate", body, {**defaults, **(headers or {})})


def test_loopback_app_serves_live_document(running_app):
    assert running_app.server_address[0] == "127.0.0.1"
    assert session(running_app)["live"] is True
    assert session(running_app)["token"]
    status, headers, body = request(running_app)
    document = json.loads(body)
    assert status == 200
    assert headers["Content-Type"] == "application/json; charset=utf-8"
    assert headers["Cache-Control"] == "no-store"
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert document["kind"] == "openleo.constellation"
    assert document["satellites"][0]["norad_id"] == 25544
    assert len(document["timestamps_utc"]) == 3
    assert "network" in document


def test_propagation_input_error_is_actionable_and_does_not_leak_values():
    from openleo.app import _input_error

    assert _input_error(ValueError("propagation secret-file.json invalid")) == (
        "Invalid propagation. Check its values and limits."
    )


def test_simulate_updates_document_in_memory_without_changing_files(running_app, scenario):
    original_source = scenario.source_path.read_bytes()
    original = json.loads(request(running_app)[2])
    edited = {
        **original["scenario"],
        "name": "edited-in-browser",
        "radio_link": {**original["scenario"]["radio_link"], "eirp_dbw": -100},
    }
    status, _, body = post(running_app, json.dumps({"scenario": edited}))
    updated = json.loads(body)
    assert status == 200, updated
    assert updated["scenario"]["name"] == "edited-in-browser"
    assert updated["links"] != original["links"]
    assert all(link["rate_bps"] == 0 for frame in updated["links"] for link in frame)
    assert json.loads(request(running_app)[2]) == updated
    assert scenario.source_path.read_bytes() == original_source
    assert {path.name for path in scenario.source_path.parent.iterdir()} == {
        "scenario.json",
        "orbit.csv",
    }


def test_initial_experiment_identifies_original_file_hash(running_app, scenario):
    document = json.loads(request(running_app)[2])
    assert (
        document["provenance"]["scenario_sha256"]
        == sha256(scenario.source_path.read_bytes()).hexdigest()
    )
    assert document["provenance"]["scenario_hash_encoding"] == "original UTF-8 file bytes"
    assert "scenario_canonical_json" not in document["provenance"]


@pytest.mark.parametrize("name", ["browser-edit", '雪 🌍 </script> " \\ & \u2028\u2029'])
def test_browser_hash_is_reproducible_from_exported_normalized_scenario(running_app, name):
    configuration = json.loads(request(running_app)[2])["scenario"]
    edited = {
        **configuration,
        "name": name,
        "radio_link": {**configuration["radio_link"], "eirp_dbw": 12},
    }
    status, _, response = post(running_app, json.dumps({"scenario": edited}))
    assert status == 200
    document = json.loads(response)
    canonical = json.dumps(
        document["scenario"],
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    assert document["provenance"]["scenario_sha256"] == sha256(canonical).hexdigest()
    assert document["provenance"]["scenario_hash_encoding"] == (
        "canonical JSON sorted compact UTF-8"
    )
    exported = json.loads(json.dumps(document, ensure_ascii=True))
    preserved = exported["provenance"]["scenario_canonical_json"]
    assert isinstance(preserved, str)
    assert (
        sha256(preserved.encode("utf-8")).hexdigest() == exported["provenance"]["scenario_sha256"]
    )
    assert json.loads(preserved) == exported["scenario"]
    assert json.loads(preserved)["name"] == name


@pytest.mark.parametrize("path", ["/etc/passwd", "/../scenario.json", "/api/experiment?file=x"])
def test_unknown_urls_do_not_expose_files(running_app, path):
    status, _, body = request(running_app, path=path)
    assert status == 404
    assert isinstance(json.loads(body)["error"], str)


@pytest.mark.parametrize("host", ["evil.example", "127.0.0.1", "localhost:1", "127.0.0.1:1"])
@pytest.mark.parametrize("method", ["GET", "POST"])
def test_untrusted_host_is_rejected_before_parsing(running_app, host, method):
    status, _, body = request(
        running_app,
        method,
        "/api/simulate" if method == "POST" else "/",
        body="invalid json" if method == "POST" else None,
        headers={"Host": host},
    )
    assert status == 403
    assert "error" in json.loads(body)


@pytest.mark.parametrize("token", [None, "wrong", "\xe9"])
def test_missing_or_invalid_session_token_is_rejected(running_app, token):
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers = {**headers, "X-OpenLEO-Token": token}
    status, _, _ = request(running_app, "POST", "/api/simulate", "{}", headers)
    assert status == 403


@pytest.mark.parametrize("origin", ["null", "https://evil.example", "http://localhost:1"])
def test_untrusted_origin_is_rejected(running_app, origin):
    assert post(running_app, "{}", {"Origin": origin})[0] == 403


def test_matching_origin_is_accepted_and_local_alias_mismatch_is_rejected(running_app):
    configuration = json.loads(request(running_app)[2])["scenario"]
    payload = json.dumps({"scenario": configuration})
    origin = f"http://localhost:{running_app.server_port}"
    assert post(running_app, payload, {"Origin": origin})[0] == 403
    assert (
        post(
            running_app, payload, {"Origin": origin, "Host": f"localhost:{running_app.server_port}"}
        )[0]
        == 200
    )


@pytest.mark.parametrize("content_type", ["text/plain", "application/x-www-form-urlencoded", ""])
def test_non_json_content_type_is_rejected(running_app, content_type):
    assert post(running_app, "{}", {"Content-Type": content_type})[0] == 415


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
def test_malformed_body_preserves_previous_document(running_app, body):
    previous = request(running_app)[2]
    status, _, response = post(running_app, body)
    assert status == 400
    assert isinstance(json.loads(response)["error"], str)
    assert request(running_app)[2] == previous


@pytest.mark.parametrize(
    "orbit_change",
    [
        {"path": "/etc/passwd"},
        {"path": "https://evil.example/catalog.csv"},
        {"source_url": "https://evil.example/catalog.csv"},
        {"sha256": "0" * 64},
    ],
)
def test_catalog_path_and_provenance_are_pinned(running_app, orbit_change):
    previous = request(running_app)[2]
    configuration = json.loads(previous)["scenario"]
    edited = {**configuration, "orbit": {**configuration["orbit"], **orbit_change}}
    status, _, body = post(running_app, json.dumps({"scenario": edited}))
    assert status == 400
    assert "/etc/passwd" not in body.decode()
    assert "evil.example" not in body.decode()
    assert request(running_app)[2] == previous


def test_invalid_scenario_preserves_previous_document_without_leaking_paths(running_app, scenario):
    previous = request(running_app)[2]
    configuration = json.loads(previous)["scenario"]
    edited = {**configuration, "time_window": {**configuration["time_window"], "step_s": 0}}
    status, _, body = post(running_app, json.dumps({"scenario": edited}))
    assert status == 400
    assert "time_window" in json.loads(body)["error"]
    assert str(scenario.source_path).encode() not in body
    assert request(running_app)[2] == previous


@pytest.mark.parametrize("port", [-1, 65536, True, 1.0, "8765"])
def test_invalid_port_is_rejected_before_initial_computation(port):
    from openleo.app import create_server

    with pytest.raises(ValueError, match="port"):
        create_server(None, port)


def test_failed_catalog_read_preserves_previous_document(running_app, scenario):
    previous = request(running_app)[2]
    configuration = json.loads(previous)["scenario"]
    scenario.orbit.path.write_bytes(b"invalid catalog")
    status, _, body = post(running_app, json.dumps({"scenario": configuration}))
    assert status == 400
    assert str(scenario.orbit.path).encode() not in body
    assert request(running_app)[2] == previous


def test_unexpected_failure_preserves_document_without_exposing_exception(
    running_app, monkeypatch, capsys
):
    previous = request(running_app)[2]
    configuration = json.loads(previous)["scenario"]

    def fail(*args):
        raise RuntimeError("/private/path secret-error-probe")

    monkeypatch.setattr("openleo.app._snapshot", fail)
    status, _, body = post(running_app, json.dumps({"scenario": configuration}))
    assert status == 500
    assert "secret-error-probe" not in body.decode()
    assert request(running_app)[2] == previous
    logs = capsys.readouterr().err
    assert "RuntimeError" in logs
    assert "secret-error-probe" not in logs


@pytest.mark.parametrize(
    "headers, status",
    [
        ({"Content-Length": "1000001"}, 413),
        ({"Content-Length": "-1"}, 400),
        ({"Content-Length": "invalid"}, 400),
        ({"Transfer-Encoding": "chunked"}, 400),
    ],
)
def test_unsafe_body_framing_is_rejected_without_waiting_for_body(running_app, headers, status):
    assert post(running_app, "", headers)[0] == status


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
def test_duplicate_security_headers_are_rejected(running_app, duplicate, expected_status):
    headers = {
        "Host": f"127.0.0.1:{running_app.server_port}",
        "Origin": f"http://127.0.0.1:{running_app.server_port}",
        "Content-Type": "application/json",
        "X-OpenLEO-Token": session(running_app)["token"],
        "Content-Length": "2",
    }
    connection = HTTPConnection("127.0.0.1", running_app.server_port, timeout=5)
    try:
        connection.putrequest("POST", "/api/simulate", skip_host=True)
        for key, value in headers.items():
            connection.putheader(key, value)
        connection.putheader(duplicate, headers[duplicate])
        connection.endheaders(b"{}")
        assert connection.getresponse().status == expected_status
    finally:
        connection.close()


def test_missing_content_length_is_rejected(running_app):
    token = session(running_app)["token"]
    connection = HTTPConnection("127.0.0.1", running_app.server_port, timeout=5)
    try:
        connection.putrequest("POST", "/api/simulate")
        connection.putheader("Content-Type", "application/json")
        connection.putheader("X-OpenLEO-Token", token)
        connection.endheaders()
        assert connection.getresponse().status == 411
    finally:
        connection.close()


def test_slow_request_body_times_out_without_changing_document(running_app, monkeypatch):
    previous = request(running_app)[2]
    token = session(running_app)["token"]
    monkeypatch.setattr(running_app.RequestHandlerClass, "timeout", 0.05)
    connection = HTTPConnection("127.0.0.1", running_app.server_port, timeout=5)
    try:
        connection.putrequest("POST", "/api/simulate")
        connection.putheader("Content-Type", "application/json")
        connection.putheader("X-OpenLEO-Token", token)
        connection.putheader("Content-Length", "100")
        connection.endheaders(b"{")
        assert connection.getresponse().status == 408
    finally:
        connection.close()
    assert request(running_app)[2] == previous


def test_request_logs_do_not_echo_url_tokens(running_app, capsys):
    request(running_app, path="/unknown?token=secret-log-probe")
    assert "secret-log-probe" not in capsys.readouterr().err


@pytest.mark.parametrize("open_browser", [True, False])
def test_run_app_prints_url_and_closes_socket_on_keyboard_interrupt(
    scenario, capsys, monkeypatch, open_browser
):
    from http.server import HTTPServer

    from openleo.app import run_app

    addresses = []
    opened = []

    def interrupt(server):
        addresses.append(server.server_address)
        raise KeyboardInterrupt

    monkeypatch.setattr(HTTPServer, "serve_forever", interrupt)
    monkeypatch.setattr("openleo.app.webbrowser.open", opened.append)
    run_app(scenario, port=0, open_browser=open_browser)
    url = f"http://127.0.0.1:{addresses[0][1]}/"
    assert url in capsys.readouterr().out
    assert opened == ([url] if open_browser else [])
    with socket.socket() as client:
        assert client.connect_ex(addresses[0]) != 0
