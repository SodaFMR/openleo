import json
from io import BytesIO
from zipfile import ZipFile

import pytest
import test_app

scenario = test_app.scenario
running_app = test_app.running_app


def _study(server, settings=None, scenario_override=None, headers=None):
    configuration = scenario_override or json.loads(test_app.request(server)[2])["scenario"]
    return test_app.request(
        server,
        "POST",
        "/api/study",
        json.dumps({"scenario": configuration, "settings": settings or {"duration_s": 10}}),
        {
            "Content-Type": "application/json",
            "X-OpenLEO-Token": test_app.session(server)["token"],
            **(headers or {}),
        },
    )


def test_model_comparison_api_exports_a_complete_portable_archive(running_app):
    status, _, body = test_app.request(running_app, path="/api/capabilities")
    assert status == 200
    assert json.loads(body) == {"link_replay": False, "network_replay": False}
    original = test_app.request(running_app)[2]
    status, _, body = _study(running_app)
    result = json.loads(body)
    assert status == 200, result
    assert result["summary"]["kind"] == "openleo.experiment"
    assert result["report_url"].startswith("/api/study/")
    status, headers, report = test_app.request(running_app, path=result["report_url"])
    assert status == 200 and headers["Content-Type"].startswith("text/html")
    assert b"Snapshot routing" in report and b"<svg" in report
    status, headers, archive = test_app.request(running_app, path=result["archive_url"])
    assert status == 200 and headers["Content-Type"] == "application/zip"
    with ZipFile(BytesIO(archive)) as zipped:
        assert "inputs/orbit.csv" in zipped.namelist()
        assert "cases/free_space/explorer.html" in zipped.namelist()
        assert json.loads(zipped.read("experiment-study.json")) == result["summary"]
    assert test_app.request(running_app)[2] == original


@pytest.mark.parametrize(
    "settings",
    [
        {"backend": "/untrusted"},
        {"output_dir": "../../escape"},
        {"packet_mode": "network", "duration_s": 10},
        {"duration_s": 1000},
    ],
)
def test_rejected_comparison_keeps_previous_result(running_app, settings):
    status, _, body = _study(running_app)
    assert status == 200
    previous = json.loads(body)
    status, _, body = _study(running_app, settings)
    assert status == 400
    assert "/untrusted" not in json.loads(body)["error"]
    assert test_app.request(running_app, path=previous["report_url"])[0] == 200


def test_study_protects_origin_token_catalog_and_result_paths(running_app):
    assert _study(running_app, headers={"X-OpenLEO-Token": "wrong"})[0] == 403
    assert _study(running_app, headers={"Origin": "https://evil.example"})[0] == 403
    raw = json.loads(test_app.request(running_app)[2])["scenario"]
    changed = {**raw, "orbit": {**raw["orbit"], "path": "/etc/passwd"}}
    assert _study(running_app, scenario_override=changed)[0] == 400
    for suffix in ("../scenario.json", "%2e%2e/secret", "index.html?file=secret"):
        assert test_app.request(running_app, path=f"/api/study/{'0' * 64}/{suffix}")[0] == 404


def test_new_study_does_not_mix_files_with_an_old_study(running_app):
    first = json.loads(_study(running_app)[2])
    second = json.loads(_study(running_app, {"duration_s": 15})[2])
    assert first["report_url"] != second["report_url"]
    assert test_app.request(running_app, path=first["report_url"])[0] == 404
    assert test_app.request(running_app, path=second["report_url"])[0] == 200


def test_local_api_rate_limit_rejects_bursts_and_recovers(running_app, monkeypatch):
    monkeypatch.setattr("openleo.app.monotonic", lambda: 1_000_000.0, raising=False)
    statuses = [test_app.request(running_app, path="/api/capabilities")[0] for _ in range(61)]
    assert statuses[:60] == [200] * 60
    assert statuses[-1] == 429
    monkeypatch.setattr("openleo.app.monotonic", lambda: 1_000_002.0)
    assert test_app.request(running_app, path="/api/capabilities")[0] == 200
