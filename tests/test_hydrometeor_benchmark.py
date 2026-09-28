import json
from hashlib import sha256
from pathlib import Path

import pytest

from openleo.cli import main

REFERENCE = Path("examples/atmosphere/hydrometeors_reference.json")


def test_reference_batch_writes_calculated_values_and_hashed_outputs(tmp_path):
    from openleo.hydrometeor_benchmark import run_hydrometeors

    output = tmp_path / "results"
    result = run_hydrometeors(REFERENCE, output)
    assert result["rain_cases"][0]["specific_attenuation_db_per_km"] == pytest.approx(
        1.58130839366869, rel=1e-12
    )
    assert result["cloud_cases"][0]["attenuation_db"] == pytest.approx(
        0.09905224128740467, rel=1e-12
    )
    assert (
        result["provenance"]["configuration_sha256"] == sha256(REFERENCE.read_bytes()).hexdigest()
    )
    manifest = json.loads((output / "manifest.json").read_text())
    assert set(manifest["files"]) == {"rain.csv", "cloud.csv", "hydrometeors.json", "index.html"}
    for name, digest in manifest["files"].items():
        assert sha256((output / name).read_bytes()).hexdigest() == digest
    assert json.loads((output / "hydrometeors.json").read_text()) == result


@pytest.mark.parametrize(
    "change",
    [
        lambda d: d.update(schema_version=1),
        lambda d: d.update(unrecognized=True),
        lambda d: d.update(rain_cases=[], cloud_cases=[]),
        lambda d: d.update(rain_cases=d["rain_cases"] * 30),
        lambda d: d["rain_cases"][0].update(rain_rate_mm_h=-1),
        lambda d: d["rain_cases"][0].update(expected=42),
        lambda d: d["cloud_cases"][0].update(liquid_water_kg_m2=float("nan")),
        lambda d: d["cloud_cases"][0].update(id=d["rain_cases"][0]["id"]),
    ],
)
def test_invalid_inputs_do_not_create_output(tmp_path, change):
    from openleo.hydrometeor_benchmark import run_hydrometeors

    data = json.loads(REFERENCE.read_text())
    change(data)
    config = tmp_path / "input.json"
    config.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        run_hydrometeors(config, tmp_path / "output")
    assert not (tmp_path / "output").exists()


@pytest.mark.parametrize(
    "raw",
    [
        '{"schema_version":"1","schema_version":"1"}',
        "[" * 2000 + "]" * 2000,
        "{" * 1_000_001,
    ],
)
def test_malformed_and_oversized_json_fails_cleanly(tmp_path, raw):
    from openleo.hydrometeor_benchmark import run_hydrometeors

    source = tmp_path / "input.json"
    source.write_text(raw)
    with pytest.raises(ValueError):
        run_hydrometeors(source, tmp_path / "output")
    assert not (tmp_path / "output").exists()


def test_existing_results_are_preserved(tmp_path):
    from openleo.hydrometeor_benchmark import run_hydrometeors

    output = tmp_path / "results"
    output.mkdir()
    (output / "important.txt").write_text("keep")
    with pytest.raises(ValueError, match="nonempty"):
        run_hydrometeors(REFERENCE, output)
    assert (output / "important.txt").read_text() == "keep"


def test_report_escapes_labels_and_separates_the_two_physical_quantities(tmp_path):
    from openleo.hydrometeor_benchmark import run_hydrometeors

    data = json.loads(REFERENCE.read_text())
    data["name"] = '<script>alert("bad")</script>'
    data["rain_cases"] = []
    config = tmp_path / "input.json"
    config.write_text(json.dumps(data))
    run_hydrometeors(config, tmp_path / "report")
    html = (tmp_path / "report/index.html").read_text()
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "Specific rain attenuation (dB/km)" in html
    assert "Cloud slant attenuation (dB)" in html
    assert "No rain cases" in html
    assert "script-src 'none'" in html


def test_hydrometeors_cli_runs_the_real_reference_cases(tmp_path, capsys):
    assert main(["hydrometeors", str(REFERENCE), "--output", str(tmp_path / "results")]) == 0
    assert "report:" in capsys.readouterr().out
    assert (tmp_path / "results/hydrometeors.json").exists()


def test_hydrometeors_cli_reports_missing_input_without_traceback(tmp_path, capsys):
    assert main(["hydrometeors", str(tmp_path / "missing"), "--output", str(tmp_path / "out")]) == 2
    message = capsys.readouterr().err
    assert "error:" in message and "Traceback" not in message
    assert not (tmp_path / "out").exists()
