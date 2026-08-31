import copy
import json
from dataclasses import FrozenInstanceError, replace
from hashlib import sha256
from math import inf, log10, nan
from pathlib import Path

import pytest

from openleo.input import load_scenario
from openleo.sensitivity import (
    SensitivityBaselineContext,
    SensitivityStudy,
    SensitivitySweep,
    load_sensitivity_study,
    run_sensitivity,
    write_sensitivity_result,
)

FROZEN_SCENARIO = Path("examples/scenarios/iss_cartagena.json")
CANONICAL_STUDY = Path("examples/sensitivity/iss_cartagena_oat.json")


def test_load_sensitivity_study_accepts_canonical_oat_file() -> None:
    scenario = load_scenario(FROZEN_SCENARIO)
    study = load_sensitivity_study(CANONICAL_STUDY, scenario)

    assert study.name == "iss-cartagena-free-space-oat"
    assert study.method == "deterministic_one_at_a_time"
    assert len(study.sweeps) == 5
    assert study.sweeps[0] == SensitivitySweep(
        parameter="time_window.step_s",
        unit="s",
        values=(1.0, 2.0, 5.0, 10.0, 20.0, 30.0, 60.0),
    )
    assert study.source_sha256 == sha256(CANONICAL_STUDY.read_bytes()).hexdigest()


def test_run_sensitivity_preserves_sweep_order_and_nominal_metrics() -> None:
    scenario = load_scenario(FROZEN_SCENARIO)
    study = load_sensitivity_study(CANONICAL_STUDY, scenario)

    result = run_sensitivity(scenario, study)

    assert len(result.cases) == 20
    assert tuple(case.parameter for case in result.cases[:7]) == ("time_window.step_s",) * 7
    nominal = tuple(case for case in result.cases if case.is_nominal)
    assert len(nominal) == 5
    assert all(case.metrics == result.baseline for case in nominal)
    assert all(case.metrics is result.baseline for case in nominal)
    assert isinstance(result.baseline_context, SensitivityBaselineContext)
    assert result.baseline_context.element_epoch_utc.isoformat() == (
        "2026-08-30T11:57:48.556224+00:00"
    )
    assert result.baseline_context.start_element_age_days == pytest.approx(-0.23875643777777777)
    assert result.baseline_context.stop_element_age_days == pytest.approx(-0.23181199333333333)
    assert result.baseline_context.maximum_absolute_element_age_days == pytest.approx(
        0.23875643777777777
    )
    assert result.baseline_context.leap_second_table_source == "skyfield-builtin"
    assert len(result.baseline_context.leap_second_table_sha256) == 64
    assert result.baseline_context.warnings == ()
    assert scenario.time_window.step_s == 10.0
    with pytest.raises(FrozenInstanceError):
        result.baseline.row_count = 0
    with pytest.raises(FrozenInstanceError):
        result.cases[0].case_value = 0.0
    with pytest.raises(FrozenInstanceError):
        result.baseline_context.warnings = ("changed",)


def test_run_sensitivity_preserves_radio_link_cn0_invariants() -> None:
    scenario = load_scenario(FROZEN_SCENARIO)
    study = SensitivityStudy(
        name="radio-link-invariants",
        method="deterministic_one_at_a_time",
        source_sha256="0" * 64,
        sweeps=(
            SensitivitySweep("radio_link.eirp_dbw", "dBW", (10.0, 11.0)),
            SensitivitySweep("radio_link.system_noise_temperature_k", "K", (300.0, 600.0)),
            SensitivitySweep("radio_link.miscellaneous_loss_db", "dB", (2.0, 3.0)),
        ),
    )

    result = run_sensitivity(scenario, study)
    eirp_plus_one = next(case for case in result.cases if case.case_value == 11.0)
    doubled_temperature = next(case for case in result.cases if case.case_value == 600.0)
    loss_plus_one = next(case for case in result.cases if case.case_value == 3.0)

    baseline_cn0 = result.baseline.maximum_carrier_to_noise_density_db_hz
    assert (
        eirp_plus_one.metrics.maximum_carrier_to_noise_density_db_hz - baseline_cn0
    ) == pytest.approx(1.0, abs=1e-9)
    assert (
        loss_plus_one.metrics.maximum_carrier_to_noise_density_db_hz - baseline_cn0
    ) == pytest.approx(-1.0, abs=1e-9)
    assert (
        doubled_temperature.metrics.maximum_carrier_to_noise_density_db_hz - baseline_cn0
    ) == pytest.approx(-10.0 * log10(2.0), abs=1e-9)


