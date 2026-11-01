"""The grid-convergence comparison and gate (design spec section 6.6)."""

import math

import numpy as np
import pytest

from openleo.engine.convergence import (
    ConvergenceRow,
    absolute_tolerance,
    compare_metrics,
    convergence_table,
    gate,
    is_gated,
    metric_class,
)
from openleo.scenario.model import ConvergenceSettings

SETTINGS = ConvergenceSettings(
    mode="error", refinement_factor=4, relative_tolerance=0.01, absolute_tolerance_bits=2.0
)


@pytest.mark.parametrize(
    ("metric", "expected"),
    [
        ("pair/Madrid/41917/integrated_rate_bits", 2.0),
        ("station/Madrid/visible_fraction", 1e-6),
        ("pair/Madrid/41917/visible_duration_s", 1e-3),
        ("route/Madrid->Tromso/minimum_delay/mean_delay_connected_s", 1e-6),
        ("pair/Madrid/41917/pass_count", 0.0),
        ("station/Madrid/best_link_handovers", 0.0),
        ("route/Madrid->Tromso/maximum_rate/route_changes", 0.0),
        ("pair/Madrid/41917/mean_rate_visible_bps", 0.0),
        ("station/Madrid/best_link_mean_rate_bps", 0.0),
    ],
)
def test_absolute_tolerance_by_suffix(metric, expected):
    assert absolute_tolerance(metric, SETTINGS) == expected


def test_counts_must_match_exactly():
    rows = compare_metrics({"pair/A/1/pass_count": 100.0}, {"pair/A/1/pass_count": 101.0}, SETTINGS)
    # 1 % of 101 would accept the difference; counts have a zero tolerance and no relative one.
    assert rows[0].passed is False


def test_pass_condition_uses_the_larger_of_relative_and_absolute():
    primary = {
        "pair/A/1/integrated_rate_bits": 1.0e6,
        "pair/A/2/integrated_rate_bits": 10.0,
        "pair/A/3/integrated_rate_bits": 1.0e6,
        "pair/A/1/mean_rate_visible_bps": 100.0,
        "station/A/visible_fraction": 0.5,
        "pair/A/1/visible_duration_s": 600.0,
    }
    refined = {
        "pair/A/1/integrated_rate_bits": 1.0e6 + 1.0e4,  # 0.99 % of the refined value
        "pair/A/2/integrated_rate_bits": 12.0,  # 2 bits: the absolute tolerance
        "pair/A/3/integrated_rate_bits": 1.0e6 + 1.1e4,  # 1.09 % of the refined value
        "pair/A/1/mean_rate_visible_bps": 101.5,  # 1.48 %: relative only
        "station/A/visible_fraction": 0.5 + 9e-7,
        "pair/A/1/visible_duration_s": 600.0 + 0.002,
    }
    rows = {row.metric: row for row in compare_metrics(primary, refined, SETTINGS)}
    assert rows["pair/A/1/integrated_rate_bits"].passed
    assert rows["pair/A/2/integrated_rate_bits"].passed
    assert not rows["pair/A/3/integrated_rate_bits"].passed
    assert not rows["pair/A/1/mean_rate_visible_bps"].passed
    assert rows["station/A/visible_fraction"].passed  # 9e-7 <= max(5.0e-3, 1e-6)
    assert rows["pair/A/1/visible_duration_s"].passed  # 0.002 <= 6.0
    row = rows["pair/A/3/integrated_rate_bits"]
    assert row.absolute_difference == pytest.approx(1.1e4)
    assert row.relative_difference == pytest.approx(1.1e4 / (1.0e6 + 1.1e4))


def test_zero_against_zero_and_undefined_against_undefined_pass():
    rows = compare_metrics(
        {"pair/A/1/integrated_rate_bits": 0.0, "pair/A/1/mean_rate_visible_bps": math.nan},
        {"pair/A/1/integrated_rate_bits": 0.0, "pair/A/1/mean_rate_visible_bps": math.nan},
        SETTINGS,
    )
    assert all(row.passed for row in rows)
    assert rows[0].relative_difference == 0.0
    assert math.isnan(rows[1].relative_difference)


def test_undefined_against_a_number_fails_and_zero_reference_is_infinite_relative():
    rows = compare_metrics(
        {"pair/A/1/mean_rate_visible_bps": math.nan, "pair/A/1/pass_count": 1.0},
        {"pair/A/1/mean_rate_visible_bps": 5.0, "pair/A/1/pass_count": 0.0},
        SETTINGS,
    )
    assert [row.passed for row in rows] == [False, False]
    assert rows[1].relative_difference == math.inf


def test_metric_sets_must_agree():
    with pytest.raises(ValueError, match="metric sets differ"):
        compare_metrics({"pair/A/1/pass_count": 1.0}, {"pair/A/2/pass_count": 1.0}, SETTINGS)


def _failing(count):
    return tuple(
        ConvergenceRow(f"pair/A/{index}/integrated_rate_bits", 1.0, 2.0e3, 1999.0, 0.9995, False)
        for index in range(count)
    ) + (ConvergenceRow("pair/A/x/pass_count", 1.0, 1.0, 0.0, 0.0, True),)


