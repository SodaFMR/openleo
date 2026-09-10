import builtins
import json
from dataclasses import replace
from pathlib import Path

import pytest

from openleo.cli import main
from openleo.constellation import load_constellation
from openleo.propagation_study import (
    render_propagation_study,
    run_propagation_study,
    write_propagation_study,
)
from openleo.workbench import load_experiment


def _short_scenario(tmp_path):
    path = Path("examples/constellations/iridium_global_reference.json")
    raw = json.loads(path.read_text())
    raw["orbit"]["path"] = str((path.parent / raw["orbit"]["path"]).resolve())
    raw["time_window"]["stop_utc"] = "2026-09-10T12:01:00Z"
    target = tmp_path / "scenario.json"
    target.write_text(json.dumps(raw))
    return load_constellation(target)


def test_propagation_study_isolates_model_and_refinement(tmp_path):
    scenario = _short_scenario(tmp_path)
    result = run_propagation_study(scenario)
    cases = result["cases"]
    assert tuple(cases) == ("free_space", "reference", "refined")
    for case in cases.values():
        assert case["satellites"] == cases["free_space"]["satellites"]
        assert case["stations"] == cases["free_space"]["stations"]
        assert case["scenario"]["radio_link"] == cases["free_space"]["scenario"]["radio_link"]
    assert cases["free_space"]["schema_version"] == "1"
    assert cases["reference"]["schema_version"] == "2"
    assert cases["reference"]["scenario"]["propagation"]["refinement"] == 1
    assert cases["refined"]["scenario"]["propagation"]["refinement"] == 2
    assert result["comparison"]["max_abs_gaseous_difference_db"] < 0.001
    assert result["comparison"]["link_count"] > 0
    assert scenario.propagation.refinement == 1


def test_study_exports_verified_cases_and_results(tmp_path):
    result = run_propagation_study(_short_scenario(tmp_path))
    output = tmp_path / "study"
    write_propagation_study(result, output)
    stored = json.loads((output / "propagation-study.json").read_text())
    assert stored["comparison"] == result["comparison"]
    assert (output / "comparison.csv").is_file()
    for name in ("free_space", "reference", "refined"):
        doc = load_experiment(output / name)
        assert doc == result["cases"][name]
        assert doc["provenance"]["scenario_canonical_json"]


def test_study_requires_an_explicit_reference_atmosphere(tmp_path):
    scenario = replace(_short_scenario(tmp_path), propagation=None)
    with pytest.raises(ValueError, match="reference"):
        run_propagation_study(scenario)


def test_study_rejects_a_grid_without_a_supported_refinement(tmp_path):
    scenario = _short_scenario(tmp_path)
    scenario = replace(scenario, propagation=replace(scenario.propagation, refinement=4))
    with pytest.raises(ValueError, match="refinement"):
        run_propagation_study(scenario)


def test_study_cli_exports_three_cases_and_figure(tmp_path, capsys):
    scenario = _short_scenario(tmp_path)
    output = tmp_path / "study"
    figure = tmp_path / "ablation.svg"
    assert (
        main(
            [
                "propagation-study",
                str(scenario.source_path),
                "--output",
                str(output),
                "--figure",
                str(figure),
            ]
        )
        == 0
    )
    assert (output / "propagation-study.json").is_file()
    assert "P.835-7" in figure.read_text()
    assert "study:" in capsys.readouterr().out


def test_study_cli_rejects_free_space_config_without_traceback(tmp_path, capsys):
    assert (
        main(
            [
                "propagation-study",
                "examples/constellations/iridium_global.json",
                "--output",
                str(tmp_path / "out"),
            ]
        )
        == 2
    )
    assert "reference propagation model is required" in capsys.readouterr().err
    assert not (tmp_path / "out").exists()


def test_study_figure_missing_extra_has_actionable_error(tmp_path, monkeypatch):
    original = builtins.__import__

    def without_matplotlib(name, *args, **kwargs):
        if name == "matplotlib":
            raise ModuleNotFoundError("No module named matplotlib", name="matplotlib")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_matplotlib)
    with pytest.raises(ValueError, match=r"install openleo-link\[plot\]"):
        render_propagation_study({}, tmp_path / "figure.svg")


def test_study_figure_rejects_other_suffixes(tmp_path):
    with pytest.raises(ValueError, match=".svg or .png"):
        render_propagation_study({}, tmp_path / "figure.html")


@pytest.mark.parametrize("suffix", ["svg", "png"])
def test_study_figure_is_deterministic(tmp_path, suffix):
    result = run_propagation_study(_short_scenario(tmp_path))
    figure = tmp_path / f"figure.{suffix}"
    render_propagation_study(result, figure)
    first = figure.read_bytes()
    render_propagation_study(result, figure)
    assert figure.read_bytes() == first
