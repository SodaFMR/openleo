import builtins
from pathlib import Path

import pytest

from openleo.cli import main

FROZEN_SCENARIO = Path("examples/scenarios/iss_cartagena.json")


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


def test_main_help_uses_standard_argparse_exit(capsys) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["--help"])

    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert "run" in captured.out
    assert "scenario" in captured.out
    assert "--output" in captured.out


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
