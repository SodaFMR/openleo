"""Command line entry point."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from openleo.input import load_scenario
from openleo.output import write_result
from openleo.simulation import simulate_scenario


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="openleo",
        description="Run a scenario or render a completed pass overview.",
        epilog=(
            "Examples:\n"
            "  openleo run SCENARIO.json --output DIRECTORY\n"
            "  openleo plot RUN_DIRECTORY --output FILE.svg"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="run a scenario and write its artifacts")
    run_parser.add_argument("scenario", metavar="SCENARIO.json", help="scenario input JSON")
    run_parser.add_argument("--output", required=True, metavar="DIRECTORY", help="output directory")

    plot_parser = subparsers.add_parser("plot", help="render a completed pass overview")
    plot_parser.add_argument(
        "run_directory", metavar="RUN_DIRECTORY", help="completed run directory"
    )
    plot_parser.add_argument(
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
    if args.command == "plot":
        try:
            from openleo.plotting import render_pass_overview

            output_path = render_pass_overview(args.run_directory, args.output)
            print(f"plot: {output_path}")
            return 0
        except (ValueError, OSError, UnicodeError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    return 2
