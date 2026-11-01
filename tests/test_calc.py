"""The standalone calculators keep v1's output files and formats byte for byte."""

import json
from hashlib import sha256
from pathlib import Path

import pytest

from openleo.calc import gases, hydrometeors

METADATA = json.loads(Path("tests/fixtures/v1/metadata.json").read_text(encoding="utf-8"))
V1_VERSION = METADATA["openleo_link"]
CONFIGS = {
    "gases": Path("examples/atmosphere/p676_13_validation.json"),
    "hydrometeors": Path("examples/atmosphere/hydrometeors_reference.json"),
}


def _hashes(directory: Path) -> dict[str, str]:
    return {
        path.relative_to(directory).as_posix(): sha256(path.read_bytes()).hexdigest()
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


def _calculate(command: str, output: Path) -> None:
    if command == "gases":
        gases.calculate_gases(CONFIGS[command], output)
    else:
        hydrometeors.run_hydrometeors(CONFIGS[command], output)


@pytest.mark.parametrize("command", ["gases", "hydrometeors"])
def test_calc_outputs_unchanged(tmp_path, monkeypatch, command):
    """Every file v1 wrote, with the same bytes, once the software version reads as v1's.

    The outputs record the installed ``openleo-link`` version; v1's hashes were recorded under
    1.0.0, so the version lookup is pinned to it. Every other byte must match.
    """
    for module in (gases, hydrometeors):
        monkeypatch.setattr(module, "version", lambda name: V1_VERSION)
    output = tmp_path / command
    _calculate(command, output)
    assert _hashes(output) == METADATA["calc_outputs"][command]


@pytest.mark.parametrize("command", ["gases", "hydrometeors"])
def test_calc_outputs_differ_from_v1_only_by_the_version(tmp_path, command):
    from importlib.metadata import version

    output = tmp_path / command
    _calculate(command, output)
    assert set(_hashes(output)) == set(METADATA["calc_outputs"][command])
    for path in output.rglob("*.json"):
        text = path.read_text(encoding="utf-8")
        if "openleo-link" in text:
            assert version("openleo-link") in text


def test_calculate_gases_returns_the_written_paths(tmp_path):
    result, csv_path, summary_path = gases.calculate_gases(CONFIGS["gases"], tmp_path / "out")
    assert result.benchmark.name == "itu-p676-13-specific-attenuation-validation"
    assert csv_path.name == "gaseous-specific-attenuation.csv"
    assert summary_path.name == "gaseous-specific-attenuation-summary.json"
    assert len(result.cases) == 5
