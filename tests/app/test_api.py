"""The v2 app API: scenario, validation, asynchronous runs and sweeps, tables and exports."""

import copy
import json
import threading
from dataclasses import replace
from hashlib import sha256
from io import BytesIO
from zipfile import ZipFile

import pytest
from app_client import EIRP_SPEC, Client, start, stop

from openleo.results import decode_table


def _canonical(document) -> bytes:
    return json.dumps(
        document, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


@pytest.fixture
def gate(monkeypatch):
    """Hold every engine run until ``gate.set()``."""
    from openleo.app import api

    release = threading.Event()
    real = api.run_engine

    def held(*args, **kwargs):
        assert release.wait(30)
        return real(*args, **kwargs)

    monkeypatch.setattr(api, "run_engine", held)
    yield release
    release.set()


def test_capabilities(client):
    status, _, body = client.request("GET", "/api/capabilities")
    document = json.loads(body)
    assert status == 200
    assert document["api_version"] == 2
    assert document["openleo"]
    assert document["scenario_schema"] == "openleo.scenario/2"
    assert document["sweep_schema"] == "openleo.sweep/2"
    assert document["backends"] == {"link_replay": False, "network_replay": False}
    assert set(document["extras"]) == {"plot", "parquet"}
    assert document["limits"]["request_bytes"] == 1_000_000
    assert document["limits"]["artifact_bytes"] == 256 * 2**20
    assert document["limits"]["requests_per_second"] == 60
    assert document["phases"][0] == "catalog"


def test_capabilities_report_trusted_backends(scenario, tmp_path):
    from openleo.app import create_server

    server = create_server(
        scenario, packet_backend=tmp_path / "ns3-link", artifact_limit_bytes=2**20
    )
    thread = start(server)
    try:
        _, _, body = Client(server).request("GET", "/api/capabilities")
        document = json.loads(body)
        assert document["backends"] == {"link_replay": True, "network_replay": False}
        assert document["limits"]["artifact_bytes"] == 2**20
        assert str(tmp_path) not in body.decode()
    finally:
        stop(server, thread)


def test_scenario_endpoint_serves_the_normalised_document_and_schema(client, scenario):
    status, document = client.get_json("/api/scenario")
    assert status == 200
    assert document["source_sha256"] == sha256(scenario.source_path.read_bytes()).hexdigest()
    assert document["scenario"]["space_segment"]["catalogs"][0]["path"] == "orbit.csv"
    assert document["schema"]["type"] == "object"
    assert str(scenario.source_path.parent) not in json.dumps(document)


def test_validate_reports_the_normalised_scenario_or_pointer_errors(client):
    raw = client.scenario()
    status, document = client.post_json("/api/scenario/validate", {"scenario": raw})
    assert status == 200
    assert document["valid"] is True
    assert document["scenario"] == raw
    assert document["sha256"] == sha256(_canonical(raw)).hexdigest()
    raw["radio"]["eirp_dbw"] = "loud"
    status, document = client.post_json("/api/scenario/validate", {"scenario": raw})
    assert status == 200
    assert document["valid"] is False
    assert document["errors"][0]["pointer"] == "/radio/eirp_dbw"
    assert document["errors"][0]["message"].startswith("/radio/eirp_dbw")
    status, document = client.post_json("/api/scenario/validate", {"scenario": {"schema": 1}})
    assert document["errors"][0]["pointer"] == "/schema"
    status, document = client.post_json("/api/scenario/validate", {"scenario": []})
    assert document["errors"][0]["pointer"] == ""


def test_async_run_lifecycle(running_app, client, gate):
    raw = client.scenario()
    status, document = client.post_json("/api/runs", {"scenario": raw})
    assert status == 202
    identifier = document["id"]
    expected = sha256(_canonical({"kind": "run", "payload": {"scenario": raw}})).hexdigest()
    assert identifier == expected
    first = client.get_json(f"/api/runs/{identifier}")[1]
    assert first["id"] == identifier and first["state"] in ("queued", "running")
    assert set(first) == {"id", "state", "phase", "error"}
    other = copy.deepcopy(raw)
    other["radio"]["eirp_dbw"] = 4.0
    second = client.post_json("/api/runs", {"scenario": other})[1]["id"]
    assert client.get_json(f"/api/runs/{second}")[1]["state"] == "queued"
    status, document = client.get_json(f"/api/runs/{identifier}/results.json")
    assert status == 409 and "not finished" in document["error"]
    gate.set()
    done = client.wait("runs", identifier)
    assert done == {"id": identifier, "state": "done", "phase": "tables", "error": None}
    assert client.wait("runs", second)["state"] == "done"

    status, headers, body = client.get(f"/api/runs/{identifier}/results.json")
    assert status == 200
    assert headers["Content-Type"] == "application/json; charset=utf-8"
    metadata = json.loads(body)
    assert metadata["kind"] == "openleo.results"
    assert metadata["scenario"]["source_sha256"] == sha256(_canonical(raw)).hexdigest()
    assert "olc" in metadata["tables"]["samples"]["files"]
    status, headers, olc = client.get(f"/api/runs/{identifier}/tables/samples.olc")
    assert status == 200
    assert headers["Content-Type"] == "application/octet-stream"
    assert int(headers["Content-Length"]) == len(olc)
    table = decode_table(olc)
    assert table.name == "samples"
    assert table.rows == metadata["tables"]["samples"]["rows"] > 0
    other = json.loads(client.get(f"/api/runs/{second}/results.json")[2])
    assert other["scenario"]["radio"]["eirp_dbw"] == 4.0
    assert metadata["scenario"]["radio"]["eirp_dbw"] == 10.0
    assert client.get(f"/api/runs/{identifier}/tables/unknown.olc")[0] == 404
    assert client.get(f"/api/sweeps/{identifier}")[0] == 404


def test_csv_only_scenario_still_serves_olc(client):
    raw = client.scenario()
    raw["outputs"]["formats"] = ["csv"]
    identifier = client.post_json("/api/runs", {"scenario": raw})[1]["id"]
    assert client.wait("runs", identifier)["state"] == "done"
    status, _, olc = client.get(f"/api/runs/{identifier}/tables/passes.olc")
    assert status == 200
    assert decode_table(olc).name == "passes"


def test_resubmitting_a_finished_run_returns_the_cached_result(running_app, client, monkeypatch):
    raw = client.scenario()
    identifier = client.post_json("/api/runs", {"scenario": raw})[1]["id"]
    assert client.wait("runs", identifier)["state"] == "done"
    results = client.get(f"/api/runs/{identifier}/results.json")[2]

    def forbidden(*args, **kwargs):
        raise AssertionError("a cached run must not run again")

    monkeypatch.setattr("openleo.app.api.run_engine", forbidden)
    assert client.post_json("/api/runs", {"scenario": raw}) == (202, {"id": identifier})
    assert client.get_json(f"/api/runs/{identifier}")[1]["state"] == "done"
    assert client.get(f"/api/runs/{identifier}/results.json")[2] == results


def test_failure_preserves_cached_result(running_app, client, scenario):
    raw = client.scenario()
    first = client.post_json("/api/runs", {"scenario": raw})[1]["id"]
    assert client.wait("runs", first)["state"] == "done"
    results = client.get(f"/api/runs/{first}/results.json")[2]
    catalog = scenario.space_segment.catalogs[0].path
    catalog.write_bytes(b"invalid catalog")
    changed = copy.deepcopy(raw)
    changed["radio"]["eirp_dbw"] = 4.0
    second = client.post_json("/api/runs", {"scenario": changed})[1]["id"]
    failed = client.wait("runs", second)
    assert failed["state"] == "failed"
    assert failed["phase"] == "catalog"
    assert failed["error"]
    assert str(catalog) not in failed["error"]
    assert str(catalog.parent) not in failed["error"]
    assert str(running_app.api.root) not in failed["error"]
    assert client.get(f"/api/runs/{second}/results.json")[0] == 409
    assert client.post_json("/api/runs", {"scenario": raw}) == (202, {"id": first})
    assert client.get_json(f"/api/runs/{first}")[1]["state"] == "done"
    assert client.get(f"/api/runs/{first}/results.json")[2] == results


def test_unexpected_failure_is_logged_by_type_without_exposing_it(client, monkeypatch, capsys):
    def fail(*args, **kwargs):
        raise RuntimeError("/private/path secret-error-probe")

    monkeypatch.setattr("openleo.app.api.run_engine", fail)
    identifier = client.post_json("/api/runs", {"scenario": client.scenario()})[1]["id"]
    failed = client.wait("runs", identifier)
    assert failed["state"] == "failed"
    assert "secret-error-probe" not in failed["error"]
    logs = capsys.readouterr().err
    assert "RuntimeError" in logs
    assert "secret-error-probe" not in logs


def test_runs_do_not_change_the_scenario_directory(client, scenario):
    directory = scenario.source_path.parent
    before = {path.name: path.read_bytes() for path in directory.iterdir()}
    raw = client.scenario()
    raw["name"] = "edited-in-browser"
    identifier = client.post_json("/api/runs", {"scenario": raw})[1]["id"]
    assert client.wait("runs", identifier)["state"] == "done"
    assert {path.name: path.read_bytes() for path in directory.iterdir()} == before


@pytest.mark.parametrize("name", ["browser-edit", '雪 🌍 </script> " \\ &   '])
def test_run_hash_is_reproducible_from_the_served_scenario(client, name):
    raw = client.scenario()
    raw["name"] = name
    identifier = client.post_json("/api/runs", {"scenario": raw})[1]["id"]
    assert client.wait("runs", identifier)["state"] == "done"
    metadata = json.loads(client.get(f"/api/runs/{identifier}/results.json")[2])
    block = dict(metadata["scenario"])
    digest = block.pop("source_sha256")
    block.pop("normalised_sha256", None)
    assert block["name"] == name
    assert digest == sha256(_canonical(block)).hexdigest()


def test_catalog_pinning(running_app, client):
    raw = client.scenario()
    raw["space_segment"]["select_norad_ids"] = [25544]
    assert client.post_json("/api/runs", {"scenario": raw})[0] == 403
    raw = client.scenario()
    raw["space_segment"]["catalogs"].append(raw["space_segment"]["catalogs"][0])
    status, document = client.post_json("/api/runs", {"scenario": raw})
    assert status == 403
    assert "catalog" in document["error"]
    raw = client.scenario()
    del raw["space_segment"]
    assert client.post_json("/api/runs", {"scenario": raw})[0] == 400
    assert running_app.api.jobs.count() == 0


@pytest.mark.parametrize(
    "path, payload",
    [
        ("/api/runs", {"packet_backend": "/bin/sh"}),
        ("/api/runs", {"output_dir": "/tmp/escape"}),
        ("/api/runs", {"scenario_path": "/etc/passwd"}),
        ("/api/sweeps", {"spec": {**EIRP_SPEC, "base_scenario": "/etc/passwd"}}),
        ("/api/sweeps", {"spec": {**EIRP_SPEC, "backend": "/bin/sh"}}),
        ("/api/sweeps", {"spec": EIRP_SPEC, "output_dir": "../../escape"}),
        (
            "/api/sweeps",
            {
                "spec": {
                    **EIRP_SPEC,
                    "replay": {"link": {"loads_bps": [1e6], "backend": "/bin/sh"}},
                }
            },
        ),
        ("/api/sweeps", {"spec": {**EIRP_SPEC, "replay": {"executable": "/bin/sh"}}}),
    ],
)
def test_no_executable_or_path_fields_accepted(running_app, client, path, payload):
    if "spec" not in payload:
        payload = {"scenario": client.scenario(), **payload}
    status, document = client.post_json(path, payload)
    assert status == 400
    assert "/bin/sh" not in document["error"]
    assert "/etc/passwd" not in document["error"]
    assert "escape" not in document["error"]
    assert running_app.api.jobs.count() == 0


def test_export_zip_deterministic_metadata(client):
    identifier = client.post_json("/api/runs", {"scenario": client.scenario()})[1]["id"]
    assert client.wait("runs", identifier)["state"] == "done"
    status, headers, archive = client.get(f"/api/runs/{identifier}/export.zip")
    assert status == 200
    assert headers["Content-Type"] == "application/zip"
    assert client.get(f"/api/runs/{identifier}/export.zip")[2] == archive
    with ZipFile(BytesIO(archive)) as zipped:
        infos = zipped.infolist()
        names = [info.filename for info in infos]
        assert names == sorted(names)
        assert {"results.json", "manifest.json", "inputs/scenario.json"} <= set(names)
        assert "tables/samples.olc" in names and "tables/samples.csv" in names
        assert all(info.date_time == (2000, 1, 1, 0, 0, 0) for info in infos)
        assert all(info.external_attr == 0o100644 << 16 for info in infos)
        results = client.get(f"/api/runs/{identifier}/results.json")[2]
        assert zipped.read("results.json") == results


def test_export_is_identical_across_servers(scenario):
    from openleo.app import create_server

    archives = []
    for _ in range(2):
        server = create_server(scenario)
        thread = start(server)
        try:
            client = Client(server)
            raw = client.scenario()
            identifier = client.post_json("/api/runs", {"scenario": raw})[1]["id"]
            assert client.wait("runs", identifier)["state"] == "done"
            archives.append(client.get(f"/api/runs/{identifier}/export.zip")[2])
        finally:
            stop(server, thread)
    with ZipFile(BytesIO(archives[0])) as first, ZipFile(BytesIO(archives[1])) as second:
        assert first.namelist() == second.namelist()
        assert [i.date_time for i in first.infolist()] == [i.date_time for i in second.infolist()]


def test_artifact_limit_fails_the_job(scenario):
    from openleo.app import create_server

    server = create_server(scenario, artifact_limit_bytes=1000)
    thread = start(server)
    try:
        client = Client(server)
        identifier = client.post_json("/api/runs", {"scenario": client.scenario()})[1]["id"]
        failed = client.wait("runs", identifier)
        assert failed["state"] == "failed"
        assert "artifact limit" in failed["error"]
        assert list(server.api.root.iterdir()) == []
    finally:
        stop(server, thread)


def test_sweep_lifecycle_tables_and_runs(client):
    status, document = client.post_json("/api/sweeps", {"spec": EIRP_SPEC})
    assert status == 202
    identifier = document["id"]
    done = client.wait("sweeps", identifier)
    assert done["state"] == "done", done
    assert done["phase"].startswith("run 2/2")
    status, sweep = client.get_json(f"/api/sweeps/{identifier}/results.json")
    assert status == 200
    assert sweep["kind"] == "openleo.sweep"
    assert [run["id"] for run in sweep["runs"]] == [
        "oat%3Anominal",
        "oat%3A%2Fradio%2Feirp_dbw%3D4.0",
    ]
    assert sweep["spec"]["base_scenario"] == "scenario.json"
    comparisons = decode_table(client.get(f"/api/sweeps/{identifier}/tables/comparisons.olc")[2])
    assert comparisons.rows == sweep["comparisons"]["rows"]
    assert [c.name for c in comparisons.columns] == sweep["comparisons"]["columns"]
    nominal = json.loads(client.get(f"/api/sweeps/{identifier}/runs/0/results.json")[2])
    assert nominal["kind"] == "openleo.results"
    assert nominal["scenario"]["radio"]["eirp_dbw"] == 10.0
    table = decode_table(client.get(f"/api/sweeps/{identifier}/runs/1/tables/samples.olc")[2])
    assert table.name == "samples"
    assert client.get(f"/api/sweeps/{identifier}/runs/2/results.json")[0] == 404
    assert client.get(f"/api/sweeps/{identifier}/runs/0/tables/nope.olc")[0] == 404
    assert client.get(f"/api/runs/{identifier}")[0] == 404
    status, _, archive = client.get(f"/api/sweeps/{identifier}/export.zip")
    assert status == 200
    with ZipFile(BytesIO(archive)) as zipped:
        names = zipped.namelist()
        assert names == sorted(names)
        assert "sweep.json" in names and "comparisons.csv" in names
        assert "runs/oat%3Anominal/results.json" in names


def test_sweep_over_an_edited_scenario(client):
    raw = client.scenario()
    raw["radio"]["eirp_dbw"] = 4.0
    parameter = {"path": "/radio/eirp_dbw", "values": [4.0, 1.0], "nominal": 4.0}
    spec = {**EIRP_SPEC, "parameters": [parameter]}
    status, document = client.post_json("/api/sweeps", {"spec": spec, "scenario": raw})
    assert status == 202
    assert client.wait("sweeps", document["id"])["state"] == "done"
    sweep = client.get_json(f"/api/sweeps/{document['id']}/results.json")[1]
    assert sweep["base"]["source_sha256"] == sha256(_canonical(raw)).hexdigest()


@pytest.mark.parametrize(
    "spec, pointer",
    [
        ({**EIRP_SPEC, "mode": "nope"}, "/mode"),
        (
            {**EIRP_SPEC, "parameters": [{"path": "/radio/eirp_dbw", "values": [9], "nominal": 1}]},
            "/parameters/0",
        ),
        ([], "/"),
    ],
)
def test_invalid_sweep_is_rejected(running_app, client, spec, pointer):
    status, document = client.post_json("/api/sweeps", {"spec": spec})
    assert status == 400
    assert document["error"].startswith(pointer)
    assert running_app.api.jobs.count() == 0


def test_sweep_ids_cover_the_normalised_request(client):
    first = client.post_json("/api/sweeps", {"spec": EIRP_SPEC})[1]["id"]
    second = client.post_json("/api/sweeps", {"spec": EIRP_SPEC, "scenario": client.scenario()})
    assert second == (202, {"id": first})
    client.wait("sweeps", first)


def test_unfinished_and_failed_jobs_serve_no_artifacts(client, gate):
    identifier = client.post_json("/api/runs", {"scenario": client.scenario()})[1]["id"]
    for suffix in ("results.json", "export.zip", "tables/samples.olc"):
        assert client.get(f"/api/runs/{identifier}/{suffix}")[0] == 409
    gate.set()
    client.wait("runs", identifier)


def test_full_queue_is_refused(running_app, client, gate, monkeypatch):
    monkeypatch.setattr(running_app.api.jobs, "max_pending", 2)
    raw = client.scenario()
    statuses = []
    for eirp in (1.0, 2.0, 3.0, 4.0):
        raw["radio"]["eirp_dbw"] = eirp
        statuses.append(client.post("/api/runs", {"scenario": raw})[0])
    assert statuses == [202, 202, 503, 503]


@pytest.mark.parametrize("suffix", ["", "/results.json", "/export.zip", "/tables/samples.olc"])
@pytest.mark.parametrize("kind", ["runs", "sweeps"])
def test_unknown_job_ids_are_404(client, kind, suffix):
    status, document = client.get_json(f"/api/{kind}/{'0' * 64}{suffix}")
    assert status == 404
    assert document["error"].startswith("Unknown")


# --- fix round 1: the catalog pin holds for sweeps, and disk use is bounded -------------------

PASSWD_SHA = "4320d92464d9902007796f5bb632fbb5e22888eb9fb01d985b0fc1e531947a87"
CATALOG_PATCHES = [
    [
        {"op": "replace", "path": "/space_segment/catalogs/0/path", "value": "/etc/passwd"},
        {"op": "replace", "path": "/space_segment/catalogs/0/sha256", "value": PASSWD_SHA},
    ],
    [{"op": "replace", "path": "/space_segment/synthetic_rf", "value": True}],
    [{"op": "add", "path": "/space_segment/select_norad_ids", "value": [25544]}],
    [{"op": "replace", "path": "/space_segment", "value": {}}],
    [{"op": "remove", "path": "/space_segment/catalogs/0"}],
]


def _no_leak(text: str) -> None:
    assert "/etc/passwd" not in text
    assert PASSWD_SHA not in text
    assert "passwd" not in text


@pytest.mark.parametrize("patch", CATALOG_PATCHES)
def test_sweep_variant_patches_cannot_touch_the_catalog(running_app, client, patch):
    spec = {
        "schema": "openleo.sweep/2",
        "name": "probe",
        "mode": "variants",
        "variants": [{"id": "a", "patch": []}, {"id": "b", "patch": patch}],
    }
    status, _, body = client.post("/api/sweeps", {"spec": spec})
    assert status == 403
    assert "catalog" in json.loads(body)["error"]
    _no_leak(body.decode())
    assert running_app.api.jobs.count() == 0


@pytest.mark.parametrize(
    "parameter",
    [
        {"path": "/space_segment/synthetic_rf", "values": [False, True], "nominal": False},
        {"path": "/space_segment/catalogs/0/path", "values": ["orbit.csv", "/etc/passwd"]},
        {"path": "/space_segment", "values": [{}]},
    ],
)
def test_sweep_parameters_cannot_touch_the_catalog(running_app, client, parameter):
    mode = "oat" if "nominal" in parameter else "grid"
    spec = {"schema": "openleo.sweep/2", "name": "probe", "mode": mode, "parameters": [parameter]}
    status, _, body = client.post("/api/sweeps", {"spec": spec})
    assert status == 403
    _no_leak(body.decode())
    assert running_app.api.jobs.count() == 0


def test_generated_runs_are_checked_against_the_pin(running_app, client, monkeypatch):
    from openleo.app import api

    real = api.expand_sweep

    def tampered(spec, document):
        runs = real(spec, document)
        document = copy.deepcopy(runs[-1].document)
        document["space_segment"]["synthetic_rf"] = True
        return (*runs[:-1], replace(runs[-1], document=document))

    monkeypatch.setattr(api, "expand_sweep", tampered)
    status, document = client.post_json("/api/sweeps", {"spec": EIRP_SPEC})
    assert status == 403
    assert "catalog" in document["error"]
    assert running_app.api.jobs.count() == 0


def test_worker_refuses_a_sweep_that_escapes_the_pin(running_app, client, monkeypatch):
    """Defence in depth: a hostile spec reaching the queue directly never reads a file."""
    import openleo.engine.run as engine_run

    def forbidden(*args, **kwargs):
        raise AssertionError("no catalog may be read")

    monkeypatch.setattr(engine_run, "load_catalogs", forbidden)
    spec = {
        "schema": "openleo.sweep/2",
        "name": "probe",
        "mode": "variants",
        "variants": [{"id": "b", "patch": CATALOG_PATCHES[0], "label": "b", "baseline": False}],
        "max_runs": 512,
    }
    identifier = running_app.api.jobs.submit("sweep", {"scenario": client.scenario(), "spec": spec})
    failed = client.wait("sweeps", identifier)
    assert failed["state"] == "failed"
    assert "catalog" in failed["error"]
    _no_leak(failed["error"])


def test_catalog_errors_do_not_reveal_server_file_hashes(running_app, client, scenario):
    catalog = scenario.space_segment.catalogs[0].path
    catalog.write_bytes(b"replaced catalog bytes")
    identifier = client.post_json("/api/runs", {"scenario": client.scenario()})[1]["id"]
    failed = client.wait("runs", identifier)
    assert failed["state"] == "failed"
    assert "SHA-256" in failed["error"]
    assert sha256(b"replaced catalog bytes").hexdigest() not in failed["error"]
    assert str(catalog) not in failed["error"]


def _job_bytes(server, identifier) -> int:
    _, path = server.api.jobs.result(identifier)
    return sum(p.stat().st_size for p in path.parent.rglob("*") if p.is_file())


def test_export_counts_against_the_artifact_limit(running_app, client, monkeypatch):
    identifier = client.post_json("/api/runs", {"scenario": client.scenario()})[1]["id"]
    assert client.wait("runs", identifier)["state"] == "done"
    size = _job_bytes(running_app, identifier)
    monkeypatch.setattr(running_app.api, "artifact_limit_bytes", size + 100)
    status, document = client.get_json(f"/api/runs/{identifier}/export.zip")
    assert status == 507
    assert "artifact limit" in document["error"]
    assert _job_bytes(running_app, identifier) == size
    assert client.get(f"/api/runs/{identifier}/results.json")[0] == 200


def _run(client, eirp):
    raw = client.scenario()
    raw["radio"]["eirp_dbw"] = eirp
    identifier = client.post_json("/api/runs", {"scenario": raw})[1]["id"]
    assert client.wait("runs", identifier)["state"] == "done"
    return identifier, raw


def test_session_limit_evicts_the_least_recently_used_results(scenario):
    from openleo.app import create_server

    server = create_server(scenario)
    thread = start(server)
    try:
        client = Client(server)
        first, first_raw = _run(client, 1.0)
        size = _job_bytes(server, first)
        server.api.session_limit_bytes = int(size * 2.5)
        second, _ = _run(client, 2.0)
        assert client.get(f"/api/runs/{first}/results.json")[0] == 200  # first is now newer
        third, _ = _run(client, 3.0)
        for suffix in ("", "/results.json", "/tables/samples.olc", "/export.zip"):
            status, document = client.get_json(f"/api/runs/{second}{suffix}")
            assert status == 404
            assert "evicted" in document["error"] and "resubmit" in document["error"]
        assert client.get(f"/api/runs/{first}/results.json")[0] == 200
        assert client.get(f"/api/runs/{third}/results.json")[0] == 200
        assert server.api.usage_bytes() <= server.api.session_limit_bytes
        assert len(list(server.api.root.iterdir())) == 2
        assert client.post_json("/api/runs", {"scenario": first_raw})[1]["id"] == first
        assert _run(client, 2.0)[0] == second
        assert client.wait("runs", second)["state"] == "done"
        assert server.api.usage_bytes() <= server.api.session_limit_bytes
    finally:
        stop(server, thread)


def test_session_limit_must_hold_one_artifact(scenario):
    from openleo.app import create_server

    with pytest.raises(ValueError, match="session_limit_bytes"):
        create_server(scenario, artifact_limit_bytes=1000, session_limit_bytes=999)
    server = create_server(scenario, artifact_limit_bytes=1000)
    try:
        assert server.api.session_limit_bytes == 4000
        assert (
            json.loads(server.api.route("/api/capabilities").handler(None).body)["limits"][
                "session_bytes"
            ]
            == 4000
        )
    finally:
        server.server_close()
