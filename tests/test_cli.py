"""The OpenLEO 2.0 command line (design spec section 8), driven through ``main``."""

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from openleo import cli
from openleo.cli import RETIRED, main
from openleo.engine.run import run, save_run
from openleo.report import write_report
from openleo.results import load_results
from openleo.scenario import load_scenario, parse_scenario, scenario_document

ROOT = Path(__file__).resolve().parents[1]
ISS = ROOT / "examples" / "scenarios" / "iss_cartagena.json"
GLOBAL = ROOT / "examples" / "constellations" / "iridium_global.json"
V1_SCENARIO = ROOT / "tests" / "fixtures" / "v1" / "examples" / "scenarios" / "iss_cartagena.json"
REFERENCE = ROOT / "examples" / "validation" / "vallado_06251.json"
GASES = ROOT / "examples" / "atmosphere" / "p676_13_validation.json"
HYDROMETEORS = ROOT / "examples" / "atmosphere" / "hydrometeors_reference.json"
BENCHMARK = ROOT / "examples" / "reproduce" / "release_benchmark.json"


def _small(path: Path, tmp_path: Path, **changes) -> Path:
    """A shortened copy of an example scenario, with its catalog paths kept valid."""
    scenario = load_scenario(path)
    document = scenario_document(scenario)
    document["time"]["stop_utc"] = changes.get("stop_utc", document["time"]["stop_utc"])
    document["time"]["convergence"]["mode"] = "off"
    if "norad_ids" in changes:
        document["space_segment"]["select_norad_ids"] = changes["norad_ids"]
    for catalog in document["space_segment"]["catalogs"]:
        catalog["path"] = str((path.parent / catalog["path"]).resolve())
    target = tmp_path / "scenario.json"
    target.write_text(json.dumps(document), encoding="utf-8")
    return target


@pytest.fixture(scope="module")
def iss_run(tmp_path_factory):
    directory = tmp_path_factory.mktemp("iss")
    scenario = _small(ISS, directory)
    output = directory / "run"
    assert main(["run", str(scenario), "--output", str(output)]) == 0
    return output


@pytest.fixture(scope="module")
def network_run(tmp_path_factory):
    """A small run with a network, for the replay commands."""
    directory = tmp_path_factory.mktemp("network")
    scenario = load_scenario(GLOBAL)
    document = scenario_document(scenario)
    document["time"]["stop_utc"] = "2026-09-10T12:05:00Z"
    document["time"]["convergence"]["mode"] = "off"
    document["space_segment"]["select_norad_ids"] = [42960, 42961, 43570, 43572]
    parsed = parse_scenario(document, GLOBAL, scenario.source_sha256)
    return save_run(parsed, run(parsed), directory / "run")


def _sweep_spec(directory: Path) -> Path:
    spec = {
        "schema": "openleo.sweep/2",
        "name": "cli sweep",
        "base_scenario": os.path.relpath(ISS, directory),
        "mode": "oat",
        "parameters": [{"path": "/radio/eirp_dbw", "values": [7.0, 10.0], "nominal": 10.0}],
    }
    path = directory / "sweep.json"
    path.write_text(json.dumps(spec), encoding="utf-8")
    return path


# ------------------------------------------------------------------------- success paths


