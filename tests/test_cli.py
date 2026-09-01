import builtins
import subprocess
import sys
from pathlib import Path

import pytest

from openleo.cli import main

FROZEN_SCENARIO = Path("examples/scenarios/iss_cartagena.json")
CANONICAL_STUDY = Path("examples/sensitivity/iss_cartagena_oat.json")
CANONICAL_GASES_BENCHMARK = Path("examples/atmosphere/p676_13_validation.json")


def test_core_import_exports_gases_and_sensitivity_without_matplotlib() -> None:
    subprocess.run(
        [
            sys.executable,
            "-c",
            """
import sys
import openleo
assert "matplotlib" not in sys.modules
assert {
    "GasesBenchmark",
    "GasesCase",
    "GasesResult",
    "SensitivityCase",
    "SensitivityMetrics",
    "SensitivityResult",
    "SensitivityStudy",
    "SensitivitySweep",
    "SpecificGaseousAttenuation",
    "load_gases_benchmark",
    "load_sensitivity_study",
    "run_gases_benchmark",
    "run_sensitivity",
    "specific_gaseous_attenuation",
    "write_gases_result",
    "write_sensitivity_result",
} <= set(openleo.__all__)
""",
        ],
        check=True,
    )


def test_main_run_writes_outputs_and_stable_success_summary(tmp_path, capsys) -> None:
    output_dir = tmp_path / "outputs"

    assert main(["run", str(FROZEN_SCENARIO), "--output", str(output_dir)]) == 0

    assert (output_dir / "trace.csv").is_file()
    assert (output_dir / "summary.json").is_file()
    assert capsys.readouterr().out == (
        "scenario: iss-cartagena-s-band-free-space\n"
        "rows: 40\n"
        "sampled AOS: 2026-08-30T06:15:30Z\n"
        "sampled LOS: 2026-08-30T06:22:00Z\n"
        f"trace: {output_dir / 'trace.csv'}\n"
        f"summary: {output_dir / 'summary.json'}\n"
    )


def test_main_missing_scenario_returns_input_error_without_traceback(tmp_path, capsys) -> None:
    missing = tmp_path / "missing.json"

    assert main(["run", str(missing), "--output", str(tmp_path / "out")]) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.count("error:") == 1
    assert str(missing) in captured.err
    assert "Traceback" not in captured.err
    assert not (tmp_path / "out").exists()


def test_main_invalid_json_returns_input_error_with_reason(tmp_path, capsys) -> None:
    scenario = tmp_path / "scenario.json"
    scenario.write_text("{", encoding="utf-8")

    assert main(["run", str(scenario), "--output", str(tmp_path / "out")]) == 2

    captured = capsys.readouterr()
    assert captured.err.count("error:") == 1
    assert str(scenario) in captured.err
    assert "invalid JSON" in captured.err
    assert "Traceback" not in captured.err


def test_main_sensitivity_writes_deterministic_artifacts(tmp_path, capsys) -> None:
    output_dir = tmp_path / "sensitivity"

    assert (
        main(
            ["sensitivity", str(FROZEN_SCENARIO), str(CANONICAL_STUDY), "--output", str(output_dir)]
        )
        == 0
    )
    assert (output_dir / "sensitivity.csv").is_file()
    assert (output_dir / "sensitivity-summary.json").is_file()
    assert capsys.readouterr().out == (
        "study: iss-cartagena-free-space-oat\n"
        "method: deterministic_one_at_a_time\n"
        "sweeps: 5\n"
        "cases: 20\n"
        f"sensitivity: {output_dir / 'sensitivity.csv'}\n"
        f"summary: {output_dir / 'sensitivity-summary.json'}\n"
    )


def test_main_gases_writes_deterministic_artifacts(tmp_path, capsys) -> None:
    output_dir = tmp_path / "gases"

    assert main(["gases", str(CANONICAL_GASES_BENCHMARK), "--output", str(output_dir)]) == 0

    assert (output_dir / "gaseous-specific-attenuation.csv").is_file()
    assert (output_dir / "gaseous-specific-attenuation-summary.json").is_file()
    assert capsys.readouterr().out == (
        "benchmark: itu-p676-13-specific-attenuation-validation\n"
        "recommendation: ITU-R P.676-13\n"
        "method: annex1_line_by_line_specific_attenuation\n"
        "cases: 5\n"
        f"csv: {output_dir / 'gaseous-specific-attenuation.csv'}\n"
        f"summary: {output_dir / 'gaseous-specific-attenuation-summary.json'}\n"
    )


