"""Per-pair and per-station link statistics on one time-measure convention (spec section 6.4).

Every duration is a measure in seconds and every fraction is a measure divided by the window
length T = T1 - T0 (no sample counts, and no extra interval after the last sample). Rates are
piecewise constant on ``RateSegment``s and integrated exactly (``integrate_piecewise``).

Passes are half-open [start_s, end_s). A pass of zero duration is not a contact: an exact mask
touch inside the window, and also a satellite visible only at the instant T (a truncated pass
of zero duration). The latter has no time inside the window, and the contact it starts belongs
to the next window, where it is a pass truncated at the start; counting it here would count it
twice over adjacent windows. ``pass_count`` therefore counts passes of positive duration.

Best link. At every time, the best link of a station is the visible satellite with the highest
rate; ties go to the lowest satellite index; when every rate is 0 (or nothing is visible) there
is no best link (None, rate 0). The envelope is piecewise constant on the union of the pairs'
segment ends. A handover is a change of best satellite between two adjacent envelope segments
that both have one, so a stretch without a best link separates two links without a handover
(v1's rule on consecutive samples).
"""

from __future__ import annotations

import math
from bisect import bisect_right
from dataclasses import dataclass
from itertools import pairwise
from typing import TYPE_CHECKING

import numpy as np

from openleo.engine.integrate import integrate_piecewise, measure
from openleo.physics.adaptation import MODCODS

if TYPE_CHECKING:
    from collections.abc import Sequence

    from openleo.engine.acm import ModcodEvent, RateSegment
    from openleo.engine.events import Pass

_RESIDENCE = {
    modcod.name: "residence_" + modcod.name.lower().replace(" ", "_").replace("/", "_") + "_s"
    for modcod in MODCODS
}


@dataclass(frozen=True, slots=True)
class PairResult:
    """Everything the engine found for one station-satellite pair over the window.

    ``segments`` tile the ``passes`` (``openleo.engine.acm``); ``shannon_bits`` is the Shannon
    upper bound integrated over the passes; ``max_elevation_deg`` and ``min_range_m`` are over
    the passes (NaN when there is none).
    """

    station_index: int
    satellite_index: int
    passes: tuple[Pass, ...]
    segments: tuple[RateSegment, ...]
    modcod_events: tuple[ModcodEvent, ...]
    shannon_bits: float
    max_elevation_deg: float
    min_range_m: float


def pair_metrics(pair: PairResult) -> dict[str, float | int]:
    """Metrics of one pair (module docstring for passes and measures).

    ``mean_rate_visible_bps`` is the integrated rate over the visible duration, NaN when the
    pair is never visible. ``residence_<slug>_s`` is the time each MODCOD of the table is in
    effect (0.0 if never); the slug is the MODCOD name in lower case, spaces and ``/`` as ``_``.
    """
    passes, segments = _pair_intervals(pair)
    visible_s = measure((p.start_s, p.end_s) for p in passes)
    bits = _bits(segments)
    unknown = {s.modcod for s in segments if s.modcod is not None} - set(_RESIDENCE)
    if unknown:
        raise ValueError(
            f"pair (station {pair.station_index}, satellite {pair.satellite_index}) has segments "
            f"with MODCODs outside the table: {sorted(unknown)}"
        )
    residence = {
        key: math.fsum(s.end_s - s.start_s for s in segments if s.modcod == name)
        for name, key in _RESIDENCE.items()
    }
    return {
        "pass_count": sum(1 for p in passes if p.end_s > p.start_s),
        "visible_duration_s": visible_s,
        "rf_usable_duration_s": measure((s.start_s, s.end_s) for s in segments if s.rate_bps > 0.0),
        "integrated_rate_bits": bits,
        "integrated_shannon_upper_bound_bits": float(pair.shannon_bits),
        "max_elevation_deg": float(pair.max_elevation_deg),
        "min_range_m": float(pair.min_range_m),
        "mean_rate_visible_bps": bits / visible_s if visible_s > 0.0 else math.nan,
        **residence,
    }


def best_link_segments(
    pairs: Sequence[PairResult],
) -> tuple[tuple[float, float, int | None, float], ...]:
    """The best-link envelope as (start, end, satellite_index or None, rate) (module docstring).

    It spans from the earliest segment start to the latest segment end of all pairs, in time
    order, with gaps as (None, 0.0); neighbours with the same satellite and rate are merged.
    Pairs without segments contribute nothing; no segments at all give ().
    """
    tracks = []
    for pair in sorted(pairs, key=lambda p: p.satellite_index):
        _, segments = _pair_intervals(pair)
        segments = [s for s in segments if s.end_s > s.start_s]
        if segments:
            tracks.append((pair.satellite_index, segments))
    if not tracks:
        return ()
    cuts = np.unique([t for _, segments in tracks for s in segments for t in (s.start_s, s.end_s)])
    starts = cuts[:-1]
    best_rate = np.zeros(starts.size)
    best_satellite = np.full(starts.size, -1, dtype=np.int64)
    # Satellites in index order with a strict ">": a tie keeps the lower index.
    for satellite, segments in tracks:
        seg_start = np.array([s.start_s for s in segments])
        seg_end = np.array([s.end_s for s in segments])
        seg_rate = np.array([s.rate_bps for s in segments])
        index = np.searchsorted(seg_start, starts, side="right") - 1
        covered = (index >= 0) & (starts < seg_end[np.maximum(index, 0)])
        rate = np.where(covered, seg_rate[np.maximum(index, 0)], 0.0)
        better = rate > best_rate
        best_rate[better] = rate[better]
        best_satellite[better] = satellite
    envelope: list[tuple[float, float, int | None, float]] = []
    for start, end, satellite, rate in zip(
        starts.tolist(), cuts[1:].tolist(), best_satellite.tolist(), best_rate.tolist(), strict=True
    ):
        key = (None, 0.0) if satellite < 0 else (satellite, rate)
        if envelope and envelope[-1][2:] == key:
            envelope[-1] = (envelope[-1][0], end, *key)
        else:
            envelope.append((start, end, *key))
    return tuple(envelope)