def test_every_subcommand_success_path(tmp_path, capsys, monkeypatch, iss_run, network_run):
    def out() -> str:
        return capsys.readouterr().out

    # scenario new / validate / explain / upgrade
    assert (
        main(["scenario", "new", "--preset", "constellation", "-o", str(tmp_path / "n.json")]) == 0
    )
    new_text = (tmp_path / "n.json").read_text(encoding="utf-8")
    assert json.loads(new_text)["schema"] == "openleo.scenario/2"
    capsys.readouterr()
    assert main(["scenario", "new"]) == 0
    assert json.loads(out())["schema"] == "openleo.scenario/2"
    assert main(["scenario", "validate", str(ISS)]) == 0
    assert "valid" in out()
    assert main(["scenario", "explain", str(ISS)]) == 0
    assert out().startswith("# iss-cartagena")
    assert main(["scenario", "upgrade", str(V1_SCENARIO)]) == 0
    assert json.loads(out())["schema"] == "openleo.scenario/2"
    upgraded = tmp_path / "up" / "v2.json"
    upgraded.parent.mkdir()
    assert main(["scenario", "upgrade", str(V1_SCENARIO), "-o", str(upgraded)]) == 0
    assert out() == ""
    load_scenario(upgraded)

    # run
    run_dir = tmp_path / "run"
    assert main(["run", str(_small(ISS, tmp_path)), "--output", str(run_dir)]) == 0
    text = out()
    assert str(run_dir) in text
    assert "convergence: 0 metrics, 0 failures (mode=off)" in text
    assert (run_dir / "results.json").is_file()

    # sweep
    sweep_dir = tmp_path / "sweep"
    assert main(["sweep", str(_sweep_spec(tmp_path)), "--output", str(sweep_dir)]) == 0
    assert str(sweep_dir) in out()
    assert (sweep_dir / "sweep.json").is_file()

    # replay link / network (the backends are trusted binaries: faked at the library boundary)
    calls = {}

    def fake_link(results_dir, config, backend, output_dir):
        calls["link"] = (Path(results_dir), config, Path(backend), Path(output_dir))
        return {}

    def fake_network(results_dir, config, backend, output_dir):
        calls["network"] = (Path(results_dir), config, Path(backend), Path(output_dir))
        return {}

    monkeypatch.setattr(cli, "run_packet_replay", fake_link)
    monkeypatch.setattr(cli, "run_network_replay", fake_network)
    link_out = tmp_path / "link"
    assert (
        main(
            [
                "replay",
                "link",
                str(iss_run),
                "--station",
                "Cartagena",
                "--satellite",
                "25544",
                "--backend",
                "ns3-link",
                "--output",
                str(link_out),
                "--offered-load-bps",
                "250000",
                "--packet-size-bytes",
                "1000",
                "--queue-packets",
                "16",
                "--seed",
                "7",
                "--start-offset-s",
                "5",
                "--duration-s",
                "30",
            ]
        )
        == 0
    )
    assert str(link_out / "packet-summary.json") in out()
    results_dir, link_config, backend, output_dir = calls["link"]
    assert (results_dir, backend, output_dir) == (iss_run, Path("ns3-link"), link_out)
    assert (link_config.station_name, link_config.norad_id) == ("Cartagena", 25544)
    assert (link_config.offered_load_bps, link_config.packet_size_bytes) == (250000, 1000)
    assert (link_config.queue_packets, link_config.seed) == (16, 7)
    assert (link_config.start_offset_s, link_config.duration_s) == (5, 30)

    pair = _first_pair(network_run)
    network_out = tmp_path / "network"
    assert (
        main(
            [
                "replay",
                "network",
                str(network_run),
                "--pair",
                pair[0],
                "--model",
                "maximum_rate",
                "--backend",
                "ns3-net",
                "--output",
                str(network_out),
                "--flows-per-direction",
                "2",
                "--acquisition-delay-s",
                "1.5",
            ]
        )
        == 0
    )
    assert str(network_out / "network-summary.json") in out()
    _, network_config, backend, _ = calls["network"]
    assert backend == Path("ns3-net")
    assert network_config.pair_index == pair[1]
    assert network_config.routing_model == "maximum_rate"
    assert (network_config.flows_per_direction, network_config.acquisition_delay_s) == (2, 1.5)

    # reproduce
    seen = {}

    def fake_reproduce(config, reference, backend, output_dir):
        seen["reproduce"] = (Path(config), Path(reference), Path(backend), Path(output_dir))
        return {"native_packet_replays": 3}

    monkeypatch.setattr(cli, "run_reproduce", fake_reproduce)
    repro_out = tmp_path / "repro"
    assert (
        main(
            [
                "reproduce",
                str(BENCHMARK),
                "--reference",
                str(REFERENCE),
                "--network-backend",
                "ns3-net",
                "--output",
                str(repro_out),
            ]
        )
        == 0
    )
    assert seen["reproduce"] == (BENCHMARK, REFERENCE, Path("ns3-net"), repro_out)
    assert "native replays: 3" in out()

    # verify
    assert main(["verify", "orbit", str(REFERENCE)]) == 0
    assert json.loads(out())["passed"] is True

    # calc
    assert main(["calc", "gases", str(GASES), "--output", str(tmp_path / "gases")]) == 0
    assert "benchmark:" in out()
    assert (tmp_path / "gases").is_dir()
    assert main(["calc", "hydrometeors", str(HYDROMETEORS), "--output", str(tmp_path / "h")]) == 0
    assert "report:" in out()

    # report / export
    report_dir = tmp_path / "report"
    assert main(["report", str(iss_run), "--output", str(report_dir)]) == 0
    assert (report_dir / "report.md").is_file()
    assert str(report_dir) in out()
    figures_dir = tmp_path / "figures"
    assert main(["report", str(iss_run), "--output", str(figures_dir), "--figures"]) == 0
    assert any(figures_dir.glob("*.png"))
    capsys.readouterr()
    export_dir = tmp_path / "export"
    assert main(["export", str(iss_run), "--to", "parquet", "--output", str(export_dir)]) == 0
    assert str(export_dir) in out()
    assert any((export_dir / "tables").glob("*.parquet"))

    # app
    app_calls = {}

    def fake_app(scenario_path, **options):
        app_calls.update(path=Path(scenario_path), **options)

    monkeypatch.setattr(cli, "run_app", fake_app)
    assert (
        main(
            [
                "app",
                str(ISS),
                "--port",
                "9001",
                "--no-browser",
                "--packet-backend",
                "p",
                "--network-backend",
                "n",
                "--artifact-limit",
                "2",
            ]
        )
        == 0
    )
    assert app_calls == {
        "path": ISS,
        "port": 9001,
        "open_browser": False,
        "packet_backend": Path("p"),
        "network_backend": Path("n"),
        "artifact_limit_bytes": 2 * 2**20,
    }
    assert main(["app", str(ISS)]) == 0
    assert app_calls["open_browser"] is True
    assert app_calls["packet_backend"] is None
    assert app_calls["artifact_limit_bytes"] == cli.DEFAULT_ARTIFACT_LIMIT_BYTES


