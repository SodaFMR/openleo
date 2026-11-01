"""Performance acceptance, CI variant (design spec section 11.6), and the benchmark scenario.

The 1,000-satellite, 24-hour benchmark is ``tests/test_performance.py`` (``pytest -m benchmark``).
This variant runs its first 100 satellites for 2 hours with a generous bound on every CI
machine, so a gross regression of the engine's speed fails the default suite.
"""

import json
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path

from openleo.engine.catalogs import MAX_CATALOG_BYTES, MAX_SATELLITES
from openleo.engine.run import run
from openleo.scenario import load_scenario

ROOT = Path(__file__).resolve().parents[1]
SCENARIO = ROOT / "examples" / "performance" / "starlink_1000_24h.json"
CATALOG = ROOT / "examples" / "data" / "starlink_2026-11-01.csv"
CI_BOUND_S = 60.0


def _ci_variant():
    scenario = load_scenario(SCENARIO)
    space = scenario.space_segment
    time_block = scenario.time
    return replace(
        scenario,
        space_segment=replace(space, select_norad_ids=space.select_norad_ids[:100]),
        time=replace(time_block, stop_utc=time_block.start_utc + timedelta(hours=2)),
    )


def test_performance_scenario_is_the_specified_benchmark():
    scenario = load_scenario(SCENARIO)
    catalog = scenario.space_segment.catalogs[0]
    raw = CATALOG.read_bytes()
    norad_ids = sorted(int(row.split(b",")[11]) for row in raw.splitlines()[1:])

    assert catalog.path == CATALOG
    assert sha256(raw).hexdigest() == catalog.sha256
    # Within the 2.0 catalog limits (10 MB and 20,000 records per file).
    assert len(raw) <= MAX_CATALOG_BYTES == 10_000_000
    assert len(norad_ids) == 11_156
    assert len(norad_ids) <= MAX_SATELLITES == 20_000
    assert catalog.retrieved_at_utc == datetime(2026, 11, 1, 20, 43, 43, tzinfo=UTC)
    assert catalog.source_url == (
        "https://celestrak.org/NORAD/elements/gp.php?GROUP=starlink&FORMAT=csv"
    )
    # The first 1,000 NORAD ids in ascending order, through select_norad_ids.
    assert list(scenario.space_segment.select_norad_ids) == norad_ids[:1000]
    assert [s.name for s in scenario.ground_segment.stations] == [
        "Madrid",
        "Tromso",
        "Singapore",
        "Quito",
    ]
    assert [s.height_amsl_m for s in scenario.ground_segment.stations] == [
        650.0,
        0.0,
        0.0,
        2850.0,
    ]
    # 24 h from the retrieval date + 1 day, 00:00Z, at 30 s, events on, itu_reference, no network.
    assert scenario.time.start_utc == datetime(2026, 11, 2, tzinfo=UTC)
    assert scenario.time.stop_utc - scenario.time.start_utc == timedelta(hours=24)
    assert scenario.time.step_s == 30.0
    assert scenario.time.events.enabled
    assert scenario.propagation.model == "itu_reference"
    assert scenario.network is None
    assert json.loads(SCENARIO.read_text(encoding="utf-8"))["network"] is None


def test_ci_variant_100_sat_2h():
    scenario = _ci_variant()

    start = time.perf_counter()
    results = run(scenario)
    elapsed = time.perf_counter() - start

    summary = results.metadata["summary"]
    assert summary["satellite_count"] == 100
    assert summary["window_s"] == 7200.0
    assert summary["pass_count"] > 0
    assert results.metadata["convergence"]["summary"]["failed"] == 0
    models = results.metadata["models"]["shannon_integrand_atmosphere"]
    assert 0.0 < models["max_error_db"] <= 1e-10
    assert elapsed < CI_BOUND_S, f"{elapsed:.1f} s"
