"""Command line entry point."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import fields
from pathlib import Path

from openleo.gases import load_gases_benchmark, run_gases_benchmark, write_gases_result
from openleo.input import load_scenario
from openleo.output import write_result
from openleo.sensitivity import load_sensitivity_study, run_sensitivity, write_sensitivity_result
from openleo.simulation import simulate_scenario

_TRAFFIC_FIELDS = (
    ("start_offset_s", float, 0),
    ("duration_s", float, 60),
    ("offered_load_bps", float, 100_000),
    ("packet_size_bytes", int, 512),
    ("queue_packets", int, 32),
    ("flows_per_direction", int, 1),
    ("acquisition_delay_s", float, 0),
    ("seed", int, 1),
)


def _traffic_arguments(parser, *, use_defaults):
    for name, value_type, default in _TRAFFIC_FIELDS:
        parser.add_argument(
            "--" + name.replace("_", "-"),
            type=value_type,
            default=default if use_defaults else None,
        )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="openleo",
        description="Run OpenLEO scientific benchmarks and render completed artifacts.",
        epilog=(
            "Examples:\n"
            "  openleo app CONSTELLATION.json\n"
            "  openleo constellation CONSTELLATION.json --output DIRECTORY\n"
            "  openleo propagation-study CONSTELLATION.json --output DIRECTORY\n"
            "  openleo fidelity-study STUDY.json --output DIRECTORY\n"
            "  openleo hydrometeors CONFIG.json --output DIRECTORY\n"
            "  openleo experiment CONSTELLATION.json --settings SETTINGS.json --output DIRECTORY\n"
            "  openleo network-replay RUN_DIRECTORY --backend EXECUTABLE --output DIRECTORY\n"
            "  openleo run SCENARIO.json --output DIRECTORY\n"
            "  openleo sensitivity SCENARIO.json SENSITIVITY.json --output DIRECTORY\n"
            "  openleo gases CONFIG.json --output DIRECTORY\n"
            "  openleo plot RUN_DIRECTORY --output FILE.svg\n"
            "  openleo plot-sensitivity RUN_DIRECTORY --output FILE.svg\n"
            "  openleo plot-gases RUN_DIRECTORY --output FILE.svg"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    app_parser = subparsers.add_parser("app", help="open the local scientific workbench")
    app_parser.add_argument("scenario", metavar="CONSTELLATION.json")
    app_parser.add_argument("--port", type=int, default=8765)
    app_parser.add_argument("--packet-backend", help="trusted local single-link ns-3 executable")
    app_parser.add_argument("--network-backend", help="trusted local multi-hop ns-3 executable")
    app_parser.add_argument(
        "--no-browser", action="store_true", help="print the local URL without opening it"
    )
    constellation_parser = subparsers.add_parser(
        "constellation", help="run a constellation experiment and export its interactive report"
    )
    constellation_parser.add_argument("scenario", metavar="CONSTELLATION.json")
    constellation_parser.add_argument("--output", required=True, metavar="DIRECTORY")

    propagation_parser = subparsers.add_parser(
        "propagation-study", help="compare free space, reference atmosphere, and a refined grid"
    )
    propagation_parser.add_argument("scenario", metavar="CONSTELLATION.json")
    propagation_parser.add_argument("--output", required=True, metavar="DIRECTORY")
    propagation_parser.add_argument(
        "--figure", metavar="FILE.svg|FILE.png", help="optional figure; requires the plot extra"
    )

    fidelity_parser = subparsers.add_parser(
        "fidelity-study", help="compare declared constellation scenarios and sampling grids"
    )
    fidelity_parser.add_argument("study", metavar="STUDY.json")
    fidelity_parser.add_argument("--output", required=True, metavar="DIRECTORY")

    hydro_parser = subparsers.add_parser(
        "hydrometeors", help="calculate declared rain and cloud attenuation cases"
    )
    hydro_parser.add_argument("configuration", metavar="CONFIG.json")
    hydro_parser.add_argument("--output", required=True, metavar="DIRECTORY")

    packet_parser = subparsers.add_parser(
        "packet-replay", help="replay one verified ground link through an optional ns-3 backend"
    )
    packet_parser.add_argument("bundle", metavar="RUN_DIRECTORY")
    packet_parser.add_argument("--station", required=True, help="exact station name")
    packet_parser.add_argument(
        "--norad", required=True, type=int, help="satellite catalog identifier"
    )
    packet_parser.add_argument(
        "--backend", required=True, help="trusted compiled OpenLEO ns-3 replay executable"
    )
    packet_parser.add_argument("--output", required=True, metavar="DIRECTORY")
    packet_parser.add_argument("--start-offset-s", type=float, default=0)
    packet_parser.add_argument("--duration-s", type=float, default=60)
    packet_parser.add_argument("--offered-load-bps", type=float, default=100_000)
    packet_parser.add_argument("--packet-size-bytes", type=int, default=512)
    packet_parser.add_argument("--queue-packets", type=int, default=32)
    packet_parser.add_argument("--seed", type=int, default=1)

    network_parser = subparsers.add_parser(
        "network-replay", help="replay multi-hop UDP routes with native ns-3"
    )
    network_parser.add_argument("bundle", metavar="RUN_DIRECTORY")
    network_parser.add_argument(
        "--backend", required=True, help="trusted compiled network-replay executable"
    )
    network_parser.add_argument("--output", required=True, metavar="DIRECTORY")
    network_parser.add_argument(
        "--routing-model",
        choices=("minimum_delay", "maximum_rate", "fixed_capacity"),
        default="minimum_delay",
    )
    _traffic_arguments(network_parser, use_defaults=True)

    experiment_parser = subparsers.add_parser(
        "experiment", help="compare aligned propagation and optional packet experiments"
    )
    experiment_parser.add_argument("scenario", metavar="CONSTELLATION.json")
    experiment_parser.add_argument("--settings", metavar="SETTINGS.json")
    experiment_parser.add_argument("--output", required=True, metavar="DIRECTORY")
    experiment_parser.add_argument("--packet-mode", choices=("none", "link", "network"))
    experiment_parser.add_argument("--station", dest="station_name")
    experiment_parser.add_argument("--norad", dest="norad_id", type=int)
    experiment_parser.add_argument("--packet-backend")
    experiment_parser.add_argument("--network-backend")
    _traffic_arguments(experiment_parser, use_defaults=False)

    benchmark_parser = subparsers.add_parser(
        "benchmark", help="run the frozen multi-scenario research matrix"
    )
    benchmark_parser.add_argument("configuration", metavar="BENCHMARK.json")
    benchmark_parser.add_argument("--output", required=True, metavar="DIRECTORY")
    benchmark_parser.add_argument("--network-backend")
    verification_parser = subparsers.add_parser(
        "verify-orbit", help="check a published numerical TEME reference"
    )
    verification_parser.add_argument("configuration", metavar="REFERENCE.json")
    reproduction_parser = subparsers.add_parser(
        "reproduce", help="reproduce complete numerical and native packet evidence"
    )
    reproduction_parser.add_argument("configuration", metavar="BENCHMARK.json")
    reproduction_parser.add_argument("--reference", required=True, metavar="REFERENCE.json")
    reproduction_parser.add_argument("--network-backend", required=True)
    reproduction_parser.add_argument("--output", required=True, metavar="DIRECTORY")

    run_parser = subparsers.add_parser("run", help="run a scenario and write its artifacts")
    run_parser.add_argument("scenario", metavar="SCENARIO.json", help="scenario input JSON")
    run_parser.add_argument("--output", required=True, metavar="DIRECTORY", help="output directory")

    sensitivity_parser = subparsers.add_parser(
        "sensitivity", help="run a deterministic sensitivity study"
    )
    sensitivity_parser.add_argument("scenario", metavar="SCENARIO.json", help="scenario input JSON")
    sensitivity_parser.add_argument(
        "sensitivity", metavar="SENSITIVITY.json", help="sensitivity study JSON"
    )
    sensitivity_parser.add_argument(
        "--output", required=True, metavar="DIRECTORY", help="output directory"
    )

    gases_parser = subparsers.add_parser(
        "gases", help="run a gaseous specific-attenuation benchmark"
    )
    gases_parser.add_argument("benchmark", metavar="CONFIG.json", help="benchmark input JSON")
    gases_parser.add_argument(
        "--output", required=True, metavar="DIRECTORY", help="output directory"
    )

    plot_parser = subparsers.add_parser("plot", help="render a completed pass overview")
    plot_parser.add_argument(
        "run_directory", metavar="RUN_DIRECTORY", help="completed run directory"
    )
    plot_parser.add_argument(
        "--output", required=True, metavar="FILE.svg|FILE.png", help="plot output file"
    )

    sensitivity_plot_parser = subparsers.add_parser(
        "plot-sensitivity", help="render a completed sensitivity overview"
    )
    sensitivity_plot_parser.add_argument(
        "run_directory", metavar="RUN_DIRECTORY", help="completed sensitivity run directory"
    )
    sensitivity_plot_parser.add_argument(
        "--output", required=True, metavar="FILE.svg|FILE.png", help="plot output file"
    )

    gases_plot_parser = subparsers.add_parser(
        "plot-gases", help="render a completed gaseous attenuation overview"
    )
    gases_plot_parser.add_argument(
        "run_directory", metavar="RUN_DIRECTORY", help="completed gaseous benchmark directory"
    )
    gases_plot_parser.add_argument(
        "--output", required=True, metavar="FILE.svg|FILE.png", help="plot output file"
    )

    args = parser.parse_args(argv)
    if args.command in ("benchmark", "verify-orbit", "reproduce"):
        try:
            if args.command == "verify-orbit":
                from openleo.orbit_verification import verify_orbit_reference

                result = verify_orbit_reference(args.configuration)
                print(json.dumps(result, indent=2, allow_nan=False))
                return 0 if result["passed"] else 1
            if args.command == "benchmark":
                from openleo.benchmark import run_benchmark

                result = run_benchmark(
                    args.configuration, args.output, network_backend=args.network_backend
                )
                print(f"packet evidence: {result['packet_evidence']}")
            else:
                from openleo.release_evidence import reproduce_release

                result = reproduce_release(
                    args.configuration,
                    args.reference,
                    args.output,
                    network_backend=args.network_backend,
                )
                print(f"native replays: {result['native_packet_replays']}")
            print(f"report: {Path(args.output) / 'index.html'}")
            return 0
        except (ValueError, OSError, UnicodeError, RecursionError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    if args.command == "experiment":
        try:
            from openleo.constellation import load_constellation
            from openleo.experiment import ExperimentConfig, parse_experiment_config, run_experiment
            from openleo.workbench import _read, _unique_object

            settings = (
                json.loads(_read(Path(args.settings), 1_000_000), object_pairs_hook=_unique_object)
                if args.settings
                else {}
            )
            if not isinstance(settings, dict):
                raise ValueError("experiment settings must be an object")  # noqa: TRY004
            overrides = {
                field.name: getattr(args, field.name)
                for field in fields(ExperimentConfig)
                if getattr(args, field.name, None) is not None
            }
            config = parse_experiment_config({**settings, **overrides})
            run_experiment(
                load_constellation(args.scenario),
                config,
                args.output,
                packet_backend=args.packet_backend,
                network_backend=args.network_backend,
            )
            print(f"report: {Path(args.output) / 'index.html'}")
            return 0
        except (ValueError, OSError, UnicodeError, RecursionError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    if args.command == "network-replay":
        try:
            from openleo.network_replay import NetworkReplayConfig, run_network_replay

            config = NetworkReplayConfig(
                routing_model=args.routing_model,
                **{name: getattr(args, name) for name, _, _ in _TRAFFIC_FIELDS},
            )
            run_network_replay(args.bundle, config, args.backend, args.output)
            print(f"network summary: {Path(args.output) / 'network-summary.json'}")
            return 0
        except (ValueError, OSError, UnicodeError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    if args.command == "hydrometeors":
        try:
            from openleo.hydrometeor_benchmark import run_hydrometeors

            result = run_hydrometeors(args.configuration, args.output)
            print(f"study: {result['name']}")
            print(f"report: {Path(args.output) / 'index.html'}")
            return 0
        except (ValueError, OSError, UnicodeError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    if args.command == "packet-replay":
        try:
            from openleo.packet_replay import ReplayConfig, run_packet_replay

            config = ReplayConfig(
                station_name=args.station,
                norad_id=args.norad,
                start_offset_s=args.start_offset_s,
                duration_s=args.duration_s,
                offered_load_bps=args.offered_load_bps,
                packet_size_bytes=args.packet_size_bytes,
                queue_packets=args.queue_packets,
                seed=args.seed,
            )
            run_packet_replay(args.bundle, config, args.backend, args.output)
            print(f"packet summary: {Path(args.output) / 'packet-summary.json'}")
            return 0
        except (ValueError, OSError, UnicodeError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    if args.command == "fidelity-study":
        try:
            from openleo.fidelity_study import load_fidelity_study, run_fidelity_study

            summary = run_fidelity_study(load_fidelity_study(args.study), args.output)
            print(f"study: {summary['name']}")
            print(f"runs: {len(summary['runs'])}")
            print(f"report: {Path(args.output) / 'index.html'}")
            return 0
        except (ValueError, OSError, UnicodeError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    if args.command == "propagation-study":
        try:
            from openleo.constellation import load_constellation
            from openleo.propagation_study import (
                render_propagation_study,
                run_propagation_study,
                write_propagation_study,
            )

            result = run_propagation_study(load_constellation(args.scenario))
            path = write_propagation_study(result, args.output)
            print(f"study: {path}")
            if args.figure:
                print(f"figure: {render_propagation_study(result, args.figure)}")
            return 0
        except (ValueError, OSError, UnicodeError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    if args.command in {"app", "constellation"}:
        try:
            from openleo.constellation import load_constellation, simulate_constellation

            scenario = load_constellation(args.scenario)
            if args.command == "app":
                from openleo.app import run_app

                backends = {
                    name: getattr(args, name)
                    for name in ("packet_backend", "network_backend")
                    if getattr(args, name) is not None
                }
                run_app(scenario, port=args.port, open_browser=not args.no_browser, **backends)
            else:
                from openleo.network import add_network
                from openleo.workbench import write_workbench

                document = add_network(simulate_constellation(scenario))
                paths = write_workbench(document, args.output)
                print(f"experiment: {scenario.name}")
                print(f"satellites: {len(document['satellites'])}")
                print(f"stations: {len(document['stations'])}")
                print(f"samples: {len(document['timestamps_utc'])}")
                for path in paths:
                    print(f"{path.name}: {path}")
            return 0
        except (ValueError, OSError, UnicodeError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    if args.command == "run":
        try:
            result = simulate_scenario(load_scenario(args.scenario))
            trace_path, summary_path = write_result(result, args.output)
            print(f"scenario: {result.scenario.name}")
            print(f"rows: {len(result.rows)}")
            print(
                f"sampled AOS: {result.summary.sampled_aos_utc.isoformat().replace('+00:00', 'Z')}"
            )
            print(
                f"sampled LOS: {result.summary.sampled_los_utc.isoformat().replace('+00:00', 'Z')}"
            )
            print(f"trace: {trace_path}")
            print(f"summary: {summary_path}")
            return 0
        except (ValueError, OSError, UnicodeError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    if args.command == "sensitivity":
        try:
            scenario = load_scenario(args.scenario)
            study = load_sensitivity_study(args.sensitivity, scenario)
            result = run_sensitivity(scenario, study)
            csv_path, summary_path = write_sensitivity_result(result, args.output)
            print(f"study: {study.name}")
            print(f"method: {study.method}")
            print(f"sweeps: {len(study.sweeps)}")
            print(f"cases: {len(result.cases)}")
            print(f"sensitivity: {csv_path}")
            print(f"summary: {summary_path}")
            return 0
        except (ValueError, OSError, UnicodeError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    if args.command == "gases":
        try:
            result = run_gases_benchmark(load_gases_benchmark(args.benchmark))
            csv_path, summary_path = write_gases_result(result, args.output)
            print(f"benchmark: {result.benchmark.name}")
            print(f"recommendation: {result.benchmark.recommendation}")
            print(f"method: {result.benchmark.method}")
            print(f"cases: {len(result.cases)}")
            print(f"csv: {csv_path}")
            print(f"summary: {summary_path}")
            return 0
        except (ValueError, OSError, UnicodeError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    if args.command == "plot":
        try:
            from openleo.plotting import render_pass_overview

            output_path = render_pass_overview(args.run_directory, args.output)
            print(f"plot: {output_path}")
            return 0
        except (ValueError, OSError, UnicodeError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    if args.command == "plot-sensitivity":
        try:
            from openleo.sensitivity_plotting import render_sensitivity_overview

            output_path = render_sensitivity_overview(args.run_directory, args.output)
            print(f"plot: {output_path}")
            return 0
        except (ValueError, OSError, UnicodeError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    if args.command == "plot-gases":
        try:
            from openleo.gases_plotting import render_gases_overview

            output_path = render_gases_overview(args.run_directory, args.output)
            print(f"plot: {output_path}")
            return 0
        except (ValueError, OSError, UnicodeError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    return 2