def test_error_mode_lists_ten_failures_and_suggests_a_step():
    with pytest.raises(ValueError) as caught:
        gate(_failing(12), SETTINGS, step_s=60.0)
    message = str(caught.value)
    assert "12 of 13" in message
    assert "pair/A/9/integrated_rate_bits" in message
    assert "pair/A/10/integrated_rate_bits" not in message
    assert "15.0 s" in message  # 60 s / refinement factor 4


def test_warn_mode_lists_every_failure_in_warnings():
    settings = ConvergenceSettings("warn", 4, 0.01, 2.0)
    block, warnings = gate(_failing(12), settings, step_s=60.0)
    assert block["skipped"] is False
    assert block["summary"]["metrics"] == 13
    assert block["summary"]["failed"] == 12
    assert len(block["failures"]) == 12
    assert len(warnings) == 12
    assert all(w.startswith("convergence: pair/A/") for w in warnings)


def test_passing_gate_and_off_mode():
    rows = (ConvergenceRow("pair/A/1/pass_count", 1.0, 1.0, 0.0, 0.0, True),)
    block, warnings = gate(rows, SETTINGS, step_s=60.0)
    assert warnings == []
    assert block["summary"] == {
        "metrics": 1,
        "passed": 1,
        "failed": 0,
        "max_relative_difference": 0.0,
        "refined_step_s": 15.0,
        "fatal": 0,
        "classes": {
            "link": {"gated": True, "metrics": 1, "failed": 0},
            "station": {"gated": True, "metrics": 0, "failed": 0},
            "route": {"gated": False, "metrics": 0, "failed": 0},
        },
    }
    off = ConvergenceSettings("off", 4, 0.01, 2.0)
    block, warnings = gate((), off, step_s=60.0)
    assert block["skipped"] is True
    assert warnings == []
    assert block["settings"]["mode"] == "off"


def test_convergence_table_has_units_and_flags():
    rows = _failing(1)
    table = convergence_table(rows)
    assert table.name == "convergence"
    assert [c.name for c in table.columns] == [
        "metric",
        "class",
        "primary",
        "refined",
        "absolute_difference",
        "relative_difference",
        "passed",
        "gated",
    ]
    assert table["passed"].tolist() == [0, 1]
    assert table["class"] == ("link", "link")
    assert table["gated"].tolist() == [1, 1]
    assert table["metric"] == tuple(row.metric for row in rows)
    assert np.array_equal(table["refined"], [2.0e3, 1.0])
    empty = convergence_table(())
    assert empty.rows == 0


def test_delay_metrics_get_a_microsecond_not_a_millisecond():
    # 0.5 ms on a 20 ms mean delay is 2.5 %: inside the 1 ms duration tolerance, outside 1 %.
    rows = compare_metrics(
        {"route/A->B/minimum_delay/mean_delay_connected_s": 0.0200},
        {"route/A->B/minimum_delay/mean_delay_connected_s": 0.0205},
        SETTINGS,
    )
    assert rows[0].passed is False
    rows = compare_metrics(
        {"route/A->B/minimum_delay/mean_delay_connected_s": 0.0200},
        {"route/A->B/minimum_delay/mean_delay_connected_s": 0.0201},  # 0.5 %: relative passes
        SETTINGS,
    )
    assert rows[0].passed is True
    assert absolute_tolerance("pair/A/1/visible_duration_s", SETTINGS) == 1e-3


def test_metric_classes_come_from_the_id_prefix():
    assert metric_class("pair/A/1/pass_count") == "link"
    assert metric_class("station/A/visible_fraction") == "station"
    assert metric_class("route/A->B/maximum_rate/route_changes") == "route"
    assert is_gated("pair/A/1/pass_count") and is_gated("station/A/visible_fraction")
    assert not is_gated("route/A->B/maximum_rate/route_changes")
    with pytest.raises(ValueError, match="must start with"):
        metric_class("other/x")


def _route_failure():
    return ConvergenceRow("route/A->B/fixed_capacity/route_changes", 53.0, 54.0, 1.0, 1 / 54, False)


def test_error_mode_with_only_route_failures_publishes_with_warnings():
    rows = (_route_failure(), ConvergenceRow("pair/A/1/pass_count", 1.0, 1.0, 0.0, 0.0, True))
    block, warnings = gate(rows, SETTINGS, step_s=60.0)
    assert block["summary"]["failed"] == 1
    assert block["summary"]["fatal"] == 0
    assert block["summary"]["classes"]["route"] == {"gated": False, "metrics": 1, "failed": 1}
    assert block["failures"][0]["class"] == "route"
    assert block["failures"][0]["fatal"] is False
    assert len(warnings) == 1
    assert "route/A->B/fixed_capacity/route_changes" in warnings[0]
    assert "not resolved in S1 (spec 6.5)" in warnings[0]
    assert "route metrics are not gated" in warnings[0]


def test_error_mode_lists_only_fatal_failures():
    rows = (_route_failure(), *_failing(1))
    with pytest.raises(ValueError) as caught:
        gate(rows, SETTINGS, step_s=60.0)
    message = str(caught.value)
    assert "1 of 3" in message
    assert "pair/A/0/integrated_rate_bits" in message
    assert "route/" not in message
    settings = ConvergenceSettings("warn", 4, 0.01, 2.0)
    block, warnings = gate(rows, settings, step_s=60.0)
    assert [entry["fatal"] for entry in block["failures"]] == [False, True]
    assert len(warnings) == 2
