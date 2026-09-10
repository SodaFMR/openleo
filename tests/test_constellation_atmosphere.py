"""Atmosphere opt-in, input boundaries and end-to-end RF/delay effects."""

from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from math import log10

import pytest
from test_constellation import ISS_SCENARIO, _raw

from openleo.constellation import parse_constellation, scenario_document, simulate_constellation


def reference_raw():
    original = _raw()
    return {
        **original,
        "stations": [
            original["stations"][0],
            {**original["stations"][0], "name": "Remote", "longitude_deg": -0.95},
        ],
        "time_window": {
            **original["time_window"],
            "start_utc": "2026-08-30T06:17:00Z",
            "stop_utc": "2026-08-30T06:17:20Z",
        },
        "radio_link": {**original["radio_link"], "carrier_frequency_hz": 28e9, "eirp_dbw": 30},
        "propagation": {
            "model": "itu_reference",
            "station_heights_amsl_m": {"Cartagena": 0.0, "Remote": 1300.0},
            "refinement": 1,
        },
    }


def reference_scenario(raw=None):
    return parse_constellation(reference_raw() if raw is None else raw, ISS_SCENARIO, "a" * 64)


def test_reference_configuration_round_trip_is_immutable_and_amsl_is_explicit():
    raw = reference_raw()
    saved = deepcopy(raw)
    scenario = reference_scenario(raw)
    assert scenario_document(scenario) == saved == raw
    assert scenario.stations[0].height_m == 20.0
    assert dict(scenario.propagation.station_heights_amsl_m)["Cartagena"] == 0.0
    with pytest.raises(FrozenInstanceError):
        scenario.propagation.refinement = 2
    raw["propagation"]["station_heights_amsl_m"]["Cartagena"] = 1000.0
    assert scenario_document(scenario)["propagation"] == saved["propagation"]


