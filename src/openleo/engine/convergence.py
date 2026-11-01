"""The grid-convergence gate (design spec section 6.6).

After the primary run the engine recomputes every exported integrated metric on the grid refined
by ``refinement_factor`` (``openleo.engine.timegrid.refine``), with the same events and the same
evaluator, and ``compare_metrics`` compares the two values of each metric.

Metric ids name what they measure: ``pair/<station>/<norad>/<metric>``,
``station/<station>/<metric>`` and ``route/<source>-><target>/<model>/<metric>``. The absolute
tolerance of a metric is chosen by the end of its id (``absolute_tolerance``):

- ``_bits``: the scenario's ``absolute_tolerance_bits``;
- ``_fraction``: 1e-6;
- ``_s`` (durations and mean delays): 1e-3 s;
- counts (``pass_count``, ``*_handovers``, ``route_changes``): 0, and no relative tolerance:
  counts must match exactly;
- anything else (rates in bit/s): 0, so the relative tolerance alone decides.

A metric passes when ``|r - p| <= max(relative_tolerance * |r|, absolute)`` with p the primary
and r the refined value; 0 against 0 passes. A metric undefined on both grids (NaN, such as the
mean rate of a pair that is never visible) passes, and undefined on one grid only fails. The
relative difference is ``|r - p| / |r|``: 0 when both are 0, infinite when only r is.

``gate`` applies the scenario's mode: ``error`` raises ValueError naming up to ten failing
metrics and the suggested step ``step_s / refinement_factor``; ``warn`` returns one warning per
failing metric; ``off`` records ``{"skipped": true}``.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

import numpy as np

from openleo.results.model import BOOL_UNIT, Column, Table

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from openleo.scenario.model import ConvergenceSettings

FRACTION_TOLERANCE = 1e-6
DURATION_TOLERANCE_S = 1e-3
COUNT_METRICS = ("pass_count", "route_changes")
COUNT_SUFFIX = "_handovers"
MAX_LISTED_FAILURES = 10


@dataclass(frozen=True, slots=True)
class ConvergenceRow:
    metric: str
    primary: float
    refined: float
    absolute_difference: float
    relative_difference: float
    passed: bool


def _is_count(metric: str) -> bool:
    name = metric.rsplit("/", 1)[-1]
    return name in COUNT_METRICS or name.endswith(COUNT_SUFFIX)


def absolute_tolerance(metric: str, settings: ConvergenceSettings) -> float:
    """The absolute tolerance of a metric id, by its suffix (module docstring)."""
    if _is_count(metric):
        return 0.0
    if metric.endswith("_bits"):
        return float(settings.absolute_tolerance_bits)
    if metric.endswith("_fraction"):
        return FRACTION_TOLERANCE
    if metric.endswith("_s"):
        return DURATION_TOLERANCE_S
    return 0.0


def compare_metrics(
    primary: Mapping[str, float],
    refined: Mapping[str, float],
    settings: ConvergenceSettings,
) -> tuple[ConvergenceRow, ...]:
    """One row per metric, in the order of ``primary``; both must hold the same metric ids."""
    if set(primary) != set(refined):
        only_primary = sorted(set(primary) - set(refined))[:3]
        only_refined = sorted(set(refined) - set(primary))[:3]
        raise ValueError(
            f"metric sets differ between the grids: only primary {only_primary}, "
            f"only refined {only_refined}"
        )
    relative = float(settings.relative_tolerance)
    rows = []
    for metric, p in primary.items():
        p, r = float(p), float(refined[metric])
        absolute = absolute_tolerance(metric, settings)
        rel = 0.0 if _is_count(metric) else relative
        if math.isnan(p) or math.isnan(r):
            both = math.isnan(p) and math.isnan(r)
            rows.append(ConvergenceRow(metric, p, r, math.nan, math.nan, both))
            continue
        difference = abs(r - p)
        if difference == 0.0:
            relative_difference = 0.0
        elif r == 0.0:
            relative_difference = math.inf
        else:
            relative_difference = difference / abs(r)
        passed = difference <= max(rel * abs(r), absolute)
        rows.append(ConvergenceRow(metric, p, r, difference, relative_difference, passed))
    return tuple(rows)


def gate(
    rows: Sequence[ConvergenceRow], settings: ConvergenceSettings, step_s: float
) -> tuple[dict[str, Any], list[str]]:
    """The ``convergence`` block of ``results.json`` and the warnings, or ValueError (mode error).

    ``rows`` are ignored in mode ``off``, which records ``skipped``.
    """
    refined_step_s = step_s / settings.refinement_factor
    block: dict[str, Any] = {
        "settings": {
            **asdict(settings),
            "absolute_tolerances": {
                "_bits": float(settings.absolute_tolerance_bits),
                "_fraction": FRACTION_TOLERANCE,
                "_s": DURATION_TOLERANCE_S,
                "counts": 0.0,
                "other": 0.0,
            },
            "counts": [*COUNT_METRICS, "*" + COUNT_SUFFIX],
        },
        "skipped": settings.mode == "off",
    }
    if settings.mode == "off":
        return block, []
    failures = [row for row in rows if not row.passed]
    finite = [row.relative_difference for row in rows if math.isfinite(row.relative_difference)]
    infinite = any(row.relative_difference == math.inf for row in rows)
    block["summary"] = {
        "metrics": len(rows),
        "passed": len(rows) - len(failures),
        "failed": len(failures),
        # None (JSON null) when some refined value is 0 and its primary is not.
        "max_relative_difference": None if infinite else max(finite, default=0.0),
        "refined_step_s": refined_step_s,
    }
    block["failures"] = [_json_row(row) for row in failures]
    if failures and settings.mode == "error":
        listed = "; ".join(_describe(row) for row in failures[:MAX_LISTED_FAILURES])
        more = len(failures) - MAX_LISTED_FAILURES
        raise ValueError(
            f"convergence gate failed for {len(failures)} of {len(rows)} metrics under "
            f"{settings.refinement_factor}x grid refinement (relative tolerance "
            f"{settings.relative_tolerance!r}): {listed}"
            + (f"; and {more} more" if more > 0 else "")
            + f". The suggested /time/step_s is {refined_step_s!r} s (step_s / refinement_factor);"
            " /time/convergence/mode 'warn' publishes the results with these failures as warnings"
        )
    warnings = [f"convergence: {_describe(row)}" for row in failures]
    return block, warnings


def _describe(row: ConvergenceRow) -> str:
    return (
        f"{row.metric} is {row.primary!r} on the primary grid and {row.refined!r} on the refined "
        f"grid (relative difference {row.relative_difference!r})"
    )


def _json_number(value: float) -> float | None:
    return value if math.isfinite(value) else None


def _json_row(row: ConvergenceRow) -> dict[str, Any]:
    return {
        "metric": row.metric,
        "primary": _json_number(row.primary),
        "refined": _json_number(row.refined),
        "absolute_difference": _json_number(row.absolute_difference),
        "relative_difference": _json_number(row.relative_difference),
    }


_COLUMNS = (
    Column(
        "metric",
        "str",
        "",
        "Metric id: pair/<station>/<norad>/<metric>, "
        "station/<station>/<metric> or route/<source>-><target>/<model>/<metric>.",
    ),
    Column("primary", "f64", "metric unit", "Value on the scenario's grid, in the metric's unit."),
    Column("refined", "f64", "metric unit", "Value on the refined grid, in the metric's unit."),
    Column("absolute_difference", "f64", "metric unit", "|refined - primary|."),
    Column("relative_difference", "f64", "1", "|refined - primary| / |refined|."),
    Column("passed", "u8", BOOL_UNIT, "Whether the metric is within its tolerance."),
)


def convergence_table(rows: Sequence[ConvergenceRow]) -> Table:
    """The ``convergence`` table (spec section 7.3), one row per metric in the given order."""
    data = {
        "metric": tuple(row.metric for row in rows),
        "primary": np.array([row.primary for row in rows], dtype=np.float64),
        "refined": np.array([row.refined for row in rows], dtype=np.float64),
        "absolute_difference": np.array(
            [row.absolute_difference for row in rows], dtype=np.float64
        ),
        "relative_difference": np.array(
            [row.relative_difference for row in rows], dtype=np.float64
        ),
        "passed": np.array([row.passed for row in rows], dtype=np.uint8),
    }
    return Table("convergence", _COLUMNS, data)
