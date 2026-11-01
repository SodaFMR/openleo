"""The OpenLEO 2.0 command line (design spec section 8).

``main(argv)`` parses the command, calls the library and returns the exit code:

- 0: success;
- 1: a user-input error (a missing or invalid file, an invalid option value, a refused
  overwrite), printed as ``error: <message>`` on stderr, never as a traceback;
- 2: a retired v1 command (``openleo <old> was removed in 2.0; use: openleo <new>``),
  ``verify itu-workbook`` (stage S3), or a command-line usage error found by argparse.

Library errors are ``ValueError`` or ``OSError``; anything else is a bug and is left to raise.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from openleo.app.api import DEFAULT_ARTIFACT_LIMIT_BYTES
from openleo.app.server import run_app
from openleo.calc.gases import load_gases_benchmark, run_gases_benchmark, write_gases_result
from openleo.calc.hydrometeors import run_hydrometeors
from openleo.engine.catalogs import load_catalogs
from openleo.engine.run import check_step, save_run
from openleo.engine.run import run as run_scenario
from openleo.engine.timegrid import builtin_timescale
from openleo.export import export_results
from openleo.replay import (
    NetworkReplayConfig,
    ReplayConfig,
    ReplaySource,
    run_network_replay,
    run_packet_replay,
)
from openleo.replay.source import ROUTE_MODELS
from openleo.report import write_report
from openleo.reproduce import run_reproduce
from openleo.results.io import FORMATS
from openleo.scenario import explain, load_scenario, starter, upgrade_file
from openleo.sweep import run_sweep
from openleo.verify import verify_itu_workbook, verify_orbit_reference

__all__ = ["DEFAULT_ARTIFACT_LIMIT_BYTES", "RETIRED", "main"]

GIB = 2**30
MIB = 2**20

# Retired v1 commands and their replacements (spec section 8). v1's single-pass ``run`` is the
# v2 ``run``: it needs no entry, and a v1 scenario file gets the parser's own upgrade guidance.
RETIRED = {
    "constellation": "run",
    "experiment": "sweep with variants, plus replay",
    "fidelity-study": "sweep",
    "propagation-study": "sweep",
    "sensitivity": "sweep",
    "benchmark": "reproduce / sweep",
    "gases": "calc gases",
    "hydrometeors": "calc hydrometeors",
    "verify-orbit": "verify orbit",
    "plot": "report --figures",
    "plot-sensitivity": "report --figures",
    # report reads results and sweeps only; the P.676 figure renders a calc gases directory.
    "plot-gases": (
        "calc gases CONFIG --output DIR, then in Python "
        "openleo.calc.gases_figure.render_gases_overview(DIR, SVG) (plot extra)"
    ),
    "packet-replay": "replay link",
    "network-replay": "replay network",
}

_PACKET_OPTIONS = (
    ("start_offset_s", float, 0),
    ("duration_s", float, 60),
    ("offered_load_bps", float, 100_000),
    ("packet_size_bytes", int, 512),
    ("queue_packets", int, 32),
    ("seed", int, 1),
)
_NETWORK_OPTIONS = (
    ("flows_per_direction", int, 1),
    ("acquisition_delay_s", float, 0),
)


def _add_options(parser: argparse.ArgumentParser, options) -> None:
    for name, kind, default in options:
        parser.add_argument("--" + name.replace("_", "-"), type=kind, default=default)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="openleo",
        description="Simulate LEO satellite links, sweeps and packet replays reproducibly.",
        epilog=(
            "Examples:\n"
            "  openleo scenario new --preset constellation -o scenario.json\n"
            "  openleo run scenario.json --output runs/first\n"
            "  openleo sweep sweep.json --output runs/sweep\n"
            "  openleo report runs/first --output reports/first --figures\n"
            "  openleo app scenario.json"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    def command(name: str, handler: Callable, help_: str, parent=commands):
        subparser = parent.add_parser(name, help=help_, description=help_)
        subparser.set_defaults(handler=handler)
        return subparser

    scenario = commands.add_parser("scenario", help="create, check, explain or upgrade scenarios")
    scenario_commands = scenario.add_subparsers(dest="action", required=True, metavar="ACTION")
    new = command("new", _scenario_new, "write a starter scenario", scenario_commands)
    new.add_argument("--preset", choices=("single-pass", "constellation"), default="single-pass")
    new.add_argument("-o", "--output", metavar="FILE", help="write here instead of stdout")
    validate = command(
        "validate", _scenario_validate, "check a scenario and its catalogs", scenario_commands
    )
    validate.add_argument("scenario", metavar="SCENARIO.json")
    explain_ = command(
        "explain", _scenario_explain, "list every assumption of a scenario", scenario_commands
    )
    explain_.add_argument("scenario", metavar="SCENARIO.json")
    upgrade = command(
        "upgrade", _scenario_upgrade, "convert an OpenLEO 1.x scenario", scenario_commands
    )
    upgrade.add_argument("scenario", metavar="V1_SCENARIO.json")
    upgrade.add_argument("-o", "--output", metavar="FILE", help="write here instead of stdout")

    run = command("run", _run, "simulate a scenario and save its results")
    run.add_argument("scenario", metavar="SCENARIO.json")
    run.add_argument("--output", required=True, metavar="DIRECTORY")
    run.add_argument(
        "--formats",
        metavar="csv,olc,parquet",
        help="table formats, overriding the scenario's outputs.formats (csv is always written)",
    )
    run.add_argument("--workers", type=int, default=1, metavar="N")
    run.add_argument(
        "--memory-budget", type=float, default=None, metavar="GiB", help="estimate limit in GiB"
    )

    sweep = command("sweep", _sweep, "run a sweep spec")
    sweep.add_argument("sweep", metavar="SWEEP.json")
    sweep.add_argument("--output", required=True, metavar="DIRECTORY")
    sweep.add_argument("--workers", type=int, default=1, metavar="N")
    sweep.add_argument("--packet-backend", metavar="EXE", help="trusted ns-3 link replay binary")
    sweep.add_argument("--network-backend", metavar="EXE", help="trusted ns-3 network binary")

    replay = commands.add_parser("replay", help="replay a saved run through ns-3")
    replay_commands = replay.add_subparsers(dest="kind", required=True, metavar="KIND")
    link = command("link", _replay_link, "replay one ground link of a run", replay_commands)
    link.add_argument("results", metavar="RESULTS")
    link.add_argument("--station", required=True, help="exact station name")
    link.add_argument("--satellite", required=True, type=int, help="NORAD catalog id")
    link.add_argument("--backend", required=True, metavar="EXE", help="trusted ns-3 binary")
    link.add_argument("--output", required=True, metavar="DIRECTORY")
    _add_options(link, _PACKET_OPTIONS)
    network = command(
        "network", _replay_network, "replay one routed station pair of a run", replay_commands
    )
    network.add_argument("results", metavar="RESULTS")
    network.add_argument("--pair", required=True, metavar="SRC:DST", help="a network.pairs entry")
    network.add_argument("--model", required=True, choices=ROUTE_MODELS, help="routing model")
    network.add_argument("--backend", required=True, metavar="EXE", help="trusted ns-3 binary")
    network.add_argument("--output", required=True, metavar="DIRECTORY")
    _add_options(network, _PACKET_OPTIONS + _NETWORK_OPTIONS)

    reproduce = command("reproduce", _reproduce, "reproduce the frozen release evidence")
    reproduce.add_argument("config", metavar="CONFIG.json")
    reproduce.add_argument("--reference", required=True, metavar="ORBIT_REF.json")
    reproduce.add_argument("--network-backend", required=True, metavar="EXE")
    reproduce.add_argument("--output", required=True, metavar="DIRECTORY")

    verify = commands.add_parser("verify", help="check against published references")
    verify_commands = verify.add_subparsers(dest="target", required=True, metavar="TARGET")
    orbit = command(
        "orbit", _verify_orbit, "check a published TEME state reference", verify_commands
    )
    orbit.add_argument("reference", metavar="REFERENCE.json")
    workbook = command(
        "itu-workbook", _verify_workbook, "ITU-R workbook check (stage S3)", verify_commands
    )
    workbook.add_argument("path", metavar="PATH")

    calc = commands.add_parser("calc", help="standalone attenuation calculators")
    calc_commands = calc.add_subparsers(dest="calculator", required=True, metavar="CALCULATOR")
    gases = command("gases", _calc_gases, "P.676-13 specific attenuation", calc_commands)
    gases.add_argument("config", metavar="CONFIG.json")
    gases.add_argument("--output", required=True, metavar="DIRECTORY")
    hydrometeors = command(
        "hydrometeors", _calc_hydrometeors, "P.838 rain and P.840 cloud attenuation", calc_commands
    )
    hydrometeors.add_argument("config", metavar="CONFIG.json")
    hydrometeors.add_argument("--output", required=True, metavar="DIRECTORY")

    report = command("report", _report, "write a Markdown and CSV report")
    report.add_argument("path", metavar="RESULTS_OR_SWEEP")
    report.add_argument("--output", required=True, metavar="DIRECTORY")
    report.add_argument("--figures", action="store_true", help="also draw matplotlib figures")

    export = command("export", _export, "re-export a results directory")
    export.add_argument("results", metavar="RESULTS")
    export.add_argument("--to", required=True, choices=("csv", "parquet"))
    export.add_argument("--output", required=True, metavar="DIRECTORY")

    app = command("app", _app, "serve the local run and sweep API")
    app.add_argument("scenario", metavar="SCENARIO.json")
    app.add_argument("--port", type=int, default=8765)
    app.add_argument("--no-browser", action="store_true", help="print the URL without opening it")
    app.add_argument("--packet-backend", metavar="EXE", help="trusted ns-3 link replay binary")
    app.add_argument("--network-backend", metavar="EXE", help="trusted ns-3 network binary")
    app.add_argument(
        "--artifact-limit",
        type=float,
        default=None,
        metavar="MiB",
        help=f"disk limit for the app's results, in MiB (default {DEFAULT_ARTIFACT_LIMIT_BYTES // MIB})",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] in RETIRED:
        old = arguments[0]
        print(f"openleo {old} was removed in 2.0; use: openleo {RETIRED[old]}", file=sys.stderr)
        return 2
    args = _parser().parse_args(arguments)
    try:
        return args.handler(args)
    except NotImplementedError as exc:
        print(exc, file=sys.stderr)
        return 2
    except (ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


# ------------------------------------------------------------------------------ scenario


def _emit(text: str, output: str | None) -> None:
    if output is None:
        sys.stdout.write(text)
    else:
        Path(output).write_text(text, encoding="utf-8", newline="\n")


def _scenario_new(args) -> int:
    _emit(starter(args.preset), args.output)
    return 0


def _scenario_validate(args) -> int:
    scenario = load_scenario(args.scenario)
    note = ""
    catalogs = scenario.space_segment.catalogs
    if all(
        os.access(catalog.path, os.R_OK) and Path(catalog.path).is_file() for catalog in catalogs
    ):
        orbits = load_catalogs(scenario.space_segment, builtin_timescale())
        if scenario.time.events.enabled:
            check_step(scenario, orbits)
    else:
        note = " (catalog not checked: a catalog file is not readable, so the event step was not)"
    print(f"valid: {scenario.name}{note}")
    return 0


def _scenario_explain(args) -> int:
    sys.stdout.write(explain(load_scenario(args.scenario)) + "\n")
    return 0


def _scenario_upgrade(args) -> int:
    text = upgrade_file(Path(args.scenario), None if args.output is None else Path(args.output))
    if args.output is None:
        sys.stdout.write(text)
    return 0


# ----------------------------------------------------------------------------- run / sweep


def _run(args) -> int:
    scenario = load_scenario(args.scenario)
    formats = None if args.formats is None else _formats(args.formats)
    options: dict = {"workers": args.workers}
    if args.memory_budget is not None:
        budget = args.memory_budget
        if not math.isfinite(budget) or budget <= 0:
            raise ValueError(f"--memory-budget={budget!r} must be a positive number of GiB")
        options["memory_budget_bytes"] = int(budget * GIB)
    results = run_scenario(scenario, **options)
    directory = save_run(scenario, results, Path(args.output), formats)
    print(f"results: {directory}")
    print(_convergence_line(results.metadata["convergence"]))
    return 0


def _formats(text: str) -> tuple[str, ...]:
    formats = tuple(part.strip() for part in text.split(",") if part.strip())
    unknown = [name for name in formats if name not in FORMATS]
    if not formats or unknown:
        raise ValueError(
            f"--formats={text!r} must list formats from {', '.join(FORMATS)}"
            + (f"; unknown: {', '.join(unknown)}" if unknown else "")
        )
    return formats


def _convergence_line(block: dict) -> str:
    mode = block["settings"]["mode"]
    summary = block.get("summary")
    if summary is None:
        return f"convergence: 0 metrics, 0 failures (mode={mode})"
    line = f"convergence: {summary['metrics']} metrics, {summary['failed']} failures (mode={mode})"
    route = summary["classes"]["route"]
    if route["metrics"]:
        line += (
            f"; route: {route['metrics']} metrics, {route['failed']} failures (reported, not gated)"
        )
    return line


def _sweep(args) -> int:
    document = run_sweep(
        Path(args.sweep),
        Path(args.output),
        workers=args.workers,
        packet_backend=_path(args.packet_backend),
        network_backend=_path(args.network_backend),
    )
    print(f"sweep: {args.output}")
    print(f"runs: {len(document['runs'])}")
    return 0


def _path(text: str | None) -> Path | None:
    return None if text is None else Path(text)


# ------------------------------------------------------------------------------- replays


def _replay_link(args) -> int:
    config = ReplayConfig(
        station_name=args.station,
        norad_id=args.satellite,
        **{name: getattr(args, name) for name, _, _ in _PACKET_OPTIONS},
    )
    run_packet_replay(args.results, config, Path(args.backend), Path(args.output))
    print(f"packet summary: {Path(args.output) / 'packet-summary.json'}")
    return 0


def _replay_network(args) -> int:
    source = ReplaySource.from_results(Path(args.results))
    names = [
        f"{source.station_names[first]}:{source.station_names[second]}"
        for first, second in source.route_pairs
    ]
    if args.pair not in names:
        raise ValueError(
            f"--pair {args.pair!r} must name one of the results' network pairs as SRC:DST: "
            + (", ".join(names) if names else "these results have no network")
        )
    config = NetworkReplayConfig(
        routing_model=args.model,
        pair_index=names.index(args.pair),
        **{name: getattr(args, name) for name, _, _ in _PACKET_OPTIONS + _NETWORK_OPTIONS},
    )
    run_network_replay(args.results, config, Path(args.backend), Path(args.output))
    print(f"network summary: {Path(args.output) / 'network-summary.json'}")
    return 0


def _reproduce(args) -> int:
    evidence = run_reproduce(
        Path(args.config), Path(args.reference), Path(args.network_backend), Path(args.output)
    )
    print(f"native replays: {evidence['native_packet_replays']}")
    print(f"evidence: {Path(args.output) / 'release-evidence.json'}")
    return 0


# ------------------------------------------------------------------- verify / calc / report


def _verify_orbit(args) -> int:
    result = verify_orbit_reference(args.reference)
    print(json.dumps(result, indent=2, allow_nan=False))
    return 0 if result["passed"] else 1


def _verify_workbook(args) -> int:
    verify_itu_workbook(args.path)  # raises NotImplementedError until S3
    return 0  # pragma: no cover


def _calc_gases(args) -> int:
    result = run_gases_benchmark(load_gases_benchmark(args.config))
    csv_path, summary_path = write_gases_result(result, args.output)
    print(f"benchmark: {result.benchmark.name}")
    print(f"recommendation: {result.benchmark.recommendation}")
    print(f"method: {result.benchmark.method}")
    print(f"cases: {len(result.cases)}")
    print(f"csv: {csv_path}")
    print(f"summary: {summary_path}")
    return 0


def _calc_hydrometeors(args) -> int:
    result = run_hydrometeors(args.config, args.output)
    print(f"study: {result['name']}")
    print(f"report: {Path(args.output) / 'index.html'}")
    return 0


def _report(args) -> int:
    directory = write_report(Path(args.path), Path(args.output), figures=args.figures)
    print(f"report: {directory}")
    return 0


def _export(args) -> int:
    directory = export_results(Path(args.results), args.to, Path(args.output))
    print(f"results: {directory}")
    return 0


def _app(args) -> int:
    limit = DEFAULT_ARTIFACT_LIMIT_BYTES
    if args.artifact_limit is not None:
        if not math.isfinite(args.artifact_limit) or args.artifact_limit <= 0:
            raise ValueError(f"--artifact-limit={args.artifact_limit!r} must be a positive size")
        limit = int(args.artifact_limit * MIB)
    run_app(
        Path(args.scenario),
        port=args.port,
        open_browser=not args.no_browser,
        packet_backend=_path(args.packet_backend),
        network_backend=_path(args.network_backend),
        artifact_limit_bytes=limit,
    )
    return 0