def test_run_sensitivity_matches_frozen_sampling_and_elevation_benchmarks() -> None:
    scenario = load_scenario(FROZEN_SCENARIO)
    result = run_sensitivity(scenario, load_sensitivity_study(CANONICAL_STUDY, scenario))

    relative_integrated_change = {
        (
            case.parameter,
            case.case_value,
        ): case.integrated_capacity_upper_bound_relative_change_percent
        for case in result.cases
    }

    expected_percentages = {
        ("time_window.step_s", 1.0): 1.39554703487,
        ("time_window.step_s", 20.0): -1.76263346877,
        ("time_window.step_s", 30.0): -0.0841572102157,
        ("time_window.step_s", 60.0): -5.78236557037,
        ("time_window.minimum_elevation_deg", 5.0): 17.2710744088,
        ("time_window.minimum_elevation_deg", 20.0): -29.9627342354,
        ("time_window.minimum_elevation_deg", 30.0): -50.0114931452,
    }
    for key, expected in expected_percentages.items():
        assert relative_integrated_change[key] == pytest.approx(expected, abs=1e-9)
    assert (
        relative_integrated_change[("time_window.step_s", 20.0)]
        < relative_integrated_change[("time_window.step_s", 30.0)]
    )


def test_run_sensitivity_adds_parameter_and_value_to_case_errors() -> None:
    scenario = load_scenario(FROZEN_SCENARIO)
    study = SensitivityStudy(
        name="unobservable-mask",
        method="deterministic_one_at_a_time",
        source_sha256="0" * 64,
        sweeps=(SensitivitySweep("time_window.minimum_elevation_deg", "deg", (10.0, 89.0)),),
    )

    with pytest.raises(
        ValueError,
        match=r"time_window\.minimum_elevation_deg=89\.0: no visible pass in time window",
    ):
        run_sensitivity(scenario, study)


def test_run_sensitivity_rejects_unsupported_nominal_parameter() -> None:
    scenario = load_scenario(FROZEN_SCENARIO)
    study = SensitivityStudy(
        name="unsupported-parameter",
        method="deterministic_one_at_a_time",
        source_sha256="0" * 64,
        sweeps=(SensitivitySweep("unsupported.parameter", "unit", (2.0,)),),
    )

    with pytest.raises(
        ValueError, match=r"unsupported sensitivity parameter 'unsupported.parameter'"
    ):
        run_sensitivity(scenario, study)


def _programmatic_invalid_studies() -> list[tuple[SensitivityStudy, str]]:
    valid = SensitivityStudy(
        name="programmatic-study",
        method="deterministic_one_at_a_time",
        source_sha256="0" * 64,
        sweeps=(SensitivitySweep("radio_link.eirp_dbw", "dBW", (10.0, 11.0)),),
    )
    excessive = (
        SensitivitySweep("time_window.step_s", "s", tuple(float(value) for value in range(1, 14))),
        SensitivitySweep(
            "time_window.minimum_elevation_deg",
            "deg",
            tuple(float(value) for value in range(13)),
        ),
        SensitivitySweep("radio_link.eirp_dbw", "dBW", tuple(float(value) for value in range(13))),
        SensitivitySweep(
            "radio_link.system_noise_temperature_k",
            "K",
            tuple(float(value) for value in range(294, 307)),
        ),
        SensitivitySweep(
            "radio_link.miscellaneous_loss_db",
            "dB",
            tuple(float(value) for value in range(13)),
        ),
    )
    return [
        (replace(valid, method="unsupported_method"), "method"),
        (replace(valid, source_sha256="not-a-sha256"), "source_sha256"),
        (replace(valid, sweeps=(replace(valid.sweeps[0], unit="dB"),)), "unit"),
        (replace(valid, sweeps=(valid.sweeps[0], valid.sweeps[0])), "duplicated"),
        (
            replace(valid, sweeps=(replace(valid.sweeps[0], values=(11.0, 10.0)),)),
            "strictly greater",
        ),
        (
            replace(valid, sweeps=(replace(valid.sweeps[0], values=(11.0, 12.0)),)),
            "scenario baseline",
        ),
        (replace(valid, sweeps=excessive), "65 cases"),
    ]


