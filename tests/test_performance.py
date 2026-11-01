"""Performance acceptance (design spec section 11.6): 1,000 satellites x 4 stations x 24 h.

The pinned CelesTrak Starlink catalog (``examples/data/starlink_2026-11-01.csv``), events on,
``itu_reference`` propagation, no network, one worker: under five minutes on the development
machine, within the default memory budget, with the convergence gate passed. Run it with
``uv run pytest -m benchmark tests/test_performance.py``. CI's slow job deselects it
(``-m "slow and not benchmark"``): the bound is set for the development machine, not for shared
CI runners.
"""

import time
from pathlib import Path

import pytest

from openleo.engine.run import run
from openleo.scenario import load_scenario

ROOT = Path(__file__).resolve().parents[1]
SCENARIO = ROOT / "examples" / "performance" / "starlink_1000_24h.json"
BOUND_S = 300.0


@pytest.mark.slow
@pytest.mark.benchmark
def test_starlink_1000_24h_under_five_minutes():
    scenario = load_scenario(SCENARIO)

    start = time.perf_counter()
    results = run(scenario, workers=1)
    elapsed = time.perf_counter() - start

    summary = results.metadata["summary"]
    assert summary["satellite_count"] == 1000
    assert summary["station_count"] == 4
    assert summary["window_s"] == 86_400.0
    convergence = results.metadata["convergence"]
    assert not convergence["skipped"]
    assert convergence["summary"]["failed"] == 0
    assert convergence["summary"]["passed"] == convergence["summary"]["metrics"] > 0
    assert elapsed < BOUND_S, f"{elapsed:.1f} s"
