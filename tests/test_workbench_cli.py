import json
from pathlib import Path

from openleo.cli import main
from openleo.workbench import load_experiment


def test_constellation_cli_exports_complete_reproducible_experiment(tmp_path, capsys):
    path = Path("examples/constellations/iridium_global.json")
    raw = json.loads(path.read_text())
    raw["orbit"]["path"] = str((path.parent / raw["orbit"]["path"]).resolve())
    raw["time_window"]["stop_utc"] = "2026-09-10T12:01:00Z"
    config = tmp_path / "scenario.json"
    config.write_text(json.dumps(raw))

    assert main(["constellation", str(config), "--output", str(tmp_path / "run")]) == 0

    document = load_experiment(tmp_path / "run")
    assert len(document["satellites"]) == 80
    assert len(document["stations"]) == 4
    assert len(document["timestamps_utc"]) == 2
    assert "satellites: 80" in capsys.readouterr().out


def test_constellation_cli_rejects_missing_config(tmp_path, capsys):
    assert (
        main(["constellation", str(tmp_path / "missing"), "--output", str(tmp_path / "run")]) == 2
    )
    assert "error:" in capsys.readouterr().err
    assert not (tmp_path / "run").exists()
