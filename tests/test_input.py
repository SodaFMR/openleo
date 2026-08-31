import copy
import json
from dataclasses import FrozenInstanceError
from datetime import UTC
from hashlib import sha256
from pathlib import Path

import pytest

from openleo.input import load_scenario

FROZEN_SCENARIO = Path("examples/scenarios/iss_cartagena.json")


def _valid_scenario() -> dict:
    return {
        "name": "reference-pass",
        "orbit": {
            "path": "orbit.csv",
            "source_url": "https://example.test/orbit.csv",
            "retrieved_at_utc": "2026-08-30T22:01:11Z",
            "terms_url": "https://example.test/terms",
            "sha256": "0" * 64,
        },
        "ground_station": {
            "name": "Cartagena",
            "latitude_deg": 37.6057,
            "longitude_deg": -0.9913,
            "height_m": 20.0,
        },
        "time_window": {
            "start_utc": "2026-08-30T06:14:00Z",
            "stop_utc": "2026-08-30T06:24:00Z",
            "step_s": 10.0,
            "minimum_elevation_deg": 10.0,
        },
        "radio_link": {
            "carrier_frequency_hz": 2200000000.0,
            "channel_bandwidth_hz": 1000000.0,
            "eirp_dbw": 10.0,
            "receiver_gain_dbi": 20.0,
            "system_noise_temperature_k": 300.0,
            "miscellaneous_loss_db": 2.0,
        },
    }


def _write_scenario(tmp_path, payload: dict) -> tuple:
    orbit_path = tmp_path / "orbit.csv"
    orbit_path.write_text("OBJECT_NAME,NORAD_CAT_ID\nOPENLEO,0\n", encoding="utf-8")
    scenario_path = tmp_path / "scenario.json"
    scenario_path.write_text(json.dumps(payload), encoding="utf-8")
    return scenario_path, orbit_path


def _set_dotted(payload: dict, dotted_path: str, value: object) -> None:
    target = payload
    parts = dotted_path.split(".")
    for part in parts[:-1]:
        target = target[part]
    target[parts[-1]] = value


def _delete_dotted(payload: dict, dotted_path: str) -> None:
    target = payload
    parts = dotted_path.split(".")
    for part in parts[:-1]:
        target = target[part]
    del target[parts[-1]]


def test_loads_valid_scenario(tmp_path) -> None:
    scenario_path, orbit_path = _write_scenario(tmp_path, _valid_scenario())

    scenario = load_scenario(scenario_path)

    assert scenario.name == "reference-pass"
    assert scenario.orbit.path == orbit_path.resolve()
    assert scenario.orbit.provenance.source_url == "https://example.test/orbit.csv"
    assert scenario.orbit.provenance.retrieved_at_utc.tzinfo is UTC
    assert scenario.ground_station.name == "Cartagena"
    assert scenario.ground_station.latitude_deg == 37.6057
    assert scenario.time_window.start_utc.tzinfo is UTC
    assert scenario.time_window.stop_utc.tzinfo is UTC
    assert scenario.time_window.step_s == 10.0
    assert scenario.radio_link.carrier_frequency_hz == 2200000000.0
    with pytest.raises(FrozenInstanceError):
        scenario.ground_station.height_m = 21.0


def test_non_object_json_is_reported_as_invalid_scenario(tmp_path) -> None:
    scenario_path = tmp_path / "scenario.json"
    scenario_path.write_text("[]", encoding="utf-8")

    with pytest.raises(ValueError, match=r"invalid scenario: scenario must be a JSON object"):
        load_scenario(scenario_path)

    with pytest.raises(ValueError) as exc_info:
        load_scenario(scenario_path)
    assert "could not load scenario" not in str(exc_info.value)


def test_frozen_scenario_fingerprint_matches_raw_bytes() -> None:
    scenario = load_scenario(FROZEN_SCENARIO)

    assert scenario.source_sha256 == sha256(FROZEN_SCENARIO.read_bytes()).hexdigest()


def test_rejects_oversized_scenario_before_json_parsing(tmp_path) -> None:
    scenario_path = tmp_path / "oversized-scenario.json"
    scenario_path.write_bytes(b" " * 1_000_001)

    with pytest.raises(ValueError) as exc_info:
        load_scenario(scenario_path)

    assert str(scenario_path) in str(exc_info.value)
    assert "scenario JSON exceeds 1000000 bytes" in str(exc_info.value)


def test_scenario_fingerprint_preserves_insignificant_numeric_text(tmp_path) -> None:
    scenario_path, _ = _write_scenario(tmp_path, _valid_scenario())
    original_bytes = scenario_path.read_bytes()
    changed_bytes = original_bytes.replace(b'"step_s": 10.0', b'"step_s": 1e1')
    assert changed_bytes != original_bytes

    original = load_scenario(scenario_path)
    scenario_path.write_bytes(changed_bytes)
    changed = load_scenario(scenario_path)

    assert original.time_window.step_s == changed.time_window.step_s == 10.0
    assert original.source_sha256 == sha256(original_bytes).hexdigest()
    assert changed.source_sha256 == sha256(changed_bytes).hexdigest()
    assert original.source_sha256 != changed.source_sha256