def _first_pair(results_dir: Path) -> tuple[str, int]:
    from openleo.replay import ReplaySource

    source = ReplaySource.from_results(results_dir)
    first = source.route_pairs[0]
    return f"{source.station_names[first[0]]}:{source.station_names[first[1]]}", 0


def test_run_summary_lists_convergence_and_route_classes(tmp_path, capsys):
    scenario = load_scenario(GLOBAL)
    document = scenario_document(scenario)
    document["time"]["stop_utc"] = "2026-09-10T12:03:00Z"
    document["space_segment"]["select_norad_ids"] = [42960, 42961]
    for catalog in document["space_segment"]["catalogs"]:
        catalog["path"] = str((GLOBAL.parent / catalog["path"]).resolve())
    path = tmp_path / "scenario.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    assert main(["run", str(path), "--output", str(tmp_path / "run"), "--workers", "1"]) == 0
    line = next(
        line for line in capsys.readouterr().out.splitlines() if line.startswith("convergence:")
    )
    metadata = load_results(tmp_path / "run").metadata["convergence"]
    summary = metadata["summary"]
    assert line.startswith(
        f"convergence: {summary['metrics']} metrics, {summary['failed']} failures (mode=warn)"
    )
    route = summary["classes"]["route"]
    assert f"route: {route['metrics']} metrics, {route['failed']} failures" in line


