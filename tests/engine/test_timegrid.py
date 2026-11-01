"""The v2 sample grid: stop point, refinement, leap seconds, UTC text and Skyfield times."""

from datetime import UTC, datetime, timedelta

import numpy as np
import pytest
from skyfield.api import load

from openleo.scenario.model import ConvergenceSettings, EventSettings, TimeConfig

START = datetime(2026, 9, 10, 12, 0, 0, tzinfo=UTC)
# Julian date of 2026-09-10T00:00:00Z, the UTC midnight after a fictitious leap second.
JD_2026_09_10 = 2461293.5


def _time(start: datetime, stop: datetime, step_s: float) -> TimeConfig:
    return TimeConfig(start, stop, step_s, EventSettings(), ConvergenceSettings())


def test_grid_includes_stop_when_off_grid() -> None:
    from openleo.engine.timegrid import build_grid

    grid = build_grid(_time(START, START + timedelta(seconds=50), 20.0))

    assert grid.start_utc == START
    assert grid.stop_utc == START + timedelta(seconds=50)
    assert grid.step_s == 20.0
    np.testing.assert_array_equal(grid.offsets_s, [0.0, 20.0, 40.0, 50.0])
    assert grid.offsets_s.dtype == np.float64
    assert not grid.offsets_s.flags.writeable


def test_grid_on_grid_stop_appears_once() -> None:
    from openleo.engine.timegrid import build_grid

    grid = build_grid(_time(START, START + timedelta(seconds=60), 20.0))

    np.testing.assert_array_equal(grid.offsets_s, [0.0, 20.0, 40.0, 60.0])


def test_grid_offsets_are_exact_for_fractional_steps() -> None:
    from openleo.engine.timegrid import build_grid

    grid = build_grid(_time(START, START + timedelta(seconds=1), 0.1))

    # Each offset is the double nearest to k * 0.1 s, not an accumulated sum.
    np.testing.assert_array_equal(grid.offsets_s, [k / 10 for k in range(11)])


def test_refine_inserts_points() -> None:
    from openleo.engine.timegrid import build_grid, refine

    grid = build_grid(_time(START, START + timedelta(minutes=5), 60.0))
    refined = refine(grid, 4)

    assert refined.step_s == 15.0
    assert (refined.start_utc, refined.stop_utc) == (grid.start_utc, grid.stop_utc)
    np.testing.assert_array_equal(refined.offsets_s, np.arange(21) * 15.0)
    assert set(grid.offsets_s) <= set(refined.offsets_s)
    assert not refined.offsets_s.flags.writeable


def test_refine_splits_the_short_last_interval_and_keeps_the_stop() -> None:
    from openleo.engine.timegrid import build_grid, refine

    grid = build_grid(_time(START, START + timedelta(seconds=50), 20.0))

    np.testing.assert_array_equal(
        refine(grid, 2).offsets_s, [0.0, 10.0, 20.0, 30.0, 40.0, 45.0, 50.0]
    )


def test_refine_rounds_interior_points_to_whole_microseconds() -> None:
    from openleo.engine.timegrid import build_grid, refine

    grid = build_grid(_time(START, START + timedelta(minutes=2), 60.0))
    refined = refine(grid, 7)

    microseconds = refined.offsets_s * 1e6
    np.testing.assert_array_equal(microseconds, np.round(microseconds))
    assert len(refined.offsets_s) == 15
    assert set(grid.offsets_s) <= set(refined.offsets_s)
    np.testing.assert_allclose(np.diff(refined.offsets_s), 60.0 / 7, rtol=0, atol=1e-6)
    assert refined.step_s == 60.0 / 7


def test_refine_factor_one_is_the_identity_and_others_are_rejected() -> None:
    from openleo.engine.timegrid import build_grid, refine

    grid = build_grid(_time(START, START + timedelta(minutes=2), 60.0))
    np.testing.assert_array_equal(refine(grid, 1).offsets_s, grid.offsets_s)
    for factor in (0, -2, 2.5, True):
        with pytest.raises(ValueError, match="factor"):
            refine(grid, factor)