@pytest.mark.parametrize(
    ("mutation", "dotted_path", "value", "message"),
    [
        ("set", "time_window.start_utc", "2026-08-30T06:14:00", "time_window.start_utc"),
        ("set", "time_window.stop_utc", "2026-08-30T07:24:00+01:00", "time_window.stop_utc"),
        ("set", "time_window.stop_utc", "2026-08-30T06:14:00Z", "time_window.stop_utc"),
        ("set", "ground_station.latitude_deg", 90.1, "ground_station.latitude_deg"),
        ("set", "time_window.minimum_elevation_deg", 90.0, "time_window.minimum_elevation_deg"),
        ("set", "radio_link.channel_bandwidth_hz", 0.0, "radio_link.channel_bandwidth_hz"),
        ("set", "radio_link.miscellaneous_loss_db", -1.0, "radio_link.miscellaneous_loss_db"),
        ("set", "orbit.sha256", "not-a-sha256", "orbit.sha256"),
        ("delete", "orbit.terms_url", None, "orbit.terms_url"),
        ("set", "radio_link.bandwith_hz", 1.0, "radio_link.bandwith_hz"),
    ],
)
def test_rejects_invalid_scenarios(tmp_path, mutation, dotted_path, value, message) -> None:
    payload = copy.deepcopy(_valid_scenario())
    if mutation == "delete":
        _delete_dotted(payload, dotted_path)
    else:
        _set_dotted(payload, dotted_path, value)
    scenario_path, _ = _write_scenario(tmp_path, payload)

    with pytest.raises(ValueError, match=message) as exc_info:
        load_scenario(scenario_path)
    assert str(scenario_path) in str(exc_info.value)


@pytest.mark.parametrize("step_s", [1e-7, 1.5e-6])
def test_rejects_sampling_intervals_below_microsecond_resolution(tmp_path, step_s) -> None:
    payload = _valid_scenario()
    payload["time_window"]["step_s"] = step_s
    scenario_path, _ = _write_scenario(tmp_path, payload)

    with pytest.raises(ValueError, match=r"time_window\.step_s=.*microsecond"):
        load_scenario(scenario_path)


def test_accepts_one_microsecond_sampling_interval(tmp_path) -> None:
    payload = _valid_scenario()
    payload["time_window"].update(
        start_utc="2026-08-30T06:14:00Z",
        stop_utc="2026-08-30T06:14:00.000001Z",
        step_s=1e-6,
    )
    scenario_path, _ = _write_scenario(tmp_path, payload)

    assert load_scenario(scenario_path).time_window.step_s == 1e-6


def test_rejects_time_grid_above_sample_limit(tmp_path) -> None:
    payload = _valid_scenario()
    payload["time_window"].update(
        start_utc="2026-08-30T06:14:00Z",
        stop_utc="2026-08-30T06:14:01Z",
        step_s=1e-5,
    )
    scenario_path, _ = _write_scenario(tmp_path, payload)

    with pytest.raises(ValueError, match=r"time_window\.step_s=.*100001.*100000"):
        load_scenario(scenario_path)


@pytest.mark.parametrize(
    ("dotted_path", "value"),
    [
        ("name", "   "),
        ("ground_station.name", "\t\n"),
        ("orbit.path", " "),
    ],
)
def test_rejects_whitespace_only_required_strings(tmp_path, dotted_path, value) -> None:
    payload = _valid_scenario()
    _set_dotted(payload, dotted_path, value)
    scenario_path, _ = _write_scenario(tmp_path, payload)

    with pytest.raises(ValueError, match=dotted_path):
        load_scenario(scenario_path)


@pytest.mark.parametrize(
    ("dotted_path", "value"),
    [
        ("orbit.source_url", "   "),
        ("orbit.source_url", "relative/orbit.csv"),
        ("orbit.source_url", "file:///tmp/orbit.csv"),
        ("orbit.source_url", "https://user:secret@example.test/orbit.csv"),
        ("orbit.source_url", "https://example.test/orbit file.csv"),
        ("orbit.terms_url", "terms"),
    ],
)
def test_rejects_invalid_provenance_urls(tmp_path, dotted_path, value) -> None:
    payload = _valid_scenario()
    _set_dotted(payload, dotted_path, value)
    scenario_path, _ = _write_scenario(tmp_path, payload)

    with pytest.raises(ValueError, match=dotted_path) as exc_info:
        load_scenario(scenario_path)
    assert repr(value) in str(exc_info.value)
    assert "absolute HTTP or HTTPS URL" in str(exc_info.value)


def test_numeric_boundary_error_includes_field_value_and_constraint(tmp_path) -> None:
    payload = _valid_scenario()
    payload["ground_station"]["latitude_deg"] = 90.1
    scenario_path, _ = _write_scenario(tmp_path, payload)

    with pytest.raises(ValueError) as exc_info:
        load_scenario(scenario_path)

    message = str(exc_info.value)
    assert "ground_station.latitude_deg" in message
    assert "90.1" in message
    assert "[-90.0, 90.0]" in message