def station_metrics(
    station_index: int,
    pairs: Sequence[PairResult],
    window_s: float,
    fixed_capacity_bps: float | None,
) -> dict[str, float | int]:
    """Metrics of one station from all its pairs over a window of ``window_s`` seconds.

    ``visible_fraction`` and ``rf_usable_fraction`` are the measures of the times at least one
    satellite is visible, and at least one has a non-zero rate (the best link's availability),
    over ``window_s``. ``best_link_mean_rate_bps`` is the best-link integral over ``window_s``.
    ``fixed_capacity_integrated_bits`` (only with ``fixed_capacity_bps``) is that capacity times
    the visible measure: the fixed-capacity baseline is available whenever any satellite is.
    """
    window_s = _positive(window_s, "window_s")
    if fixed_capacity_bps is not None:
        fixed_capacity_bps = _positive(fixed_capacity_bps, "fixed_capacity_bps")
    pairs = tuple(pairs)
    seen = set()
    passes, usable = [], []
    for pair in pairs:
        if pair.station_index != station_index:
            raise ValueError(
                f"station_metrics({station_index}) got a pair of station_index "
                f"{pair.station_index} (satellite {pair.satellite_index})"
            )
        if pair.satellite_index in seen:
            raise ValueError(
                f"satellite {pair.satellite_index} appears more than once for station "
                f"{station_index}"
            )
        seen.add(pair.satellite_index)
        pair_passes, segments = _pair_intervals(pair)
        for p in pair_passes:
            if p.start_s < 0.0 or p.end_s > window_s:
                raise ValueError(
                    f"satellite {pair.satellite_index} has a pass [{p.start_s!r}, {p.end_s!r}) "
                    f"outside the window [0, {window_s!r}] of station {station_index}"
                )
        passes += [(p.start_s, p.end_s) for p in pair_passes]
        usable += [(s.start_s, s.end_s) for s in segments if s.rate_bps > 0.0]
    envelope = best_link_segments(pairs)
    visible_s = measure(passes)
    bits = integrate_piecewise(
        [e[0] for e in envelope], [e[1] for e in envelope], [e[3] for e in envelope]
    )
    metrics: dict[str, float | int] = {
        "visible_fraction": visible_s / window_s,
        "rf_usable_fraction": measure(usable) / window_s,
        "best_link_integrated_bits": bits,
        "best_link_handovers": sum(
            1
            for left, right in pairwise(envelope)
            if left[2] is not None and right[2] is not None and left[2] != right[2]
        ),
        "best_link_mean_rate_bps": bits / window_s,
    }
    if fixed_capacity_bps is not None:
        metrics["fixed_capacity_integrated_bits"] = fixed_capacity_bps * visible_s
    return metrics


def _pair_intervals(pair: PairResult) -> tuple[tuple[Pass, ...], tuple[RateSegment, ...]]:
    """The pair's passes and segments, checked: finite, ordered, disjoint, segments in passes."""
    name = f"pair (station {pair.station_index}, satellite {pair.satellite_index})"
    passes = tuple(sorted(pair.passes, key=lambda p: (p.start_s, p.end_s)))
    segments = tuple(sorted(pair.segments, key=lambda s: (s.start_s, s.end_s)))
    for kind, items in (("pass", passes), ("segment", segments)):
        for item in items:
            if not (math.isfinite(item.start_s) and math.isfinite(item.end_s)):
                raise ValueError(f"{name} has a {kind} with non-finite ends: {item!r}")
            if item.start_s > item.end_s:
                raise ValueError(f"{name} has a {kind} with start_s > end_s: {item!r}")
        for left, right in pairwise(items):
            if right.start_s < left.end_s:
                raise ValueError(f"{name} has {kind}s that overlap: {left!r} and {right!r}")
    starts = [p.start_s for p in passes]
    for s in segments:
        if not (math.isfinite(s.rate_bps) and s.rate_bps >= 0.0):
            raise ValueError(f"{name} has a segment whose rate is not finite and >= 0: {s!r}")
        index = bisect_right(starts, s.start_s) - 1
        if index < 0 or s.end_s > passes[index].end_s:
            raise ValueError(f"{name} has a segment outside its passes: {s!r}")
    return passes, segments


def _bits(segments: Sequence[RateSegment]) -> float:
    return integrate_piecewise(
        [s.start_s for s in segments], [s.end_s for s in segments], [s.rate_bps for s in segments]
    )


def _positive(value, name: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float | np.floating)
        or not math.isfinite(value)
        or value <= 0.0
    ):
        raise ValueError(f"{name} must be a positive finite number, got {value!r}")
    return float(value)