def test_omitted_atmosphere_retains_schema_one_and_does_not_build_a_column(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("omitted propagation must not calculate the atmosphere")

    monkeypatch.setattr("openleo.constellation.build_reference_column", forbidden)
    raw = {key: value for key, value in reference_raw().items() if key != "propagation"}
    scenario = reference_scenario(raw)
    assert scenario.propagation is None
    assert scenario_document(scenario) == raw
    result = simulate_constellation(scenario)
    assert result["schema_version"] == "1"
    assert "propagation" not in result["models"]
    assert any("No atmosphere" in item for item in result["limitations"])


@pytest.mark.parametrize(
    "propagation",
    [
        None,
        False,
        [],
        {},
        {
            "model": "weather",
            "station_heights_amsl_m": {"Cartagena": 0, "Remote": 0},
            "refinement": 1,
        },
        {
            "model": "itu_reference",
            "station_heights_amsl_m": {"Cartagena": 0, "Remote": 0},
            "refinement": 1,
            "profile": "local",
        },
    ],
)
def test_invalid_or_unknown_propagation_configuration_is_rejected(propagation):
    with pytest.raises(ValueError, match="propagation"):
        reference_scenario({**reference_raw(), "propagation": propagation})


@pytest.mark.parametrize("height", [-1, 10_001, float("nan"), float("inf"), True, "0", None])
def test_reference_heights_reject_invalid_values(height):
    raw = reference_raw()
    raw["propagation"]["station_heights_amsl_m"]["Cartagena"] = height
    with pytest.raises(ValueError, match="station_heights_amsl_m"):
        reference_scenario(raw)


@pytest.mark.parametrize(
    "heights", [{}, {"Cartagena": 0}, {"Cartagena": 0, "Unknown": 0}, [], None]
)
def test_reference_heights_require_exact_station_names(heights):
    raw = reference_raw()
    raw["propagation"]["station_heights_amsl_m"] = heights
    with pytest.raises(ValueError, match="station_heights_amsl_m"):
        reference_scenario(raw)


@pytest.mark.parametrize("refinement", [0, -1, 3, 8, True, 1.0, "1", None])
def test_reference_refinement_requires_supported_integer(refinement):
    raw = reference_raw()
    raw["propagation"]["refinement"] = refinement
    with pytest.raises(ValueError, match="refinement"):
        reference_scenario(raw)


@pytest.mark.parametrize("frequency", [1e9 - 1, 1e12 + 1])
def test_reference_frequency_domain_is_enforced_only_when_enabled(frequency):
    raw = reference_raw()
    raw["radio_link"]["carrier_frequency_hz"] = frequency
    with pytest.raises(ValueError, match="carrier_frequency_hz"):
        reference_scenario(raw)
    reference_scenario({key: value for key, value in raw.items() if key != "propagation"})


def test_reference_geometric_mask_domain_is_enforced_only_when_enabled():
    raw = reference_raw()
    raw["time_window"]["minimum_elevation_deg"] = 4.99
    with pytest.raises(ValueError, match="minimum_elevation_deg"):
        reference_scenario(raw)
    reference_scenario({key: value for key, value in raw.items() if key != "propagation"})


@pytest.mark.parametrize("refinement", [1, 2, 4])
def test_supported_profile_domain_endpoints_and_refinements(refinement):
    raw = reference_raw()
    raw["propagation"]["refinement"] = refinement
    raw["propagation"]["station_heights_amsl_m"] = {"Cartagena": 0, "Remote": 10_000}
    raw["time_window"]["minimum_elevation_deg"] = 5
    for frequency in (1e9, 1e12):
        raw["radio_link"]["carrier_frequency_hz"] = frequency
        assert scenario_document(reference_scenario(raw))["propagation"] == raw["propagation"]


def test_atmosphere_corrects_rf_adaptation_and_delay_without_changing_geometry(monkeypatch):
    from openleo.atmospheric_path import build_reference_column
    from openleo.propagation import PROPAGATION_LINK_FIELDS

    scenario = reference_scenario()
    vacuum = simulate_constellation(replace(scenario, propagation=None))
    calls = []

    def tracked(*args, **kwargs):
        calls.append((args, kwargs))
        return build_reference_column(*args, **kwargs)

    monkeypatch.setattr("openleo.constellation.build_reference_column", tracked)
    result = simulate_constellation(scenario)
    assert len(calls) == len(scenario.stations)
    assert result["schema_version"] == "2"
    assert result["satellites"] == vacuum["satellites"]
    assert result["stations"] == vacuum["stations"]
    assert result["timestamps_utc"] == vacuum["timestamps_utc"]
    assert not any("No atmosphere" in item for item in result["limitations"])
    assert "fixed" in result["models"]["propagation"]["noise"].lower()
    assert result["models"]["propagation"]["station_heights_amsl_m"] == {
        "Cartagena": 0.0,
        "Remote": 1300.0,
    }
    for frame, old_frame in zip(result["links"], vacuum["links"], strict=True):
        assert len(frame) == len(old_frame) > 0
        for link, old in zip(frame, old_frame, strict=True):
            assert set(link) - set(old) == set(PROPAGATION_LINK_FIELDS)
            for field in (
                "station_index",
                "satellite_index",
                "elevation_deg",
                "azimuth_deg",
                "range_m",
                "range_rate_mps",
                "doppler_hz",
                "remaining_contact_s",
                "contact_truncated",
            ):
                assert link[field] == old[field]
            assert link["gaseous_attenuation_db"] == pytest.approx(
                link["gaseous_dry_attenuation_db"] + link["gaseous_water_attenuation_db"]
            )
            assert link["gaseous_attenuation_db"] > 0
            assert link["free_space_cn0_db_hz"] == old["cn0_db_hz"]
            assert link["cn0_db_hz"] == old["cn0_db_hz"] - link["gaseous_attenuation_db"]
            assert link["snr_db"] == pytest.approx(
                link["cn0_db_hz"] - 10 * log10(scenario.radio_link.channel_bandwidth_hz)
            )
            assert link["esn0_db"] == pytest.approx(
                link["cn0_db_hz"] - 10 * log10(scenario.adaptation.symbol_rate_baud)
            )
            assert link["rate_bps"] <= old["rate_bps"]
            assert link["shannon_upper_bound_bps"] <= old["shannon_upper_bound_bps"]
            assert link["geometric_delay_s"] == old["delay_s"]
            assert link["atmospheric_excess_delay_s"] > 0
            assert link["delay_s"] == old["delay_s"] + link["atmospheric_excess_delay_s"]
            assert link["elevation_deg"] < link["apparent_elevation_deg"] <= 90


def test_reference_attenuation_reduces_selected_modcod_at_the_nominal_boundary():
    scenario = reference_scenario()
    initial = simulate_constellation(replace(scenario, propagation=None))["links"][0][0]
    radio = replace(
        scenario.radio_link, eirp_dbw=scenario.radio_link.eirp_dbw - initial["esn0_db"] + 1.01
    )
    scenario = replace(scenario, radio_link=radio)
    vacuum = simulate_constellation(replace(scenario, propagation=None))["links"][0][0]
    reference = simulate_constellation(scenario)["links"][0][0]
    assert vacuum["modcod"] == "QPSK 1/2"
    assert vacuum["rate_bps"] == pytest.approx(791086.4)
    assert reference["modcod"] == "QPSK 1/4"
    assert reference["rate_bps"] == pytest.approx(392194.4)
