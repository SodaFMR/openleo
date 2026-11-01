"""Release reproduction: refusals, the evidence contract and the orchestration."""

import json
import os
from hashlib import sha256
from pathlib import Path

import pytest

from openleo import reproduce, sweep
from openleo.results import verify_manifest
from openleo.scenario import load_scenario, scenario_document

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples" / "v2"
REFERENCE = ROOT / "examples" / "validation" / "vallado_06251.json"
GLOBAL = EXAMPLES / "iridium_global.json"
CONFIG = EXAMPLES / "reproduce" / "release_benchmark.json"
NETWORK = {"loads_bps": [10000, 300000], "packet_size_bytes": 1200, "queue_packets": 8}


def test_reproduce_refuses_without_backend_or_failing_orbit(tmp_path):
    output = tmp_path / "output"
    with pytest.raises(ValueError, match="backend"):
        reproduce.run_reproduce(Path("missing"), REFERENCE, None, output)
    assert not output.exists()

    fixture = json.loads(REFERENCE.read_text())
    fixture["states"][0]["position_km"][0] += 1
    failing = tmp_path / "reference.json"
    failing.write_text(json.dumps(fixture))
    with pytest.raises(ValueError, match="reference"):
        reproduce.run_reproduce(Path("missing"), failing, Path("missing"), output)
    assert not output.exists()
    assert list(tmp_path.glob("output*")) == []


def test_reproduce_refuses_an_existing_output_and_a_missing_backend_file(tmp_path):
    output = tmp_path / "output"
    output.mkdir()
    with pytest.raises(FileExistsError):
        reproduce.run_reproduce(CONFIG, REFERENCE, tmp_path / "ns3", output)
    output.rmdir()
    with pytest.raises(ValueError, match=r"network backend .* is not a file"):
        reproduce.run_reproduce(CONFIG, REFERENCE, tmp_path / "ns3", output)
    assert not output.exists()


def _small_base(directory: Path) -> Path:
    scenario = load_scenario(GLOBAL)
    catalog = scenario.space_segment.catalogs[0].path
    document = scenario_document(scenario)
    document["space_segment"]["select_norad_ids"] = [
        int(row.split(",")[11]) for row in catalog.read_text().splitlines()[1:4]
    ]
    document["time"]["convergence"]["mode"] = "off"
    path = directory / "base" / "scenario.json"
    path.parent.mkdir()
    document["space_segment"]["catalogs"][0]["path"] = os.path.relpath(catalog, path.parent)
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def _config(directory: Path, replay) -> Path:
    spec = {
        "schema": "openleo.sweep/2",
        "name": "small release sweep",
        "base_scenario": os.path.relpath(_small_base(directory), directory),
        "mode": "variants",
        "variants": [
            {"id": "a", "patch": []},
            {"id": "b", "patch": [{"op": "replace", "path": "/radio/eirp_dbw", "value": 18.0}]},
        ],
        "window": {"start_offset_s": 0, "duration_s": 120},
        "replay": replay,
    }
    path = directory / "config.json"
    path.write_text(json.dumps(spec), encoding="utf-8")
    return path


