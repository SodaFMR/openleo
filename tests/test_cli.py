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
