"""The 2.0 sample grid and its conversion to UTC text and Skyfield times.

A grid is a vector of offsets in seconds after ``start_utc``: 0, Δ, 2Δ, ... and the window
length (the stop point) when it is not on the grid. Offsets are whole microseconds, the time
resolution of scenario timestamps and of the Python datetimes v1 propagated at.

Offsets count UTC seconds as Python datetimes do, which have no 23:59:60. A window that
contains a UTC leap second would make them disagree with elapsed SI seconds, so it is refused.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from math import isclose, isfinite
from typing import TYPE_CHECKING

import numpy as np
from skyfield.api import load

from openleo.scenario.parse import MAX_GRID_SAMPLES

if TYPE_CHECKING:
    from numpy.typing import ArrayLike, NDArray

    from openleo.scenario.model import TimeConfig

_MICROSECOND = timedelta(microseconds=1)
_UNIX_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_UNIX_EPOCH_JD = 2440587.5


@dataclass(frozen=True, slots=True, eq=False)
class TimeGrid:
    start_utc: datetime
    stop_utc: datetime
    step_s: float
    # Seconds after start_utc, read-only: 0, step, 2 step, ..., and the window length.
    offsets_s: NDArray[np.float64]


@functools.cache
def builtin_timescale():
    """Skyfield's timescale from its builtin leap-second and ΔT tables, as v1 uses."""
    return load.timescale(builtin=True)


def build_grid(time: TimeConfig) -> TimeGrid:
    """Sample grid of a scenario's time block. The parser has checked it; this re-checks."""
    start = _utc(time.start_utc, "/time/start_utc")
    stop = _utc(time.stop_utc, "/time/stop_utc")
    if stop <= start:
        raise ValueError("/time/stop_utc must be after /time/start_utc")
    window_us = (stop - start) // _MICROSECOND
    step_us = _step_microseconds(time.step_s, window_us)
    _reject_leap_seconds(start, stop)
    offsets_us = np.append(np.arange(0, window_us, step_us, dtype=np.int64), window_us)
    return TimeGrid(start, stop, float(time.step_s), _seconds(offsets_us))


def refine(grid: TimeGrid, factor: int) -> TimeGrid:
    """Split every grid interval, the short last one included, into ``factor`` equal parts.

    Every primary point is kept exactly. Inserted points are rounded to whole microseconds.
    """
    if isinstance(factor, bool) or not isinstance(factor, int) or factor < 1:
        raise ValueError(f"refinement factor must be a positive integer, got {factor!r}")
    primary_us = _microseconds(grid.offsets_s)
    parts = np.arange(factor, dtype=np.int64)
    lengths_us = np.diff(primary_us)[:, None]
    # Nearest whole microsecond to start + length * part / factor, halves rounded up.
    inserted_us = primary_us[:-1, None] + (2 * lengths_us * parts + factor) // (2 * factor)
    offsets_us = np.unique(np.append(inserted_us.ravel(), primary_us[-1]))
    return TimeGrid(grid.start_utc, grid.stop_utc, grid.step_s / factor, _seconds(offsets_us))


def utc_text(start_utc: datetime, offset_s: float) -> str:
    """ISO 8601 UTC text ending in Z, at the offset rounded to the microsecond."""
    timestamp = start_utc + timedelta(seconds=float(offset_s))
    return timestamp.astimezone(UTC).isoformat().replace("+00:00", "Z")


def skyfield_times(timescale, start_utc: datetime, offsets_s: ArrayLike):
    """Skyfield times at ``start_utc`` + offsets, built exactly as v1 built its grid times.

    v1 called ``timescale.from_datetimes`` on Python datetimes, so each offset is rounded to
    the microsecond by datetime arithmetic (round half to even) before the conversion.
    """
    offsets = np.asarray(offsets_s, dtype=np.float64)
    if offsets.ndim != 1 or offsets.size == 0:
        raise ValueError("offsets must be a non-empty one-dimensional array")
    return timescale.from_datetimes(
        [start_utc + timedelta(seconds=offset) for offset in offsets.tolist()]
    )


def _utc(value: datetime, pointer: str) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() != timedelta(0):
        raise ValueError(f"{pointer} must be a timezone-aware UTC datetime")
    return value.astimezone(UTC)


def _step_microseconds(step_s: float, window_us: int) -> int:
    if not isinstance(step_s, int | float) or not isfinite(step_s) or step_s <= 0.0:
        raise ValueError(f"/time/step_s={step_s!r} must be a positive finite number")
    if step_s * 1e6 > window_us:
        raise ValueError(f"/time/step_s={step_s!r} must be no larger than the window")
    step = timedelta(seconds=step_s)
    if step <= timedelta(0) or not isclose(step.total_seconds(), step_s, abs_tol=1e-12):
        raise ValueError(f"/time/step_s={step_s!r} must be a whole number of microseconds")
    step_us = step // _MICROSECOND
    samples = -(-window_us // step_us) + 1
    if samples > MAX_GRID_SAMPLES:
        raise ValueError(
            f"/time/step_s={step_s!r} produces {samples} samples including the stop point; "
            f"the limit is {MAX_GRID_SAMPLES}"
        )
    return step_us


def _reject_leap_seconds(start: datetime, stop: datetime) -> None:
    """Refuse a window containing a leap second, using Skyfield's leap-second table.

    The table lists the Julian date of the UTC midnight that follows each leap second. The
    leap second lies inside the window when start < that midnight <= stop.
    """
    for julian_date in builtin_timescale().leap_dates.tolist():
        if not isfinite(julian_date):  # legacy tables pad with infinities
            continue
        midnight = _UNIX_EPOCH + timedelta(days=julian_date - _UNIX_EPOCH_JD)
        if start < midnight <= stop:
            raise ValueError(
                "/time: windows containing a UTC leap second are not supported; the leap "
                f"second before {utc_text(midnight, 0.0)} lies between start_utc and stop_utc"
            )


def _microseconds(offsets_s: NDArray[np.float64]) -> NDArray[np.int64]:
    return np.round(np.asarray(offsets_s) * 1e6).astype(np.int64)


def _seconds(offsets_us: NDArray[np.int64]) -> NDArray[np.float64]:
    # Integer microseconds divided once: each offset is the double nearest to its true value.
    offsets = offsets_us / 1e6
    offsets.setflags(write=False)
    return offsets
