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
        description="Run a scenario, sensitivity study, or completed pass overview.",
        epilog=(
            "Examples:\n"
            "  openleo run SCENARIO.json --output DIRECTORY\n"
            "  openleo sensitivity SCENARIO.json SENSITIVITY.json --output DIRECTORY\n"
            "  openleo gases CONFIG.json --output DIRECTORY\n"
            "  openleo plot RUN_DIRECTORY --output FILE.svg\n"
            "  openleo plot-sensitivity RUN_DIRECTORY --output FILE.svg"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

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

    args = parser.parse_args(argv)
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
    return 2