@pytest.mark.parametrize("index", range(7))
def test_run_sensitivity_rejects_invalid_programmatic_studies_before_simulation(
    monkeypatch, index: int
) -> None:
    study, message = _programmatic_invalid_studies()[index]

    def fail_if_called(_scenario):
        pytest.fail("invalid programmatic study reached simulation")

    monkeypatch.setattr("openleo.sensitivity.simulate_scenario", fail_if_called)
    with pytest.raises(ValueError, match=message):
        run_sensitivity(_scenario(), study)


def test_write_sensitivity_result_is_deterministic(tmp_path) -> None:
    scenario = load_scenario(FROZEN_SCENARIO)
    result = run_sensitivity(scenario, load_sensitivity_study(CANONICAL_STUDY, scenario))
    zero_baseline = replace(
        result.baseline,
        maximum_shannon_capacity_upper_bound_bps=0.0,
        integrated_shannon_capacity_upper_bound_bits=0.0,
    )
    zero_case = replace(
        result.cases[0],
        maximum_capacity_upper_bound_relative_change_percent=None,
        integrated_capacity_upper_bound_relative_change_percent=None,
    )
    result = replace(result, baseline=zero_baseline, cases=(zero_case, *result.cases[1:]))

    first_csv, first_summary = write_sensitivity_result(result, tmp_path / "first")
    second_csv, second_summary = write_sensitivity_result(result, tmp_path / "second")

    csv_bytes = first_csv.read_bytes()
    assert csv_bytes == second_csv.read_bytes()
    assert first_summary.read_bytes() == second_summary.read_bytes()
    assert b"\r" not in csv_bytes
    assert csv_bytes.count(b"\n") == 21
    lines = csv_bytes.decode("utf-8").splitlines()
    assert lines[0] == (
        "parameter,unit,baseline_value,case_value,is_nominal,row_count,sampled_aos_utc,"
        "sampled_los_utc,sampled_duration_s,maximum_elevation_deg,minimum_range_m,"
        "maximum_carrier_to_noise_density_db_hz,maximum_shannon_capacity_upper_bound_bps,"
        "integrated_shannon_capacity_upper_bound_bits,maximum_capacity_upper_bound_delta_bps,"
        "maximum_capacity_upper_bound_relative_change_percent,"
        "integrated_capacity_upper_bound_delta_bits,"
        "integrated_capacity_upper_bound_relative_change_percent"
    )
    assert ",true," in csv_bytes.decode("utf-8")
    assert ",false," in csv_bytes.decode("utf-8")
    assert lines[1].split(",")[15] == ""
    assert lines[1].split(",")[17] == ""

    summary = json.loads(first_summary.read_text(encoding="utf-8"))
    assert set(summary) == {
        "schema_version",
        "study_name",
        "method",
        "provenance",
        "sweeps",
        "baseline_inputs",
        "baseline_context",
        "baseline",
        "sweep_count",
        "case_count",
        "versions",
        "ordering",
        "numeric_format",
        "limitations",
    }
    assert summary["provenance"] == {
        "scenario": {"sha256": scenario.source_sha256},
        "sensitivity": {"sha256": result.study.source_sha256},
    }
    assert summary["baseline_inputs"] == {
        "scenario_name": "iss-cartagena-s-band-free-space",
        "orbit": {
            "source_url": "https://celestrak.org/NORAD/elements/gp.php?CATNR=25544&FORMAT=CSV",
            "retrieved_at_utc": "2026-08-30T22:01:11Z",
            "terms_url": "https://celestrak.org/usage-policy.php",
            "sha256": "ddb21d9a4a3a4812ee397751555b9d55d2767f30c33190afba90e4bbb8db13f9",
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
            "carrier_frequency_hz": 2_200_000_000.0,
            "channel_bandwidth_hz": 1_000_000.0,
            "eirp_dbw": 10.0,
            "receiver_gain_dbi": 20.0,
            "system_noise_temperature_k": 300.0,
            "miscellaneous_loss_db": 2.0,
        },
    }
    assert summary["baseline_context"] == {
        "element_epoch_utc": "2026-08-30T11:57:48.556224Z",
        "start_element_age_days": pytest.approx(-0.23875643777777777),
        "stop_element_age_days": pytest.approx(-0.23181199333333333),
        "maximum_absolute_element_age_days": pytest.approx(0.23875643777777777),
        "leap_second_table_source": "skyfield-builtin",
        "leap_second_table_sha256": result.baseline_context.leap_second_table_sha256,
        "warnings": [],
    }
    assert set(summary["baseline"]) == {
        "row_count",
        "sampled_aos_utc",
        "sampled_los_utc",
        "sampled_duration_s",
        "maximum_elevation_deg",
        "minimum_range_m",
        "maximum_carrier_to_noise_density_db_hz",
        "maximum_shannon_capacity_upper_bound_bps",
        "integrated_shannon_capacity_upper_bound_bits",
    }
    assert summary["baseline"]["maximum_shannon_capacity_upper_bound_bps"] == 0.0
    assert summary["baseline"]["integrated_shannon_capacity_upper_bound_bits"] == 0.0
    assert summary["sweep_count"] == 5
    assert summary["case_count"] == 20


