"""Command line entry point."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from openleo.gases import load_gases_benchmark, run_gases_benchmark, write_gases_result
from openleo.input import load_scenario
from openleo.output import write_result
from openleo.sensitivity import load_sensitivity_study, run_sensitivity, write_sensitivity_result
from openleo.simulation import simulate_scenario


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
    if args.command == "fidelity-study":
        try:
            from pathlib import Path

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

                run_app(scenario, port=args.port, open_browser=not args.no_browser)
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
