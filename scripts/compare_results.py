"""Compare the numeric tables of two OpenLEO results directories, or one against a digest.

Usage (from the repository root):

    python scripts/compare_results.py RESULTS OTHER_RESULTS
    python scripts/compare_results.py RESULTS DIGEST.json
    python scripts/compare_results.py RESULTS --write-digest DIGEST.json

The CI matrix runs the second form on every operating system against a digest written from an
Ubuntu run (``tests/fixtures/ci/coupled-ubuntu.json``), so that numerical tables agree across
platforms within the tolerances of design spec section 11 (spec section 7.5).

Two directories are compared element by element. A digest stores, per table, the row count and,
per column, the NaN and finite counts, the exactly rounded sum (``math.fsum``), the sum of magnitudes and the
extrema of the finite values; text columns store a SHA-256 of their values. Against a digest
the sums and extrema are checked with the bounds the element-wise tolerance implies:
``|Σa − Σb| ≤ n·atol + rtol·Σ|b|`` and ``|max a − max b| ≤ atol + rtol·max|b|`` (likewise the
minimum), so a digest check is a necessary condition of the element-wise check.

Tolerances, by the unit a column declares (``results.json``), with ``τ`` the event tolerance
``time.events.tolerance_s`` when events are enabled and 1e-9 s otherwise (grid times are the
same whole microseconds on every platform):

| unit | absolute | relative | source |
| --- | --- | --- | --- |
| deg | 1e-8 (apparent elevation 1e-9) | 0 | §11.1 angles |
| m | 1e-4 | 0 | §11.1 range and ECEF |
| m/s | 1e-6 | 0 | §11.1 range rate |
| Hz | 1e-3 | 0 | §11.1 Doppler |
| dB, dB-Hz, dBi, dB/km | 1e-9 | 0 | §11.1 dB quantities |
| s, delay columns | 1e-12 (excess delay 1e-13) | 0 | §11.1, range tolerance / c |
| s, other times and durations | 2τ | 0 | §6.1 event tolerance, two ends |
| bit | 1 | 1e-6 | §6.6 absolute bit tolerance |
| bit/s, rate_bps and bottleneck_bps | 0 | 0 | §11.1 rates exact |
| bit/s, other | 1e-3 | 1e-6 | Shannon bound, mean rates |
| 1 (fractions and other ratios) | 1e-6 | 1e-6 | §6.6 absolute fraction tolerance |
| d | 1e-9 | 0 | element age |
| metric unit (convergence table) | max(1, 2τ) | 1e-6 | the loosest metric class |
| other units | 0 | 1e-12 | declared inputs |

The convergence table repeats, under the unit ``metric unit``, metrics that their own tables
compare with their own tolerances, so its values get the loosest metric tolerance.

Integer, flag and text columns must be equal. Row counts and NaN positions must be equal.
Exit status 0 means agreement, 1 disagreement (each failure is printed), 2 a usage error.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from hashlib import sha256
from pathlib import Path

import numpy as np

from openleo.results import load_results

DIGEST_KIND = "openleo.results-digest"
DIGEST_SCHEMA = "1"
MAX_REPORTED = 40
GRID_TIME_TOLERANCE_S = 1e-9
_EXACT_RATES = frozenset(("rate_bps", "bottleneck_bps"))
_DB_UNITS = frozenset(("dB", "dB-Hz", "dBi", "dB/km"))


def event_tolerance_s(metadata: dict) -> float:
    """τ: the event tolerance of an events-on run, else the grid-time tolerance."""
    events = metadata["scenario"]["time"]["events"]
    if events["enabled"]:
        return float(events["tolerance_s"])
    return GRID_TIME_TOLERANCE_S


def tolerance(name: str, unit: str, tau: float) -> tuple[float, float]:
    """(absolute, relative) tolerance of a floating-point column."""
    if unit == "deg":
        return (1e-9 if name == "apparent_elevation_deg" else 1e-8), 0.0
    if unit == "m":
        return 1e-4, 0.0
    if unit == "m/s":
        return 1e-6, 0.0
    if unit == "Hz":
        return 1e-3, 0.0
    if unit in _DB_UNITS:
        return 1e-9, 0.0
    if unit == "s":
        if name == "atmospheric_excess_delay_s":
            return 1e-13, 0.0
        if "delay" in name:
            return 1e-12, 0.0
        return 2.0 * tau, 0.0
    if unit == "bit":
        return 1.0, 1e-6
    if unit == "bit/s":
        return (0.0, 0.0) if name in _EXACT_RATES else (1e-3, 1e-6)
    if unit == "1":
        return 1e-6, 1e-6
    if unit == "d":
        return 1e-9, 0.0
    if unit == "metric unit":
        return max(1.0, 2.0 * tau), 1e-6
    return 0.0, 1e-12


def _text_sha256(values) -> str:
    return sha256("\x1f".join(values).encode("utf-8")).hexdigest()


def _column_digest(column, values) -> dict:
    if column.dtype == "str":
        return {"dtype": "str", "unit": column.unit, "sha256": _text_sha256(values)}
    array = np.asarray(values, dtype=np.float64)
    finite = array[np.isfinite(array)]
    return {
        "dtype": column.dtype,
        "unit": column.unit,
        "nan": int(np.isnan(array).sum()),
        "finite": int(finite.size),
        "sum": math.fsum(finite.tolist()),
        "abs_sum": math.fsum(np.abs(finite).tolist()),
        "min": float(finite.min()) if finite.size else None,
        "max": float(finite.max()) if finite.size else None,
    }


def digest(directory: Path) -> dict:
    """The digest of a results directory (see the module docstring)."""
    results = load_results(directory)
    metadata = results.metadata
    tables = {}
    for name in sorted(results.tables):
        table = results.tables[name]
        tables[name] = {
            "rows": table.rows,
            "column_order": [column.name for column in table.columns],
            "columns": {
                column.name: _column_digest(column, table[column.name]) for column in table.columns
            },
        }
    return {
        "kind": DIGEST_KIND,
        "schema_version": DIGEST_SCHEMA,
        "scenario_name": metadata["scenario"]["name"],
        "scenario_sha256": metadata["scenario"]["normalised_sha256"],
        "event_tolerance_s": event_tolerance_s(metadata),
        "software": metadata["software"],
        "tables": tables,
    }


def _close(actual: float, expected: float, atol: float, rtol: float) -> bool:
    return abs(actual - expected) <= atol + rtol * abs(expected)


def compare_digests(actual: dict, expected: dict) -> list[str]:
    """Failures of ``actual`` against ``expected`` (both digests), as readable lines."""
    failures = []
    if actual["scenario_sha256"] != expected["scenario_sha256"]:
        failures.append("scenario: the normalised scenario differs from the digest's")
    tau = expected["event_tolerance_s"]
    if set(actual["tables"]) != set(expected["tables"]):
        failures.append(
            f"tables: {sorted(actual['tables'])} != expected {sorted(expected['tables'])}"
        )
    for name in sorted(set(actual["tables"]) & set(expected["tables"])):
        mine, theirs = actual["tables"][name], expected["tables"][name]
        if mine["rows"] != theirs["rows"]:
            failures.append(f"{name}: {mine['rows']} rows, expected {theirs['rows']}")
            continue
        if mine["column_order"] != theirs["column_order"]:
            failures.append(f"{name}: columns {mine['column_order']} differ from the digest's")
            continue
        for column in theirs["column_order"]:
            expected_column = theirs["columns"][column]
            failures += _compare_column_digest(
                f"{name}.{column}", column, mine["columns"][column], expected_column, tau
            )
    return failures


def _compare_column_digest(where: str, name: str, mine: dict, theirs: dict, tau: float):
    if mine["dtype"] != theirs["dtype"] or mine["unit"] != theirs["unit"]:
        return [f"{where}: dtype/unit {mine['dtype']}/{mine['unit']} != digest's"]
    if theirs["dtype"] == "str":
        return [] if mine["sha256"] == theirs["sha256"] else [f"{where}: text values differ"]
    if mine["nan"] != theirs["nan"]:
        return [f"{where}: {mine['nan']} NaN values, expected {theirs['nan']}"]
    if (mine["min"] is None) != (theirs["min"] is None):
        return [f"{where}: finite values present in only one run"]
    if theirs["min"] is None:
        return []
    if theirs["dtype"] == "f64":
        atol, rtol = tolerance(name, theirs["unit"], tau)
    else:
        atol, rtol = 0.0, 0.0
    failures = []
    sum_bound = theirs["finite"] * atol + rtol * theirs["abs_sum"]
    if abs(mine["sum"] - theirs["sum"]) > sum_bound:
        failures.append(
            f"{where}: sum {mine['sum']!r} differs from {theirs['sum']!r} by more than "
            f"{sum_bound:.3g}"
        )
    for key in ("min", "max"):
        if not _close(mine[key], theirs[key], atol, rtol):
            failures.append(
                f"{where}: {key} {mine[key]!r} differs from {theirs[key]!r} by more than "
                f"{atol:g} + {rtol:g} relative"
            )
    return failures


def compare_directories(first: Path, second: Path) -> list[str]:
    """Element-wise failures of ``first`` against ``second`` (two results directories)."""
    actual, expected = load_results(first), load_results(second)
    failures = []
    if (
        actual.metadata["scenario"]["normalised_sha256"]
        != (expected.metadata["scenario"]["normalised_sha256"])
    ):
        failures.append("scenario: the two runs have different normalised scenarios")
    tau = event_tolerance_s(expected.metadata)
    if set(actual.tables) != set(expected.tables):
        failures.append(f"tables: {sorted(actual.tables)} != {sorted(expected.tables)}")
    for name in sorted(set(actual.tables) & set(expected.tables)):
        mine, theirs = actual.tables[name], expected.tables[name]
        if mine.rows != theirs.rows:
            failures.append(f"{name}: {mine.rows} rows, expected {theirs.rows}")
            continue
        if [c.name for c in mine.columns] != [c.name for c in theirs.columns]:
            failures.append(f"{name}: the column lists differ")
            continue
        for column in theirs.columns:
            where = f"{name}.{column.name}"
            if column.dtype == "str":
                if tuple(mine[column.name]) != tuple(theirs[column.name]):
                    failures.append(f"{where}: text values differ")
                continue
            a = np.asarray(mine[column.name], dtype=np.float64)
            b = np.asarray(theirs[column.name], dtype=np.float64)
            if not np.array_equal(np.isnan(a), np.isnan(b)):
                failures.append(f"{where}: NaN positions differ")
                continue
            atol, rtol = (
                tolerance(column.name, column.unit, tau) if column.dtype == "f64" else (0.0, 0.0)
            )
            mask = ~np.isnan(b)
            excess = np.abs(a[mask] - b[mask]) - (atol + rtol * np.abs(b[mask]))
            if excess.size and excess.max() > 0.0:
                row = int(np.flatnonzero(mask)[int(np.argmax(excess))])
                failures.append(
                    f"{where}: row {row}: {float(a[row])!r} differs from {float(b[row])!r} by more than "
                    f"{atol:g} + {rtol:g} relative"
                )
    return failures


def _load_digest(path: Path) -> dict:
    document = json.loads(path.read_text(encoding="utf-8"))
    if document.get("kind") != DIGEST_KIND or document.get("schema_version") != DIGEST_SCHEMA:
        raise ValueError(f"{path}: not an {DIGEST_KIND} schema {DIGEST_SCHEMA} document")
    return document


def write_digest(directory: Path, path: Path) -> None:
    """Write the digest of ``directory`` to ``path`` (sorted keys, LF, trailing newline)."""
    text = json.dumps(digest(directory), indent=1, sort_keys=True, ensure_ascii=False)
    path.write_text(text + "\n", encoding="utf-8", newline="\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("results", type=Path, help="a results directory")
    parser.add_argument("reference", type=Path, nargs="?", help="a results directory or digest")
    parser.add_argument("--write-digest", type=Path, metavar="DIGEST.json")
    args = parser.parse_args(argv)
    if (args.reference is None) == (args.write_digest is None):
        parser.error("give either a reference (directory or digest) or --write-digest")
    try:
        if args.write_digest is not None:
            write_digest(args.results, args.write_digest)
            print(f"wrote {args.write_digest}")
            return 0
        if args.reference.is_dir():
            failures = compare_directories(args.results, args.reference)
        else:
            failures = compare_digests(digest(args.results), _load_digest(args.reference))
    except (OSError, ValueError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    for line in failures[:MAX_REPORTED]:
        print(line)
    if len(failures) > MAX_REPORTED:
        print(f"... and {len(failures) - MAX_REPORTED} more")
    if failures:
        print(f"{len(failures)} disagreement(s) with {args.reference}")
        return 1
    print(f"{args.results} agrees with {args.reference} within the spec section 11 tolerances")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