def test_replay_network_pair_errors(tmp_path, capsys, network_run):
    args = [
        "replay",
        "network",
        str(network_run),
        "--backend",
        "b",
        "--output",
        str(tmp_path / "o"),
    ]
    assert main([*args, "--pair", "Nowhere:Else", "--model", "minimum_delay"]) == 1
    err = capsys.readouterr().err
    assert err.startswith("error: ") and "Nowhere:Else" in err
    assert main([*args, "--pair", "nocolon", "--model", "minimum_delay"]) == 1
    assert "SRC:DST" in capsys.readouterr().err


# ------------------------------------------------------------------------------- errors


def _fails(argv, capsys, code=1):
    assert main(argv) == code
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err + captured.out
    return captured


def test_user_errors_have_no_traceback(tmp_path, capsys, iss_run):
    missing = tmp_path / "missing.json"
    out = tmp_path / "out"
    cases = [
        ["run", str(missing), "--output", str(out)],
        ["run", str(V1_SCENARIO), "--output", str(out)],
        ["run", str(ISS), "--output", str(iss_run)],
        ["run", str(ISS), "--output", str(out), "--formats", "csv,xml"],
        ["run", str(ISS), "--output", str(out), "--workers", "0"],
        ["run", str(ISS), "--output", str(out), "--memory-budget", "-1"],
        ["scenario", "validate", str(missing)],
        ["scenario", "explain", str(missing)],
        ["scenario", "upgrade", str(missing)],
        ["scenario", "upgrade", str(ISS)],
        ["scenario", "new", "-o", str(tmp_path / "no" / "dir" / "x.json")],
        ["sweep", str(missing), "--output", str(out)],
        ["replay", "link", str(tmp_path), "--station", "A", "--satellite", "1", "--backend", "b"]
        + ["--output", str(out)],
        ["replay", "network", str(tmp_path), "--pair", "A:B", "--model", "minimum_delay"]
        + ["--backend", "b", "--output", str(out)],
        ["reproduce", str(missing), "--reference", str(REFERENCE), "--network-backend", "b"]
        + ["--output", str(out)],
        ["verify", "orbit", str(missing)],
        ["calc", "gases", str(missing), "--output", str(out)],
        ["calc", "hydrometeors", str(missing), "--output", str(out)],
        ["report", str(tmp_path), "--output", str(out)],
        ["export", str(tmp_path), "--to", "csv", "--output", str(out)],
        ["app", str(missing)],
        ["app", str(ISS), "--artifact-limit", "0"],
    ]
    for argv in cases:
        captured = _fails(argv, capsys)
        assert captured.err.startswith("error: "), argv
        assert captured.err.count("\n") == 1, (argv, captured.err)
    assert not out.exists()


def test_v1_scenario_points_at_upgrade(capsys):
    captured = _fails(["run", str(V1_SCENARIO), "--output", "unused"], capsys)
    assert "run `openleo scenario upgrade" in captured.err


def test_unreadable_orbit_reference_fails_the_verification_exit_code(tmp_path, capsys):
    data = json.loads(REFERENCE.read_text())
    data["states"][0]["position_km"][0] += 1
    failing = tmp_path / "reference.json"
    failing.write_text(json.dumps(data), encoding="utf-8")
    assert main(["verify", "orbit", str(failing)]) == 1
    assert json.loads(capsys.readouterr().out)["passed"] is False


def test_itu_workbook_is_not_available_until_s3(capsys):
    assert main(["verify", "itu-workbook", "workbook.xlsx"]) == 2
    captured = capsys.readouterr()
    assert captured.err == "openleo verify itu-workbook is not available until S3\n"
    assert captured.out == ""


def test_argparse_usage_errors_exit_two(capsys):
    for argv in ([], ["run"], ["bogus"], ["export", "x", "--to", "xml", "--output", "y"]):
        with pytest.raises(SystemExit) as raised:
            main(argv)
        assert raised.value.code == 2
    assert "Traceback" not in capsys.readouterr().err


