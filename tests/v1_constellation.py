"""The v1 constellation fixture once in tests/test_app.py, shared by the v1 study tests."""

import json
from pathlib import Path

import pytest


@pytest.fixture
def scenario(tmp_path):
    from openleo.constellation import load_constellation

    original = json.loads(Path("examples/scenarios/iss_cartagena.json").read_text())
    (tmp_path / "orbit.csv").write_bytes(Path("examples/data/iss_2026-08-30.csv").read_bytes())
    payload = {
        "name": "local-test",
        "orbit": {**original["orbit"], "path": "orbit.csv"},
        "stations": [
            original["ground_station"],
            {**original["ground_station"], "name": "Neighbor", "longitude_deg": -0.95},
        ],
        "time_window": {
            **original["time_window"],
            "start_utc": "2026-08-30T06:17:00Z",
            "stop_utc": "2026-08-30T06:17:20Z",
        },
        "radio_link": original["radio_link"],
        "adaptation": {
            "symbol_rate_baud": 500_000,
            "rolloff": 0.2,
            "implementation_margin_db": 1,
            "hysteresis_db": 0.5,
        },
        "network": {
            "source_station": "Cartagena",
            "target_station": "Neighbor",
            "isl_max_range_m": 5_000_000,
            "isl_capacity_bps": 10_000_000,
            "fixed_capacity_bps": 1_000_000,
        },
    }
    path = tmp_path / "scenario.json"
    path.write_text(json.dumps(payload))
    return load_constellation(path)