def test_reproduce_requires_a_network_replay(tmp_path):
    backend = tmp_path / "ns3"
    backend.touch()
    link = {"station": "Madrid", "norad_id": 5, "loads_bps": [100]}
    for index, replay in enumerate((None, {"link": link})):
        directory = tmp_path / f"case{index}"
        directory.mkdir()
        config = _config(directory, replay)
        with pytest.raises(ValueError, match=r"^/replay/network: .*requires native network"):
            reproduce.run_reproduce(config, REFERENCE, backend, tmp_path / "out")
    directory = tmp_path / "both"
    directory.mkdir()
    config = _config(directory, {"network": NETWORK, "link": link})
    with pytest.raises(ValueError, match=r"^/replay/link: .*no packet backend"):
        reproduce.run_reproduce(config, REFERENCE, backend, tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_reference_changed_during_verification(tmp_path, monkeypatch):
    backend = tmp_path / "ns3"
    backend.touch()
    verified = reproduce.verify_orbit_reference(REFERENCE)
    monkeypatch.setattr(
        reproduce, "verify_orbit_reference", lambda path: {**verified, "fixture_sha256": "0" * 64}
    )
    with pytest.raises(ValueError, match="changed during verification"):
        reproduce.run_reproduce(CONFIG, REFERENCE, backend, tmp_path / "out")
    oversized = tmp_path / "big.json"
    oversized.write_bytes(b" " * (reproduce.MAX_REFERENCE_BYTES + 1))
    monkeypatch.setattr(reproduce, "verify_orbit_reference", lambda path: verified)
    with pytest.raises(ValueError, match="changed during verification"):
        reproduce.run_reproduce(CONFIG, oversized, backend, tmp_path / "out")
    assert not (tmp_path / "out").exists()


def _packet(unresolved=0, send_errors=0):
    return {
        "kind": "network",
        "summary": {
            "aggregate": {"unresolved_packets": unresolved, "send_error_packets": send_errors}
        },
    }


def _evidence(*replays):
    return {"evidence": "actual_ns3", "replays": list(replays)}


def test_native_evidence_contract():
    ok = {"runs": [{"id": "a", "replay": _evidence(_packet(), _packet())}]}
    assert reproduce.require_native_evidence(ok) == 2
    cases = [
        (None, "requires actual native packet results; run a has none"),
        ({"evidence": "missing", "replays": [_packet()]}, "actual native packet results"),
        (_evidence(), "actual native packet results"),
        ({"evidence": "actual_ns3"}, "actual native packet results"),
        (_evidence(_packet(unresolved=1)), "unresolved packets or source send errors"),
        (_evidence(_packet(send_errors=2)), "unresolved packets or source send errors"),
        (_evidence({"summary": {}}), "lacks summary.aggregate"),
        (_evidence({"summary": {"aggregate": {"unresolved_packets": 0}}}), "lacks summary"),
        (_evidence("x"), "lacks summary.aggregate"),
    ]
    for replay, message in cases:
        with pytest.raises(ValueError, match=message):
            reproduce.require_native_evidence({"runs": [{"id": "a", "replay": replay}]})
    with pytest.raises(ValueError, match="run b"):
        reproduce.require_native_evidence(
            {"runs": [{"id": "a", "replay": _evidence(_packet())}, {"id": "b", "replay": None}]}
        )


def test_example_release_benchmark_is_the_release_matrix():
    spec = sweep.load_sweep(CONFIG)
    matrix = sweep.load_sweep(EXAMPLES / "sweeps" / "release_matrix.json")
    assert spec == matrix | {"base_scenario_path": spec["base_scenario_path"]}
    assert spec["mode"] == "grid"
    assert spec["replay"]["network"] is not None
    assert spec["replay"]["link"] is None
    base = load_scenario(Path(spec["base_scenario_path"]))
    assert len(sweep.expand_sweep(spec, scenario_document(base))) == 8


def test_reproduce_orchestration_with_a_stand_in_for_the_native_replays(tmp_path, monkeypatch):
    """Everything but the compiled backend: the seam is replaced, as Task 15 will replace it."""
    seen = []

    def seam(run_dir, replay_spec, packet_backend, network_backend):
        seen.append((run_dir.name, packet_backend, network_backend))
        return _evidence(_packet())

    monkeypatch.setattr(sweep, "_run_replays", seam)
    config = _config(tmp_path, {"network": NETWORK})
    backend = tmp_path / "ns3"
    backend.touch()
    output = tmp_path / "evidence"
    summary = reproduce.run_reproduce(config, REFERENCE, backend, output)
    assert [name for name, _, _ in seen] == ["variant%3Aa", "variant%3Ab"]
    assert all(p is None and b == backend for _, p, b in seen)
    verify_manifest(output)
    verify_manifest(output / "sweep")
    assert sorted(p.name for p in output.iterdir()) == [
        "inputs",
        "manifest.json",
        "orbit-verification.json",
        "release-evidence.json",
        "sweep",
    ]
    stored = json.loads((output / "release-evidence.json").read_text(encoding="utf-8"))
    assert stored == json.loads(json.dumps(summary))
    assert stored["kind"] == "openleo.release-evidence"
    assert stored["completed_runs"] == 2
    assert stored["native_packet_replays"] == 2
    assert stored["orbit_reference_passed"] is True
    assert stored["orbit_reference_sha256"] == sha256(REFERENCE.read_bytes()).hexdigest()
    assert (
        stored["numerical_metrics_sha256"]
        == sha256((output / "sweep" / "comparisons.csv").read_bytes()).hexdigest()
    )
    assert stored["comparison_label"] == "deterministic comparison; not an uncertainty interval"
    assert (output / "inputs" / "orbit-reference.json").read_bytes() == REFERENCE.read_bytes()
    assert json.loads((output / "orbit-verification.json").read_text())["passed"] is True
    assert (output / "inputs" / "configuration.json").read_bytes() == config.read_bytes()
    assert stored["numerical_metrics_sha256"]


def test_reproduce_publishes_nothing_without_native_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(sweep, "_run_replays", lambda *args: {"evidence": "missing", "replays": []})
    config = _config(tmp_path, {"network": NETWORK})
    backend = tmp_path / "ns3"
    backend.touch()
    with pytest.raises(ValueError, match="actual native packet results"):
        reproduce.run_reproduce(config, REFERENCE, backend, tmp_path / "evidence")
    assert list(tmp_path.glob("evidence*")) == []


def test_reproduce_with_an_untrusted_backend_fails_closed(tmp_path):
    config = _config(tmp_path, {"network": NETWORK})
    backend = tmp_path / "ns3"
    backend.touch()
    with pytest.raises(ValueError, match="handshake"):
        reproduce.run_reproduce(config, REFERENCE, backend, tmp_path / "evidence")
    assert list(tmp_path.glob("evidence*")) == []


def test_reproduce_with_the_native_backend(tmp_path):
    backend = os.environ.get("OPENLEO_NS3_NETWORK")
    if not backend:
        pytest.skip("set OPENLEO_NS3_NETWORK to the actual pinned ns-3 network backend")
    config = _config(tmp_path, {"network": NETWORK})
    summary = reproduce.run_reproduce(config, REFERENCE, Path(backend), tmp_path / "evidence")
    assert summary["completed_runs"] == 2
    assert summary["native_packet_replays"] == 2 * 2 * 3
    sweep_document = json.loads((tmp_path / "evidence" / "sweep" / "sweep.json").read_text())
    assert sweep_document["replay"] == {"evidence": "actual", "missing": []}
    for entry in sweep_document["runs"]:
        for replay in entry["replay"]["replays"]:
            directory = tmp_path / "evidence" / "sweep" / replay["directory"]
            assert (directory / "network-summary.json").is_file()
            assert replay["summary"]["aggregate"]["unresolved_packets"] == 0