def test_main_sensitivity_missing_study_returns_input_error_without_traceback(
    tmp_path, capsys
) -> None:
    missing = tmp_path / "missing.json"

    assert (
        main(["sensitivity", str(FROZEN_SCENARIO), str(missing), "--output", str(tmp_path / "out")])
        == 2
    )

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.count("error:") == 1
    assert str(missing) in captured.err
    assert "Traceback" not in captured.err
    assert not (tmp_path / "out").exists()


def test_main_help_uses_standard_argparse_exit(capsys) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["--help"])

    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert "run a scenario" in captured.out
    assert "render a completed pass overview" in captured.out
    assert "run a deterministic sensitivity study" in captured.out
    assert "run a gaseous specific-attenuation benchmark" in captured.out
    assert "render a completed sensitivity overview" in captured.out
    assert "render a completed gaseous attenuation overview" in captured.out
    assert "openleo run SCENARIO.json --output DIRECTORY" in captured.out
    assert "openleo sensitivity SCENARIO.json SENSITIVITY.json --output DIRECTORY" in captured.out
    assert "openleo gases CONFIG.json --output DIRECTORY" in captured.out
    assert "openleo plot RUN_DIRECTORY --output FILE.svg" in captured.out
    assert "openleo plot-sensitivity RUN_DIRECTORY --output FILE.svg" in captured.out
    assert "openleo plot-gases RUN_DIRECTORY --output FILE.svg" in captured.out


def test_main_plot_writes_svg_and_stable_success_label(tmp_path, capsys) -> None:
    run_directory = tmp_path / "run"
    output_path = tmp_path / "overview.svg"
    assert main(["run", str(FROZEN_SCENARIO), "--output", str(run_directory)]) == 0
    capsys.readouterr()

    assert main(["plot", str(run_directory), "--output", str(output_path)]) == 0

    assert output_path.is_file()
    assert output_path.stat().st_size > 0
    assert capsys.readouterr().out == f"plot: {output_path}\n"


def test_main_plot_unsupported_suffix_returns_error_without_traceback(tmp_path, capsys) -> None:
    run_directory = tmp_path / "run"
    output_path = tmp_path / "overview.txt"
    assert main(["run", str(FROZEN_SCENARIO), "--output", str(run_directory)]) == 0
    capsys.readouterr()

    assert main(["plot", str(run_directory), "--output", str(output_path)]) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.count("error:") == 1
    assert "output_path must end in .svg or .png" in captured.err
    assert "Traceback" not in captured.err
    assert not output_path.exists()


def test_main_plot_missing_matplotlib_returns_install_error(tmp_path, capsys, monkeypatch) -> None:
    run_directory = tmp_path / "run"
    output_path = tmp_path / "overview.svg"
    assert main(["run", str(FROZEN_SCENARIO), "--output", str(run_directory)]) == 0
    capsys.readouterr()
    original_import = builtins.__import__

    def block_matplotlib(name, *args, **kwargs):
        if name == "matplotlib":
            raise ModuleNotFoundError("No module named 'matplotlib'", name="matplotlib")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", block_matplotlib)

    assert main(["plot", str(run_directory), "--output", str(output_path)]) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.count("error:") == 1
    assert "install openleo-link[plot]" in captured.err
    assert "Traceback" not in captured.err


def test_main_plot_sensitivity_writes_svg_and_stable_success_label(tmp_path, capsys) -> None:
    run_directory = tmp_path / "sensitivity"
    output_path = tmp_path / "sensitivity-overview.svg"
    assert (
        main(
            [
                "sensitivity",
                str(FROZEN_SCENARIO),
                str(CANONICAL_STUDY),
                "--output",
                str(run_directory),
            ]
        )
        == 0
    )
    capsys.readouterr()

    assert main(["plot-sensitivity", str(run_directory), "--output", str(output_path)]) == 0

    assert output_path.is_file()
    assert capsys.readouterr().out == f"plot: {output_path}\n"


