import copy
import json
from dataclasses import FrozenInstanceError, replace
from datetime import datetime
from hashlib import sha256
from math import dist
from pathlib import Path

import pytest

from openleo.input import load_scenario
from openleo.simulation import simulate_scenario

ISS_SCENARIO = Path("examples/scenarios/iss_cartagena.json")
GLOBAL_SCENARIO = Path("examples/constellations/iridium_global.json")


def _raw():
    original = json.loads(ISS_SCENARIO.read_text(encoding="utf-8"))
    return {
        **{key: value for key, value in original.items() if key != "ground_station"},
        "stations": [
            original["ground_station"],
            {"name": "Remote", "latitude_deg": 69.6492, "longitude_deg": 18.9553, "height_m": 0},
        ],
        "adaptation": {
            "symbol_rate_baud": 800_000.0,
            "rolloff": 0.2,
            "implementation_margin_db": 0.0,
            "hysteresis_db": 0.5,
        },
        "network": {
            "source_station": "Cartagena",
            "target_station": "Remote",
            "isl_max_range_m": 5_000_000.0,
            "isl_capacity_bps": 1_000_000_000.0,
            "fixed_capacity_bps": 1_000_000.0,
        },
    }


def _scenario(raw=None):
    from openleo.constellation import parse_constellation

    return parse_constellation(_raw() if raw is None else raw, ISS_SCENARIO, "a" * 64)


def test_parse_and_portable_document_round_trip_without_mutating_input():
    from openleo.constellation import parse_constellation, scenario_document

    raw = _raw()
    saved = copy.deepcopy(raw)
    scenario = _scenario(raw)
    document = scenario_document(scenario)

    assert raw == saved == document
    assert isinstance(scenario.stations, tuple)
    assert scenario.orbit.path == Path("examples/data/iss_2026-08-30.csv").resolve()
    assert parse_constellation(document, ISS_SCENARIO, "a" * 64) == scenario
    document["stations"][0]["name"] = "changed"
    assert scenario.stations[0].name == "Cartagena"
    with pytest.raises(FrozenInstanceError):
        scenario.name = "changed"


def test_portable_document_supports_catalog_on_a_different_windows_drive(monkeypatch):
    from openleo.constellation import scenario_document

    scenario = _scenario()

    def cross_drive(*args):
        raise ValueError("path is on another drive")

    monkeypatch.setattr("openleo.constellation.relpath", cross_drive)
    assert scenario_document(scenario)["orbit"]["path"] == scenario.orbit.path.as_posix()


def test_load_checks_raw_input_bytes_and_reports_invalid_files(tmp_path):
    from openleo.constellation import load_constellation

    path = tmp_path / "input.json"
    payload = _raw()
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert load_constellation(path).source_sha256 == sha256(path.read_bytes()).hexdigest()
    for contents, message in (
        (b"{bad", "invalid JSON"),
        (b"\xff", "UTF-8"),
        (b" " * 1_000_001, "exceeds 1000000"),
        (b'{"name":"first","name":"second"}', "duplicate"),
        (b"[]", "JSON object"),
    ):
        path.write_bytes(contents)
        with pytest.raises(ValueError, match=message):
            load_constellation(path)
    with pytest.raises(ValueError, match="could not load"):
        load_constellation(tmp_path / "missing.json")


@pytest.mark.parametrize(
    ("group", "field", "value", "message"),
    [
        (None, "unexpected", True, "unexpected"),
        (None, "name", " ", "name"),
        ("adaptation", "symbol_rate_baud", 1_000_000, "bandwidth"),
        ("adaptation", "symbol_rate_baud", 0, "symbol_rate_baud"),
        ("adaptation", "hysteresis_db", -1, "hysteresis_db"),
        ("adaptation", "extra", 0, "extra"),
        ("radio_link", "carrier_frequency_hz", True, "carrier_frequency_hz"),
        ("radio_link", "eirp_dbw", float("nan"), "eirp_dbw"),
        ("time_window", "step_s", 0.4, "1441"),
        ("time_window", "step_s", 0.0000001, "microsecond"),
        ("time_window", "step_s", float("inf"), "step_s"),
        ("time_window", "start_utc", "2026-08-30T06:14:00", "start_utc"),
        ("time_window", "stop_utc", "2026-08-30T06:14:00Z", "stop_utc"),
        ("network", "target_station", "unknown", "known stations"),
        ("network", "target_station", "Cartagena", "distinct"),
        ("network", "fixed_capacity_bps", 0, "fixed_capacity_bps"),
    ],
)
def test_rejects_bad_json_values(group, field, value, message):
    raw = _raw()
    if group is None:
        raw[field] = value
    else:
        raw[group][field] = value
    with pytest.raises(ValueError, match=message):
        _scenario(raw)


@pytest.mark.parametrize("stations", [[], None, {}, [1], [True]])
def test_rejects_empty_or_malformed_station_lists(stations):
    with pytest.raises(ValueError, match="station"):
        _scenario({**_raw(), "stations": stations})


