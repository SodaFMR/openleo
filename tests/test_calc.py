"""The standalone calculators keep v1's output files and formats byte for byte."""

import json
import math
import sys
from hashlib import sha256
from pathlib import Path

import pytest

from openleo.calc import gases, hydrometeors

METADATA = json.loads(Path("tests/fixtures/v1/metadata.json").read_text(encoding="utf-8"))
V1_HYDROMETEORS_JSON = Path("tests/fixtures/v1/calc/hydrometeors.json")
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
    expected = METADATA["calc_outputs"][command]
    if command == "hydrometeors":
        _assert_v1_hydrometeor_values(output, expected)
    # Windows' C runtime rounds exp, log10, pow and cos differently from glibc and macOS in
    # the last bit, and hydrometeors.json keeps full-precision floats; there the values check
    # above stands in for the two hashes that depend on those bits.
    if command != "hydrometeors" or sys.platform != "win32":
        assert _hashes(output) == expected


def _assert_v1_hydrometeor_values(output: Path, expected: dict[str, str]) -> None:
    """v1's values to the last few bits; every byte that does not hold a float is v1's."""
    v1_bytes = V1_HYDROMETEORS_JSON.read_bytes()
    assert sha256(v1_bytes).hexdigest() == expected["hydrometeors.json"]  # it is v1's file
    written = (output / "hydrometeors.json").read_bytes()
    _assert_close(json.loads(written), json.loads(v1_bytes), "")
    hashes = _hashes(output)
    for name in ("cloud.csv", "rain.csv", "index.html"):
        assert hashes[name] == expected[name], name
    # The manifest is v1's once v1's hydrometeors.json hash stands in for this one's.
    manifest = (output / "manifest.json").read_text(encoding="utf-8")
    restored = manifest.replace(hashes["hydrometeors.json"], expected["hydrometeors.json"])
    assert sha256(restored.encode("utf-8")).hexdigest() == expected["manifest.json"]


def _assert_close(actual, expected, pointer: str) -> None:
    assert type(actual) is type(expected), pointer
    if isinstance(expected, dict):
        assert list(actual) == list(expected), pointer
        for key, value in expected.items():
            _assert_close(actual[key], value, f"{pointer}/{key}")
    elif isinstance(expected, list):
        assert len(actual) == len(expected), pointer
        for index, (left, right) in enumerate(zip(actual, expected, strict=True)):
            _assert_close(left, right, f"{pointer}/{index}")
    elif isinstance(expected, float):
        assert math.isclose(actual, expected, rel_tol=1e-12, abs_tol=0.0), pointer
    else:
        assert actual == expected, pointer


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


def test_deeply_nested_gases_config_is_a_value_error(tmp_path, monkeypatch):
    # The JSON decoder raises RecursionError on deeply nested input. It is simulated: real deep
    # recursion can also overflow inside the coverage tracer, which then stops measuring.
    def too_deep(*args, **kwargs):
        raise RecursionError("maximum recursion depth exceeded")

    path = tmp_path / "deep.json"
    path.write_text("[[[]]]", encoding="utf-8")
    monkeypatch.setattr(gases.json, "loads", too_deep)
    with pytest.raises(ValueError, match=r"deep\.json: invalid JSON: nested too deeply$"):
        gases.load_gases_benchmark(path)
