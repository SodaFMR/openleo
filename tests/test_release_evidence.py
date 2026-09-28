import json
import os
from hashlib import sha256
from pathlib import Path

import pytest
import test_app
from test_benchmark import _config

scenario = test_app.scenario
REFERENCE = Path("examples/validation/vallado_06251.json")


def test_release_rejects_missing_backend_before_writing(tmp_path):
    from openleo.release_evidence import reproduce_release

    with pytest.raises(ValueError, match="backend"):
        reproduce_release("missing", REFERENCE, tmp_path / "output", network_backend=None)
    assert not (tmp_path / "output").exists()


def test_reference_failure_prevents_benchmark_and_publication(tmp_path):
    from openleo.release_evidence import reproduce_release

    fixture = json.loads(REFERENCE.read_text())
    fixture["states"][0]["position_km"][0] += 1
    path = tmp_path / "reference.json"
    path.write_text(json.dumps(fixture))
    with pytest.raises(ValueError, match="reference"):
        reproduce_release("missing", path, tmp_path / "output", network_backend="missing")
    assert not (tmp_path / "output").exists()


def test_release_cli_missing_reference_has_clean_error(tmp_path, capsys):
    from openleo.cli import main

    assert (
        main(
            [
                "reproduce",
                "missing",
                "--reference",
                "missing",
                "--network-backend",
                "missing",
                "--output",
                str(tmp_path / "output"),
            ]
        )
        == 2
    )
    assert "error:" in capsys.readouterr().err


def test_orbit_cli_reports_published_reference(capsys):
    from openleo.cli import main

    assert main(["verify-orbit", str(REFERENCE)]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["passed"] is True and len(output["epochs"]) == 5


def test_benchmark_cli_model_only_is_explicit(scenario, tmp_path, capsys):
    from openleo.cli import main

    config = _config(scenario, tmp_path, sampling_steps_s=[10], refinements=[1])
    assert main(["benchmark", str(config), "--output", str(tmp_path / "output")]) == 0
    assert "packet evidence: missing" in capsys.readouterr().out


def test_complete_release_evidence_uses_native_results_and_binds_sources(scenario, tmp_path):
    from openleo.release_evidence import reproduce_release

    backend = os.environ.get("OPENLEO_NS3_NETWORK")
    if not backend:
        pytest.skip("set OPENLEO_NS3_NETWORK for the complete native release workflow")
    config = _config(
        scenario, tmp_path, sampling_steps_s=[10], refinements=[1], traffic_loads_bps=[10000]
    )
    output = tmp_path / "release"
    summary = reproduce_release(config, REFERENCE, output, network_backend=backend)
    assert summary["kind"] == "openleo.release-evidence"
    assert summary["completed_experiments"] == 1
    assert summary["native_packet_replays"] == 9
    assert summary["orbit_reference_passed"] is True
    assert summary["orbit_reference_sha256"] == sha256(REFERENCE.read_bytes()).hexdigest()
    for filename, checksum in json.loads((output / "manifest.json").read_text())["files"].items():
        assert sha256((output / filename).read_bytes()).hexdigest() == checksum
    html = (output / "index.html").read_text()
    assert "benchmark/index.html" in html and "orbit-verification.json" in html
    assert str(tmp_path) not in (output / "release-evidence.json").read_text()
    with pytest.raises(ValueError, match="nonempty"):
        reproduce_release(config, REFERENCE, output, network_backend=backend)