def test_rejects_duplicate_stations_and_limits_station_count():
    raw = _raw()
    with pytest.raises(ValueError, match="unique"):
        _scenario({**raw, "stations": [raw["stations"][0]] * 2})
    stations = [{**raw["stations"][0], "name": f"station-{i}"} for i in range(17)]
    with pytest.raises(ValueError, match="16"):
        _scenario({**raw, "stations": stations})


def test_revalidates_programmatic_inputs_before_loading_catalog(monkeypatch):
    from openleo.constellation import simulate_constellation

    scenario = _scenario()

    def forbidden(*args):
        pytest.fail("invalid programmatic configuration reached catalog loading")

    monkeypatch.setattr("openleo.constellation.load_catalog", forbidden)
    invalid = (
        replace(scenario, radio_link=replace(scenario.radio_link, eirp_dbw=float("nan"))),
        replace(scenario, stations=(replace(scenario.stations[0], latitude_deg=91),)),
        replace(scenario, network=replace(scenario.network, fixed_capacity_bps=-1)),
        replace(scenario, time_window=replace(scenario.time_window, step_s=True)),
        replace(
            scenario,
            time_window=replace(
                scenario.time_window,
                start_utc=datetime(2026, 8, 30, 6, 14),  # noqa: DTZ001 - invalid input under test
            ),
        ),
    )
    for candidate in invalid:
        with pytest.raises(ValueError):
            simulate_constellation(candidate)


def test_product_limit_fails_before_any_propagation(monkeypatch):
    from openleo.constellation import load_constellation, simulate_constellation

    scenario = load_constellation(GLOBAL_SCENARIO)
    stations = tuple(replace(scenario.stations[0], name=f"station-{index}") for index in range(16))
    scenario = replace(
        scenario,
        stations=stations,
        network=replace(scenario.network, source_station="station-0", target_station="station-1"),
        time_window=replace(scenario.time_window, step_s=18),
    )

    def forbidden(*args):
        pytest.fail("product limit reached SGP4 propagation")

    monkeypatch.setattr("skyfield.api.EarthSatellite.at", forbidden)
    with pytest.raises(ValueError, match="500000"):
        simulate_constellation(scenario)


def test_iss_links_agree_with_existing_pass_and_ecef_geometry():
    from openleo.constellation import simulate_constellation

    scenario = _scenario()
    output = simulate_constellation(scenario)
    original = simulate_scenario(load_scenario(ISS_SCENARIO))
    links = [link for frame in output["links"] for link in frame if link["station_index"] == 0]

    assert set(output) == {
        "schema_version",
        "kind",
        "scenario",
        "provenance",
        "models",
        "warnings",
        "limitations",
        "timestamps_utc",
        "satellites",
        "stations",
        "links",
        "statistics",
    }
    assert output["schema_version"] == "1"
    assert output["kind"] == "openleo.constellation"
    assert len(links) == len(original.rows) == 40
    for actual, expected in zip(links, original.rows, strict=True):
        for field in ("azimuth_deg", "elevation_deg", "range_m", "range_rate_mps", "doppler_hz"):
            assert actual[field] == pytest.approx(getattr(expected, field), rel=1e-9, abs=1e-6)
        assert actual["delay_s"] == pytest.approx(expected.propagation_delay_s, rel=1e-9)
        assert actual["cn0_db_hz"] == pytest.approx(expected.carrier_to_noise_density_db_hz)
        assert actual["snr_db"] == pytest.approx(expected.signal_to_noise_ratio_db)
        assert actual["esn0_db"] == pytest.approx(
            expected.carrier_to_noise_density_db_hz - 59.03089987
        )
        assert actual["shannon_upper_bound_bps"] == pytest.approx(expected.capacity_upper_bound_bps)
        assert actual["rate_bps"] <= actual["shannon_upper_bound_bps"]
    for frame_index, frame in enumerate(output["links"]):
        for link in frame:
            satellite = output["satellites"][link["satellite_index"]]
            station = output["stations"][link["station_index"]]
            assert dist(
                satellite["positions_ecef_m"][frame_index], station["position_ecef_m"]
            ) == pytest.approx(link["range_m"], rel=0, abs=1e-5)
    assert links[0]["remaining_contact_s"] == 400.0
    assert links[-1]["remaining_contact_s"] == 10.0
    assert not any(link["contact_truncated"] for link in links)
    assert output["statistics"][0]["visible_sample_fraction"] == pytest.approx(40 / 61)
    assert output["statistics"][0]["fixed_baseline_integrated_bits"] == 400_000_000.0
    assert output["statistics"][0]["handover_count"] == 0
    assert output["provenance"]["scenario_sha256"] == "a" * 64
    assert not output["warnings"]
    json.dumps(output, allow_nan=False)


