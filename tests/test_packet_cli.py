from openleo.cli import main


def test_packet_replay_cli_rejects_missing_bundle_without_traceback(tmp_path, capsys):
    result = main(
        [
            "packet-replay",
            str(tmp_path / "missing"),
            "--station",
            "Madrid",
            "--norad",
            "42960",
            "--backend",
            str(tmp_path / "backend"),
            "--output",
            str(tmp_path / "results"),
        ]
    )
    assert result == 2
    message = capsys.readouterr().err
    assert "error:" in message and "Traceback" not in message
    assert not (tmp_path / "results").exists()