def _valid_study() -> dict:
    return json.loads(CANONICAL_STUDY.read_text(encoding="utf-8"))


def _write_study(tmp_path, payload: object) -> Path:
    path = tmp_path / "study.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _scenario():
    return load_scenario(FROZEN_SCENARIO)


def test_sensitivity_types_are_immutable() -> None:
    sweep = SensitivitySweep("radio_link.eirp_dbw", "dBW", (7.0, 10.0, 13.0))
    study = SensitivityStudy("reference", "deterministic_one_at_a_time", "0" * 64, (sweep,))

    with pytest.raises(FrozenInstanceError):
        sweep.unit = "dB"
    with pytest.raises(FrozenInstanceError):
        study.name = "changed"


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ([], "study must be a JSON object"),
        ({}, "method is required"),
        ({**_valid_study(), "extra": True}, "extra is not allowed"),
        ({**_valid_study(), "schema_version": "2"}, "schema_version"),
        ({**_valid_study(), "method": "unsupported_method"}, "method"),
        ({**_valid_study(), "name": ""}, "name"),
        ({**_valid_study(), "name": "x" * 129}, "name"),
    ],
)
def test_rejects_invalid_study_envelope(tmp_path, payload, message) -> None:
    with pytest.raises(ValueError) as exc_info:
        load_sensitivity_study(_write_study(tmp_path, payload), _scenario())
    assert message in str(exc_info.value)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda study: study.update(sweeps=[]), "sweeps"),
        (
            lambda study: study.update(
                sweeps=study["sweeps"] + [copy.deepcopy(study["sweeps"][0])] * 6
            ),
            "sweeps",
        ),
        (lambda study: study["sweeps"][0].update(extra=True), "sweeps[0].extra"),
        (lambda study: study["sweeps"][0].update(parameter="unsupported"), "sweeps[0].parameter"),
        (
            lambda study: study["sweeps"].append(copy.deepcopy(study["sweeps"][0])),
            "parameter",
        ),
        (lambda study: study["sweeps"][0].update(values=[10.0]), "sweeps[0].values"),
        (
            lambda study: study["sweeps"][0].update(values=list(range(21))),
            "sweeps[0].values",
        ),
        (lambda study: study["sweeps"][0].update(values=[1.0, True, 10.0]), "sweeps[0].values[1]"),
        (lambda study: study["sweeps"][0].update(values=[1.0, "2", 10.0]), "sweeps[0].values[1]"),
        (lambda study: study["sweeps"][0].update(values=[1.0, nan, 10.0]), "sweeps[0].values[1]"),
        (lambda study: study["sweeps"][0].update(values=[1.0, inf, 10.0]), "sweeps[0].values[1]"),
        (lambda study: study["sweeps"][0].update(values=[1.0, 10.0, 5.0]), "sweeps[0].values[2]"),
        (lambda study: study["sweeps"][0].update(values=[1.0, 10.0, 10.0]), "sweeps[0].values[2]"),
        (lambda study: study["sweeps"][0].update(values=[1.0, 2.0]), "sweeps[0].values"),
    ],
)
def test_rejects_invalid_sweep_structure(tmp_path, mutate, message) -> None:
    payload = _valid_study()
    mutate(payload)

    with pytest.raises(ValueError) as exc_info:
        load_sensitivity_study(_write_study(tmp_path, payload), _scenario())
    assert message in str(exc_info.value)


