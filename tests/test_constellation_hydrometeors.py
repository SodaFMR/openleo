"""Coupled RF, immutable scenario parsing and schema 3 artifacts."""

import csv
import json
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from functools import lru_cache
from hashlib import sha256
from io import StringIO

import pytest
from test_constellation_atmosphere import reference_raw, reference_scenario

from openleo.constellation import scenario_document, simulate_constellation
from openleo.network import add_network
from openleo.workbench import links_csv, load_experiment, render_workbench, write_workbench

HYDRO_FIELDS = (
    "cloud_attenuation_db",
    "rain_specific_attenuation_db_per_km",
    "rain_path_length_m",
    "rain_attenuation_db",
    "hydrometeor_attenuation_db",
    "total_atmospheric_attenuation_db",
)


def hydrometeor_raw(liquid=0.5, rain=25.0):
    raw = reference_raw()
    raw["propagation"]["hydrometeors"] = {
        "model": "declared_uniform_layers",
        "stations": {
            name: {
                "liquid_water_kg_m2": liquid,
                "rain_rate_mm_h": rain,
                "rain_top_height_amsl_m": 5000.0,
                "polarization_tilt_deg": 0.0,
            }
            for name in ("Cartagena", "Remote")
        },
    }
    return raw


def test_hydrometeor_configuration_round_trip_is_deeply_immutable():
    raw = hydrometeor_raw()
    saved = deepcopy(raw)
    scenario = reference_scenario(raw)
    assert scenario_document(scenario) == saved
    hydro = scenario.propagation.hydrometeors
    with pytest.raises(FrozenInstanceError):
        hydro.model = "weather"
    with pytest.raises(FrozenInstanceError):
        hydro.stations[0][1].rain_rate_mm_h = 0.0
    raw["propagation"]["hydrometeors"]["stations"]["Cartagena"]["rain_rate_mm_h"] = 0
    assert scenario_document(scenario) == saved


@pytest.mark.parametrize(
    "field,value",
    [
        ("liquid_water_kg_m2", -1),
        ("liquid_water_kg_m2", True),
        ("rain_rate_mm_h", -1),
        ("rain_rate_mm_h", float("nan")),
        ("rain_rate_mm_h", 10**400),
        ("rain_top_height_amsl_m", 20001),
        ("rain_top_height_amsl_m", 1299),
        ("polarization_tilt_deg", -1),
        ("polarization_tilt_deg", 181),
        ("polarization_tilt_deg", "0"),
    ],
)
def test_station_hydrometeor_domains_are_checked(field, value):
    raw = hydrometeor_raw()
    raw["propagation"]["hydrometeors"]["stations"]["Remote"][field] = value
    with pytest.raises(ValueError):
        reference_scenario(raw)


@pytest.mark.parametrize(
    "change",
    [
        "model",
        "missing_station",
        "extra_station",
        "extra_key",
        "missing_key",
        "null",
        "station_type",
    ],
)
def test_hydrometeor_parser_requires_exact_keys_and_all_stations(change):
    raw = hydrometeor_raw()
    hydro = raw["propagation"]["hydrometeors"]
    if change == "model":
        hydro["model"] = "p618_weather"
    elif change == "missing_station":
        del hydro["stations"]["Remote"]
    elif change == "extra_station":
        hydro["stations"]["Unknown"] = hydro["stations"]["Remote"]
    elif change == "extra_key":
        hydro["stations"]["Remote"]["weather"] = 1
    elif change == "missing_key":
        del hydro["stations"]["Remote"]["rain_rate_mm_h"]
    elif change == "station_type":
        hydro["stations"]["Remote"] = []
    else:
        raw["propagation"]["hydrometeors"] = None
    with pytest.raises(ValueError, match="hydrometeor"):
        reference_scenario(raw)


def test_cloud_domain_does_not_shrink_legacy_gaseous_domain():
    raw = hydrometeor_raw()
    raw["radio_link"]["carrier_frequency_hz"] = 200e9 + 1
    with pytest.raises(ValueError):
        reference_scenario(raw)
    del raw["propagation"]["hydrometeors"]
    reference_scenario(raw)


def test_zero_hydrometeors_preserve_every_legacy_link_value():
    legacy = simulate_constellation(reference_scenario())
    result = simulate_constellation(reference_scenario(hydrometeor_raw(0.0, 0.0)))
    assert result["schema_version"] == "3"
    for frame, old_frame in zip(result["links"], legacy["links"], strict=True):
        for link, old in zip(frame, old_frame, strict=True):
            assert {key: link[key] for key in old} == old
            assert set(link) - set(old) == set(HYDRO_FIELDS)
            assert link["hydrometeor_attenuation_db"] == 0.0