def test_help_uses_standard_argparse_exit(capsys):
    with pytest.raises(SystemExit) as raised:
        main(["--help"])
    assert raised.value.code == 0
    text = capsys.readouterr().out
    for command in ("scenario", "run", "sweep", "replay", "reproduce", "verify", "calc", "report"):
        assert command in text


# --------------------------------------------------------------------- retired commands

RETIRED_CASES = [
    ("constellation", "run"),
    ("experiment", "sweep with variants, plus replay"),
    ("fidelity-study", "sweep"),
    ("propagation-study", "sweep"),
    ("sensitivity", "sweep"),
    ("benchmark", "reproduce / sweep"),
    ("gases", "calc gases"),
    ("hydrometeors", "calc hydrometeors"),
    ("verify-orbit", "verify orbit"),
    ("plot", "report --figures"),
    ("plot-sensitivity", "report --figures"),
    ("plot-gases", "report --figures"),
    ("packet-replay", "replay link"),
    ("network-replay", "replay network"),
]


@pytest.mark.parametrize(("old", "new"), RETIRED_CASES)
def test_retired_commands_point_to_replacements(old, new, capsys):
    # v1 arguments after the command name are ignored: the message is all the user needs.
    assert main([old, "SOME.json", "--output", "somewhere", "--unknown-v1-flag"]) == 2
    captured = capsys.readouterr()
    assert captured.err == f"openleo {old} was removed in 2.0; use: openleo {new}\n"
    assert captured.out == ""


def test_retired_table_matches_spec_and_leaves_run_alone():
    assert {old: new for old, new in RETIRED_CASES} == dict(RETIRED)
    assert "run" not in RETIRED


# ------------------------------------------------------------------------------- options


def test_formats_flag_overrides_outputs(tmp_path):
    scenario = _small(ISS, tmp_path)
    default = tmp_path / "default"
    assert main(["run", str(scenario), "--output", str(default)]) == 0
    assert any((default / "tables").glob("*.olc")) and not any(
        (default / "tables").glob("*.parquet")
    )

    csv_only = tmp_path / "csv"
    assert main(["run", str(scenario), "--output", str(csv_only), "--formats", "csv"]) == 0
    assert any((csv_only / "tables").glob("*.csv")) and not any((csv_only / "tables").glob("*.olc"))

    both = tmp_path / "both"
    assert main(["run", str(scenario), "--output", str(both), "--formats", "csv,parquet"]) == 0
    assert any((both / "tables").glob("*.parquet")) and not any((both / "tables").glob("*.olc"))


def test_memory_budget_flag(tmp_path, monkeypatch, capsys):
    scenario = _small(ISS, tmp_path)
    seen = {}
    real = cli.run_scenario

    def spy(loaded, **options):
        seen.update(options)
        return real(loaded, **options)

    monkeypatch.setattr(cli, "run_scenario", spy)
    assert (
        main(
            ["run", str(scenario), "--output", str(tmp_path / "a"), "--memory-budget", "0.5"]
            + ["--workers", "1"]
        )
        == 0
    )
    assert seen["memory_budget_bytes"] == 2**29
    assert seen["workers"] == 1

    captured = _fails(
        ["run", str(scenario), "--output", str(tmp_path / "b"), "--memory-budget", "0.000001"],
        capsys,
    )
    assert "exceeds the budget of 1073 bytes" in captured.err
    assert not (tmp_path / "b").exists()


def test_workers_flag_reaches_the_sweep(tmp_path, monkeypatch):
    seen = {}

    def fake_sweep(spec, output, **options):
        seen.update(options)
        return {"runs": []}

    monkeypatch.setattr(cli, "run_sweep", fake_sweep)
    assert main(["sweep", "spec.json", "--output", "o", "--workers", "3"]) == 0
    assert seen["workers"] == 3


# ------------------------------------------------------------------ scenario validation