def test_visibility_is_distinct_from_rf_lock_and_contact_is_truncated():
    from openleo.constellation import simulate_constellation

    raw = _raw()
    raw["radio_link"]["eirp_dbw"] = -100.0
    raw["time_window"].update(start_utc="2026-08-30T06:16:00Z", stop_utc="2026-08-30T06:20:00Z")
    output = simulate_constellation(_scenario(raw))
    links = [link for frame in output["links"] for link in frame if link["station_index"] == 0]

    assert len(links) == 25
    assert all(link["modcod"] is None and link["rate_bps"] == 0.0 for link in links)
    assert all(link["margin_db"] < 0 and link["contact_truncated"] for link in links)
    assert links[0]["remaining_contact_s"] == 240.0
    assert links[-1]["remaining_contact_s"] == 0.0
    assert output["statistics"][0]["visible_sample_fraction"] == 1.0
    assert output["statistics"][0]["usable_sample_fraction"] == 0.0
    assert output["statistics"][0]["best_link_integrated_bits"] == 0.0
    assert output["statistics"][0]["fixed_baseline_integrated_bits"] == 240_000_000.0


def test_inclusive_utc_grid_uses_actual_last_interval_for_integration():
    from openleo.constellation import simulate_constellation

    raw = _raw()
    raw["time_window"].update(
        start_utc="2026-08-30T06:16:00Z", stop_utc="2026-08-30T06:20:00Z", step_s=19
    )
    raw["radio_link"]["eirp_dbw"] = 100.0
    output = simulate_constellation(_scenario(raw))

    assert len(output["timestamps_utc"]) == 14
    assert output["timestamps_utc"][-2:] == ["2026-08-30T06:19:48Z", "2026-08-30T06:20:00Z"]
    assert output["statistics"][0]["best_link_integrated_bits"] == pytest.approx(427_799_808.0)
    assert output["statistics"][0]["fixed_baseline_integrated_bits"] == 240_000_000.0
    assert output["links"][0][0]["rate_bps"] == pytest.approx(1_782_499.2)


def test_global_catalog_propagates_and_preserves_station_and_satellite_order():
    from openleo.constellation import load_constellation, simulate_constellation

    scenario = load_constellation(GLOBAL_SCENARIO)
    before = copy.deepcopy(scenario)
    output = simulate_constellation(scenario)

    assert scenario == before
    assert len(output["satellites"]) == 80
    assert len(output["timestamps_utc"]) == 121
    assert [station["name"] for station in output["stations"]] == [
        "Madrid",
        "Tromso",
        "Singapore",
        "Quito",
    ]
    assert [satellite["norad_id"] for satellite in output["satellites"]] == sorted(
        satellite["norad_id"] for satellite in output["satellites"]
    )
    assert all(len(satellite["positions_ecef_m"]) == 121 for satellite in output["satellites"])
    for frame in output["links"]:
        indices = [(link["station_index"], link["satellite_index"]) for link in frame]
        assert indices == sorted(indices)
        assert all(link["elevation_deg"] >= 10.0 for link in frame)
    assert any(record["handover_count"] > 0 for record in output["statistics"])
    assert output["models"]["coordinates"]["frame"] == "ITRS"
    assert output["models"]["time"]["leap_second_table_source"] == "skyfield-builtin"
    assert len(output["models"]["time"]["leap_second_table_sha256"]) == 64
    assert "Table 13" in output["models"]["adaptation"]["reference"]


def test_wgs84_station_coordinates_no_contact_and_stale_warning():
    from openleo.constellation import simulate_constellation

    raw = _raw()
    raw["stations"][0].update(latitude_deg=0.0, longitude_deg=0.0, height_m=0.0)
    raw["time_window"].update(
        start_utc="2026-09-20T00:00:00Z",
        stop_utc="2026-09-20T00:01:00Z",
        minimum_elevation_deg=89.9,
    )
    output = simulate_constellation(_scenario(raw))

    assert output["stations"][0]["position_ecef_m"] == pytest.approx([6_378_137.0, 0.0, 0.0])
    assert all(not frame for frame in output["links"])
    assert all(station["best_link_integrated_bits"] == 0 for station in output["statistics"])
    assert any("14 days" in warning and "25544" in warning for warning in output["warnings"])


def test_sgp4_propagation_failures_are_reported_with_norad_id_and_timestamp():
    from openleo.constellation import simulate_constellation

    raw = _raw()
    raw["time_window"].update(start_utc="2100-01-01T00:00:00Z", stop_utc="2100-01-01T00:01:00Z")
    with pytest.raises(ValueError, match="SGP4.*25544.*2100-01-01"):
        simulate_constellation(_scenario(raw))


def test_non_finite_derived_station_geometry_is_not_silently_treated_as_no_contact():
    from openleo.constellation import simulate_constellation

    raw = _raw()
    raw["stations"][0]["height_m"] = 1e308
    with pytest.raises(ValueError, match="geometry.*Cartagena"):
        simulate_constellation(_scenario(raw))