@pytest.mark.parametrize(
    ("parameter", "values", "message"),
    [
        ("time_window.step_s", [0.0, 10.0], "time_window.step_s"),
        ("time_window.step_s", [1e-7, 10.0], "time_window.step_s"),
        ("time_window.step_s", [10.0, 601.0], "time_window.step_s"),
        ("time_window.minimum_elevation_deg", [-1.0, 10.0], "minimum_elevation_deg"),
        ("time_window.minimum_elevation_deg", [10.0, 90.0], "minimum_elevation_deg"),
        ("radio_link.system_noise_temperature_k", [0.0, 300.0], "system_noise_temperature_k"),
        ("radio_link.miscellaneous_loss_db", [-1.0, 2.0], "miscellaneous_loss_db"),
    ],
)
def test_rejects_parameter_domain_boundaries(tmp_path, parameter, values, message) -> None:
    payload = _valid_study()
    payload["sweeps"][0].update(parameter=parameter, values=values)

    with pytest.raises(ValueError, match=message):
        load_sensitivity_study(_write_study(tmp_path, payload), _scenario())


def test_rejects_step_s_that_exceeds_the_inclusive_sample_limit(tmp_path) -> None:
    payload = _valid_study()
    payload["sweeps"][0]["values"] = [0.005, 10.0]

    with pytest.raises(ValueError, match=r"sweeps\[0\]\.values\[0\].*120001.*100000"):
        load_sensitivity_study(_write_study(tmp_path, payload), _scenario())


def test_eirp_accepts_finite_negative_values_and_rejects_non_finite_values(tmp_path) -> None:
    payload = _valid_study()
    payload["sweeps"][2]["values"] = [-10.0, 10.0]

    study = load_sensitivity_study(_write_study(tmp_path, payload), _scenario())

    assert study.sweeps[2].values == (-10.0, 10.0)

    payload["sweeps"][2]["values"] = [nan, 10.0]
    with pytest.raises(ValueError, match=r"sweeps\[2\]\.values\[0\].*finite"):
        load_sensitivity_study(_write_study(tmp_path, payload), _scenario())


def test_rejects_more_than_64_declared_cases(tmp_path) -> None:
    payload = _valid_study()
    payload["sweeps"] = [
        {
            "parameter": "time_window.step_s",
            "values": [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0, 11.0, 12.0, 13.0],
        },
        {
            "parameter": "time_window.minimum_elevation_deg",
            "values": [0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0, 11.0, 12.0],
        },
        {
            "parameter": "radio_link.eirp_dbw",
            "values": [0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0, 11.0, 12.0],
        },
        {
            "parameter": "radio_link.system_noise_temperature_k",
            "values": [
                290.0,
                291.0,
                292.0,
                293.0,
                294.0,
                295.0,
                296.0,
                297.0,
                298.0,
                299.0,
                300.0,
                301.0,
                302.0,
            ],
        },
        {
            "parameter": "radio_link.miscellaneous_loss_db",
            "values": [0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0, 5.5, 6.0],
        },
    ]

    with pytest.raises(ValueError, match="65"):
        load_sensitivity_study(_write_study(tmp_path, payload), _scenario())


def test_rejects_oversized_invalid_utf8_and_missing_study_files(tmp_path) -> None:
    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(b" " * 1_000_001)
    invalid_utf8 = tmp_path / "invalid-utf8.json"
    invalid_utf8.write_bytes(b"\xff")

    for path, message in (
        (oversized, "1000000"),
        (invalid_utf8, "UTF-8"),
        (tmp_path / "missing.json", "could not load sensitivity study"),
    ):
        with pytest.raises(ValueError, match=message):
            load_sensitivity_study(path, _scenario())