def test_validate_checks_the_event_step_against_the_catalog(tmp_path, capsys):
    scenario = load_scenario(ISS)
    document = scenario_document(scenario)
    document["time"]["step_s"] = 1000.0
    document["time"]["stop_utc"] = "2026-08-30T09:00:00Z"
    for catalog in document["space_segment"]["catalogs"]:
        catalog["path"] = str((ISS.parent / catalog["path"]).resolve())
    path = tmp_path / "coarse.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    captured = _fails(["scenario", "validate", str(path)], capsys)
    assert "exceeds the event-detection limit P_min/8" in captured.err
    # the same text `run` reports
    run_captured = _fails(["run", str(path), "--output", str(tmp_path / "o")], capsys)
    assert run_captured.err == captured.err

    document["time"]["events"]["enabled"] = False
    path.write_text(json.dumps(document), encoding="utf-8")
    assert main(["scenario", "validate", str(path)]) == 0


def test_validate_with_an_unreadable_catalog_says_it_was_not_checked(tmp_path, capsys):
    document = scenario_document(load_scenario(ISS))
    document["space_segment"]["catalogs"][0]["path"] = str(tmp_path / "gone.csv")
    path = tmp_path / "scenario.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    assert main(["scenario", "validate", str(path)]) == 0
    out = capsys.readouterr().out
    assert "valid" in out and "catalog not checked" in out


# --------------------------------------------------------------------- launcher / report


def test_launcher_sets_blas_threads_before_numpy_loads():
    code = (
        "import os, sys\n"
        "import openleo._launcher\n"
        "assert 'numpy' not in sys.modules, 'importing the launcher imported numpy'\n"
        "assert 'OPENBLAS_NUM_THREADS' not in os.environ\n"
        "import openleo\n"
        "assert 'numpy' not in sys.modules, 'import openleo imported numpy'\n"
        "sys.argv = ['openleo', 'scenario', 'new']\n"
        "try:\n"
        "    openleo._launcher.main()\n"
        "except SystemExit:\n"
        "    pass\n"
        "assert os.environ['OPENBLAS_NUM_THREADS'] == '1'\n"
        "assert os.environ['OMP_NUM_THREADS'] == '1'\n"
        "import numpy\n"
    )
    environment = {
        k: v for k, v in os.environ.items() if k not in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS")
    }
    done = subprocess.run(
        [sys.executable, "-c", code], env=environment, capture_output=True, text=True, check=False
    )
    assert done.returncode == 0, done.stderr


def test_launcher_respects_the_users_thread_choice():
    code = "import openleo._launcher as l; l.main()"
    environment = {**os.environ, "OPENBLAS_NUM_THREADS": "4", "OMP_NUM_THREADS": "3"}
    probe = (
        "import os, sys\n"
        "sys.argv = ['openleo', 'scenario', 'new']\n"
        f"{code}\n"
        "assert os.environ['OPENBLAS_NUM_THREADS'] == '4'\n"
        "assert os.environ['OMP_NUM_THREADS'] == '3'\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", probe],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr


def test_console_script_points_at_the_launcher():
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'openleo = "openleo._launcher:main"' in text


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_report_directory_gets_default_permissions(iss_run, tmp_path):
    old = os.umask(0o022)
    try:
        output = write_report(iss_run, tmp_path / "report")
    finally:
        os.umask(old)
    assert stat.S_IMODE(output.stat().st_mode) == 0o755

    old = os.umask(0o077)
    try:
        private = write_report(iss_run, tmp_path / "private")
    finally:
        os.umask(old)
    assert stat.S_IMODE(private.stat().st_mode) == 0o700


def test_importing_the_package_keeps_lazy_exports():
    import openleo

    assert openleo.load_scenario is load_scenario
    assert "run" in dir(openleo) and "main" in openleo.__all__
    with pytest.raises(AttributeError):
        openleo.no_such_name  # noqa: B018