def test_losses_are_subtracted_before_snr_modcod_and_leave_delay_fixed():
    scenario = reference_scenario()
    initial = simulate_constellation(scenario)["links"][0][0]
    radio = replace(
        scenario.radio_link, eirp_dbw=scenario.radio_link.eirp_dbw - initial["esn0_db"] + 1.01
    )
    legacy = simulate_constellation(replace(scenario, radio_link=radio))["links"][0][0]
    hydro = reference_scenario(hydrometeor_raw(1.0, 25.0))
    changed = simulate_constellation(replace(hydro, radio_link=radio))["links"][0][0]
    assert legacy["modcod"] == "QPSK 1/2"
    assert changed["rate_bps"] < legacy["rate_bps"]
    assert (
        changed["cn0_db_hz"]
        == legacy["free_space_cn0_db_hz"] - changed["total_atmospheric_attenuation_db"]
    )
    assert (
        changed["total_atmospheric_attenuation_db"]
        == changed["gaseous_attenuation_db"] + changed["hydrometeor_attenuation_db"]
    )
    for field in (
        "delay_s",
        "geometric_delay_s",
        "atmospheric_excess_delay_s",
        "doppler_hz",
        "elevation_deg",
    ):
        assert changed[field] == legacy[field]


@lru_cache(maxsize=1)
def computed_document():
    return add_network(simulate_constellation(reference_scenario(hydrometeor_raw())))


def test_schema_three_bundle_and_csv_round_trip_and_hash_validation(tmp_path):
    source = deepcopy(computed_document())
    write_workbench(source, tmp_path)
    assert load_experiment(tmp_path) == source
    rows = csv.DictReader(StringIO(links_csv(source)))
    assert rows.fieldnames[-6:] == list(HYDRO_FIELDS)
    row = next(rows)
    for field in HYDRO_FIELDS:
        assert float(row[field]) == pytest.approx(source["links"][0][0][field])
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    for name, digest in manifest["files"].items():
        assert sha256((tmp_path / name).read_bytes()).hexdigest() == digest
    (tmp_path / "links.csv").write_text("corrupt")
    with pytest.raises(ValueError, match="SHA-256"):
        load_experiment(tmp_path)


@pytest.mark.parametrize("field", HYDRO_FIELDS + ("cn0_db_hz", "snr_db", "esn0_db"))
def test_schema_three_rejects_inconsistent_quantities(field):
    source = deepcopy(computed_document())
    source["links"][0][0][field] += 1.0
    with pytest.raises(ValueError, match="inconsistent"):
        render_workbench(source)


@pytest.mark.parametrize("schema", ["1", "2"])
def test_legacy_schema_cannot_hide_hydrometeor_configuration(schema):
    with pytest.raises(ValueError, match="propagation"):
        render_workbench({**computed_document(), "schema_version": schema})


@pytest.mark.parametrize("field", HYDRO_FIELDS)
@pytest.mark.parametrize("value", [True, float("inf"), -1.0, "1"])
def test_schema_three_requires_nonnegative_finite_numeric_units(field, value):
    source = deepcopy(computed_document())
    source["links"][0][0][field] = value
    with pytest.raises(ValueError, match=field):
        render_workbench(source)


def test_hydrometeor_metadata_and_canonical_scenario_hash_are_validated():
    source = deepcopy(computed_document())
    source["models"]["propagation"]["hydrometeor_path"]["units"][
        "rain_specific_attenuation_db_per_km"
    ] = "dB"
    with pytest.raises(ValueError, match="propagation"):
        render_workbench(source)
    source = deepcopy(computed_document())
    canonical = json.dumps(
        source["scenario"], sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    source["provenance"].update(
        scenario_hash_encoding="canonical JSON sorted compact UTF-8",
        scenario_canonical_json=canonical,
        scenario_sha256=sha256(canonical.encode()).hexdigest(),
    )
    render_workbench(source)
    source["provenance"]["scenario_canonical_json"] += " "
    with pytest.raises(ValueError, match="scenario_sha256"):
        render_workbench(source)


def test_public_propagation_config_rejects_mutable_hydrometeors_and_station_mismatch():
    scenario = reference_scenario(hydrometeor_raw())
    with pytest.raises(ValueError, match="hydrometeor"):
        replace(scenario.propagation, hydrometeors={})
    with pytest.raises(ValueError, match="hydrometeor"):
        replace(scenario.propagation, station_heights_amsl_m=(("Unknown", 0.0),))
