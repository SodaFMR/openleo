import json
from hashlib import sha256
from pathlib import Path

from openleo.input import load_scenario
from openleo.output import _float, _json_dumps, write_result
from openleo.simulation import simulate_scenario

FROZEN_SCENARIO = Path("examples/scenarios/iss_cartagena.json")
CSV_HEADER = (
    "timestamp_utc,azimuth_deg,elevation_deg,range_m,range_rate_mps,delay_s,"
    "doppler_hz,free_space_path_loss_db,received_carrier_power_dbw,"
    "noise_density_dbw_per_hz,carrier_to_noise_density_db_hz,signal_to_noise_ratio_db,"
    "shannon_capacity_upper_bound_bps"
)


def test_json_dumps_defaults_to_the_existing_12_significant_digit_format() -> None:
    value = {"nested": [9.97288878634056, 0.1234567890123456]}

    assert _json_dumps(value) == (
        '{\n  "nested": [\n    9.97288878634,\n    0.123456789012\n  ]\n}'
    )
    assert _json_dumps(value, significant_digits=12) == _json_dumps(value)


def test_float_defaults_to_the_existing_12_significant_digit_format() -> None:
    assert _float(9.97288878634056) == "9.97288878634"
    assert _float(9.97288878634056, significant_digits=15) == "9.97288878634056"


def test_write_result_creates_deterministic_trace_and_summary(tmp_path) -> None:
    result = simulate_scenario(load_scenario(FROZEN_SCENARIO))
    output_dir = tmp_path / "nested" / "outputs"

    csv_path, json_path = write_result(result, output_dir)
    first_csv = csv_path.read_bytes()
    first_json = json_path.read_bytes()
    write_result(result, output_dir)

    assert csv_path == output_dir / "trace.csv"
    assert json_path == output_dir / "summary.json"
    assert first_csv == csv_path.read_bytes()
    assert first_json == json_path.read_bytes()
    assert b"\r\n" not in first_csv
    assert first_csv.decode("utf-8").splitlines()[0] == CSV_HEADER
    assert len(first_csv.decode("utf-8").splitlines()) == 41

    summary = json.loads(first_json)
    assert first_json.endswith(b"\n")
    assert summary["schema_version"] == "1"
    assert summary["scenario_name"] == "iss-cartagena-s-band-free-space"
    assert summary["row_count"] == 40
    assert summary["sampled_aos_utc"] == "2026-08-30T06:15:30Z"
    assert summary["sampled_los_utc"] == "2026-08-30T06:22:00Z"
    assert summary["sampling_interval_s"] == 10.0
    assert summary["element_epoch_utc"] == "2026-08-30T11:57:48.556224Z"
    assert summary["start_element_age_days"] == -0.238756437778
    assert summary["stop_element_age_days"] == -0.231811993333
    assert summary["maximum_absolute_element_age_days"] == 0.238756437778
    assert summary["provenance"]["orbit"]["sha256"] == (
        "ddb21d9a4a3a4812ee397751555b9d55d2767f30c33190afba90e4bbb8db13f9"
    )
    assert (
        summary["provenance"]["scenario"]["sha256"]
        == sha256(FROZEN_SCENARIO.read_bytes()).hexdigest()
    )
    assert summary["provenance"]["time"]["leap_second_table_source"] == "skyfield-builtin"
    leap_fingerprint = summary["provenance"]["time"]["leap_second_table_sha256"]
    assert len(leap_fingerprint) == 64
    assert set(leap_fingerprint) <= set("0123456789abcdef")
    assert summary["inputs"] == {
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
    assert {"openleo-link", "skyfield", "sgp4"} <= summary["versions"].keys()
    assert summary["warnings"] == []
    assert summary["extrema"]["maximum_elevation_deg"] == 61.5799734359
    assert summary["extrema"]["minimum_range_m"] == 473676.7193
    assert summary["extrema"]["maximum_capacity_upper_bound_bps"] == 6336955.32414
    assert summary["integrated_capacity_upper_bound_bits"] == 1867325705.98
    assert summary["limitations"] == [
        "free-space-only propagation; no atmospheric gases, rain, cloud, fog, scintillation, or instantaneous weather",
        "geometric elevation only; no atmospheric refraction",
        "synthetic RF parameters; not a real commercial constellation performance claim",
        "Shannon-Hartley capacity is a theoretical upper bound, not throughput or achieved goodput",
        "sample-grid AOS/LOS estimates; true elevation-mask crossings are not interpolated",
        "SGP4 propagation from public mean elements; GP element age is a quality indicator, not a covariance or accuracy guarantee",
        "no calibrated-observation validation; SatNOGS observations are qualitative sanity checks unless a complete calibrated acquisition chain is documented",
    ]