@pytest.mark.parametrize(
    ("run_kind", "output_name", "message"),
    [
        ("missing", "overview.svg", "could not read sensitivity-summary.json"),
        ("malformed", "overview.svg", "schema_version"),
        ("missing", "overview.txt", "output_path must end in .svg or .png"),
    ],
)
def test_main_plot_sensitivity_returns_artifact_errors_without_traceback(
    tmp_path, capsys, run_kind, output_name, message
) -> None:
    run_directory = tmp_path / "sensitivity"
    if run_kind == "malformed":
        run_directory.mkdir()
        (run_directory / "sensitivity-summary.json").write_text("{}", encoding="utf-8")
    output_path = tmp_path / output_name

    assert main(["plot-sensitivity", str(run_directory), "--output", str(output_path)]) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.count("error:") == 1
    assert message in captured.err
    assert "Traceback" not in captured.err
    assert not output_path.exists()


def test_main_plot_sensitivity_missing_matplotlib_returns_install_error(
    tmp_path, capsys, monkeypatch
) -> None:
    run_directory = tmp_path / "sensitivity"
    output_path = tmp_path / "overview.svg"
    assert (
        main(
            [
                "sensitivity",
                str(FROZEN_SCENARIO),
                str(CANONICAL_STUDY),
                "--output",
                str(run_directory),
            ]
        )
        == 0
    )
    capsys.readouterr()
    original_import = builtins.__import__

    def block_matplotlib(name, *args, **kwargs):
        if name == "matplotlib":
            raise ModuleNotFoundError("No module named 'matplotlib'", name="matplotlib")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", block_matplotlib)

    assert main(["plot-sensitivity", str(run_directory), "--output", str(output_path)]) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.count("error:") == 1
    assert "install openleo-link[plot]" in captured.err
    assert "Traceback" not in captured.err


def test_main_plot_gases_writes_svg_and_stable_success_label(tmp_path, capsys) -> None:
    run_directory = tmp_path / "gases"
    output_path = tmp_path / "gases-overview.svg"
    assert main(["gases", str(CANONICAL_GASES_BENCHMARK), "--output", str(run_directory)]) == 0
    capsys.readouterr()

    assert main(["plot-gases", str(run_directory), "--output", str(output_path)]) == 0

    assert output_path.is_file()
    assert capsys.readouterr().out == f"plot: {output_path}\n"


@pytest.mark.parametrize(
    ("run_kind", "output_name", "message"),
    [
        ("missing", "overview.svg", "could not read gaseous-specific-attenuation-summary.json"),
        ("malformed", "overview.svg", "schema_version"),
        ("missing", "overview.txt", "output_path must end in .svg or .png"),
    ],
)
def test_main_plot_gases_returns_artifact_errors_without_traceback(
    tmp_path, capsys, run_kind, output_name, message
) -> None:
    run_directory = tmp_path / "gases"
    if run_kind == "malformed":
        run_directory.mkdir()
        (run_directory / "gaseous-specific-attenuation-summary.json").write_text(
            "{}", encoding="utf-8"
        )
    output_path = tmp_path / output_name

    assert main(["plot-gases", str(run_directory), "--output", str(output_path)]) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.count("error:") == 1
    assert message in captured.err
    assert "Traceback" not in captured.err
    assert not output_path.exists()


def test_main_plot_gases_missing_matplotlib_returns_install_error(
    tmp_path, capsys, monkeypatch
) -> None:
    run_directory = tmp_path / "gases"
    output_path = tmp_path / "overview.svg"
    assert main(["gases", str(CANONICAL_GASES_BENCHMARK), "--output", str(run_directory)]) == 0
    capsys.readouterr()
    original_import = builtins.__import__

    def block_matplotlib(name, *args, **kwargs):
        if name == "matplotlib":
            raise ModuleNotFoundError("No module named 'matplotlib'", name="matplotlib")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", block_matplotlib)

    assert main(["plot-gases", str(run_directory), "--output", str(output_path)]) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.count("error:") == 1
    assert "install openleo-link[plot]" in captured.err
    assert "Traceback" not in captured.err