def test_leap_second_window_rejected(monkeypatch) -> None:
    from openleo.engine.timegrid import build_grid, builtin_timescale

    timescale = builtin_timescale()
    # Legacy Skyfield tables pad the dates with infinities, which are skipped.
    table = np.concatenate(([-np.inf], timescale.leap_dates, [JD_2026_09_10, np.inf]))
    monkeypatch.setattr(timescale, "leap_dates", table)
    midnight = datetime(2026, 9, 10, tzinfo=UTC)

    with pytest.raises(ValueError) as caught:
        build_grid(_time(midnight - timedelta(hours=1), midnight + timedelta(hours=1), 60.0))
    message = str(caught.value)
    assert message.startswith("/time"), message
    assert "windows containing a UTC leap second are not supported" in message

    # The leap second is inserted just before UTC midnight: a window ending at midnight
    # contains it, and a window starting at midnight does not.
    with pytest.raises(ValueError, match="^/time"):
        build_grid(_time(midnight - timedelta(hours=1), midnight, 60.0))
    grid = build_grid(_time(midnight, midnight + timedelta(hours=1), 60.0))
    assert grid.offsets_s[-1] == 3600.0


def test_real_2016_leap_second_rejected() -> None:
    from openleo.engine.timegrid import build_grid

    midnight = datetime(2017, 1, 1, tzinfo=UTC)
    with pytest.raises(ValueError, match="^/time: .*leap second"):
        build_grid(_time(midnight - timedelta(minutes=30), midnight + timedelta(minutes=30), 60.0))


def test_build_grid_rechecks_what_it_relies_on() -> None:
    from openleo.engine.timegrid import build_grid

    naive = datetime(2026, 9, 10, 12)  # noqa: DTZ001 (deliberately naive)
    for config, pointer in (
        (_time(START, START, 1.0), "/time/stop_utc"),
        (_time(START, START + timedelta(seconds=10), 0.0), "/time/step_s"),
        (_time(START, START + timedelta(seconds=10), 1e-7), "/time/step_s"),
        (_time(START, START + timedelta(seconds=10), 11.0), "/time/step_s"),
        (_time(naive, naive + timedelta(seconds=10), 1.0), "/time/start_utc"),
        (_time(START, START + timedelta(days=31), 1.0), "/time/step_s"),
    ):
        with pytest.raises(ValueError, match=f"^{pointer}"):
            build_grid(config)


def test_build_grid_step_check_has_no_relative_tolerance() -> None:
    from openleo.engine.timegrid import build_grid

    # 1000.0000004 s rounds to 1000 s at microsecond resolution. A default relative tolerance
    # (1e-9 of 1000 s = 1e-6 s) would accept the 4e-7 s difference; the parser and v1 do not.
    with pytest.raises(ValueError, match="^/time/step_s=1000.0000004 .*whole number"):
        build_grid(_time(START, START + timedelta(hours=1), 1000.0000004))


def test_utc_text_rounds_to_microseconds() -> None:
    from openleo.engine.timegrid import utc_text

    assert utc_text(START, 0.0) == "2026-09-10T12:00:00Z"
    assert utc_text(START, 60.0) == "2026-09-10T12:01:00Z"
    assert utc_text(START, 1.2345674) == "2026-09-10T12:00:01.234567Z"
    assert utc_text(START, 59.9999996) == "2026-09-10T12:01:00Z"
    assert utc_text(START, -0.5) == "2026-09-10T11:59:59.500000Z"


def test_skyfield_times_equal_the_v1_grid_times() -> None:
    from openleo.engine.timegrid import build_grid, skyfield_times

    timescale = load.timescale(builtin=True)
    stop = START + timedelta(minutes=2, seconds=5)
    grid = build_grid(_time(START, stop, 7.5))
    step = timedelta(seconds=7.5)
    # v1: start + index * step for every point but the last, which is stop itself.
    v1 = [START + index * step for index in range(len(grid.offsets_s) - 1)] + [stop]

    expected = timescale.from_datetimes(v1)
    actual = skyfield_times(timescale, START, grid.offsets_s)

    np.testing.assert_array_equal(actual.whole, expected.whole)
    np.testing.assert_array_equal(actual.tai_fraction, expected.tai_fraction)
    np.testing.assert_array_equal(actual.tt_fraction, expected.tt_fraction)


def test_skyfield_times_need_a_non_empty_vector() -> None:
    from openleo.engine.timegrid import skyfield_times

    timescale = load.timescale(builtin=True)
    for offsets in (np.array([]), np.zeros((2, 2))):
        with pytest.raises(ValueError, match="offsets"):
            skyfield_times(timescale, START, offsets)


def test_skyfield_times_round_offsets_to_microseconds() -> None:
    from openleo.engine.timegrid import skyfield_times

    timescale = load.timescale(builtin=True)
    actual = skyfield_times(timescale, START, np.array([1.2345674, 1.2345666]))
    expected = timescale.from_datetimes([START + timedelta(microseconds=1_234_567)] * 2)

    np.testing.assert_array_equal(actual.whole, expected.whole)
    np.testing.assert_array_equal(actual.tai_fraction, expected.tai_fraction)
