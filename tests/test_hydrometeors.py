import json
from dataclasses import FrozenInstanceError
from importlib import import_module
from math import isclose
from pathlib import Path

import pytest

REFERENCE_CASES = Path("examples/atmosphere/hydrometeors_reference.json")


def _api():
    return import_module("openleo.hydrometeors")


def test_source_metadata_identifies_the_verified_official_pdfs() -> None:
    assert dict(_api().RAIN_SOURCE) == {
        "recommendation": "ITU-R P.838-3",
        "url": ("https://www.itu.int/dms_pubrec/itu-r/rec/p/r-rec-p.838-3-200503-i!!pdf-e.pdf"),
        "sha256": "3ab7482993e51fc63c5127a72e9e8930614e73652ac614882760817e7c1469cb",
    }
    assert dict(_api().CLOUD_SOURCE) == {
        "recommendation": "ITU-R P.840-9",
        "url": ("https://www.itu.int/dms_pubrec/itu-r/rec/p/R-REC-P.840-9-202308-I!!PDF-E.pdf"),
        "sha256": "cd554242549a4f3c5854133dce6f7f93dd86e04cfa8a20b5c1ff5fe9357f37b6",
    }
    with pytest.raises(TypeError):
        _api().RAIN_SOURCE["sha256"] = "changed"


@pytest.mark.parametrize(
    ("frequency_hz", "rain_rate_mm_h", "elevation_deg", "tilt_deg", "expected"),
    [
        (
            14.25e9,
            26.48052,
            31.076991235657,
            0.0,
            (0.0397548797329313, 1.12418042813791, 1.58130839366869),
        ),
        (
            29e9,
            26.48052,
            31.076991235657,
            0.0,
            (0.22106803684893, 0.953200051046016, 5.02180188909084),
        ),
        (
            14.25e9,
            63.62668149,
            48.2411705405115,
            90.0,
            (0.0422647354773402, 1.07871664199312, 3.72901263518665),
        ),
    ],
    ids=("p838-row-20", "p838-row-32", "p838-row-60"),
)
def test_rain_matches_official_workbook_cells(
    frequency_hz, rain_rate_mm_h, elevation_deg, tilt_deg, expected
) -> None:
    attenuation = _api().rain_specific_attenuation(
        frequency_hz, rain_rate_mm_h, elevation_deg, tilt_deg
    )

    assert isclose(attenuation.k, expected[0], rel_tol=1e-12, abs_tol=1e-15)
    assert isclose(attenuation.alpha, expected[1], rel_tol=1e-12, abs_tol=1e-15)
    assert isclose(
        attenuation.specific_attenuation_db_per_km,
        expected[2],
        rel_tol=1e-12,
        abs_tol=1e-14,
    )


@pytest.mark.parametrize(
    ("frequency_hz", "liquid_water_kg_m2", "elevation_deg", "expected"),
    [
        (6e9, 0.8235924623564901, 15.0, (0.031127781854533063, 0.09905224128740467)),
        (15e9, 0.22133683746466337, 45.0, (0.19011334907784644, 0.0595088161565868)),
        (45e9, 0.027846001703619755, 90.0, (1.4430598865763187, 0.0401834480600295)),
    ],
    ids=("p840-row-21", "p840-row-22", "p840-row-24"),
)
def test_cloud_matches_official_workbook_cells(
    frequency_hz, liquid_water_kg_m2, elevation_deg, expected
) -> None:
    attenuation = _api().cloud_slant_attenuation(frequency_hz, liquid_water_kg_m2, elevation_deg)

    assert isclose(
        attenuation.mass_absorption_db_per_kg_m2,
        expected[0],
        rel_tol=1e-12,
        abs_tol=1e-15,
    )
    assert isclose(attenuation.attenuation_db, expected[1], rel_tol=1e-12, abs_tol=1e-15)


@pytest.mark.parametrize(
    ("frequency_hz", "tilt_deg", "expected_k", "expected_alpha"),
    [
        (1e9, 0.0, 0.0000259, 0.9691),
        (10e9, 0.0, 0.01217, 1.2571),
        (10e9, 90.0, 0.01129, 1.2156),
        (1000e9, 90.0, 1.3822, 0.6365),
    ],
)
def test_rain_coefficients_match_p838_3_table_5_rounding(
    frequency_hz, tilt_deg, expected_k, expected_alpha
) -> None:
    attenuation = _api().rain_specific_attenuation(frequency_hz, 0.0, 0.0, tilt_deg)

    assert attenuation.k == pytest.approx(expected_k, abs=5e-8 if expected_k < 0.001 else 5e-5)
    assert attenuation.alpha == pytest.approx(expected_alpha, abs=5e-5)


def test_zero_hydrometeors_produce_zero_loss_and_frozen_results() -> None:
    rain = _api().rain_specific_attenuation(20e9, 0.0, 30.0, 45.0)
    cloud = _api().cloud_slant_attenuation(20e9, 0.0, 30.0)

    assert rain.specific_attenuation_db_per_km == 0.0
    assert cloud.attenuation_db == 0.0
    assert rain.k > 0.0 and rain.alpha > 0.0
    assert cloud.mass_absorption_db_per_kg_m2 > 0.0
    with pytest.raises(FrozenInstanceError):
        rain.k = 0.0
    with pytest.raises(FrozenInstanceError):
        cloud.attenuation_db = 1.0


def test_rain_loss_increases_with_rain_rate() -> None:
    losses = [
        _api().rain_specific_attenuation(20e9, rate, 30.0, 0.0).specific_attenuation_db_per_km
        for rate in (1.0, 10.0, 100.0)
    ]

    assert losses[0] < losses[1] < losses[2]


def test_cloud_loss_is_linear_in_liquid_water_and_lower_at_zenith() -> None:
    low = _api().cloud_slant_attenuation(30e9, 0.5, 30.0).attenuation_db
    doubled = _api().cloud_slant_attenuation(30e9, 1.0, 30.0).attenuation_db
    zenith = _api().cloud_slant_attenuation(30e9, 1.0, 90.0).attenuation_db

    assert doubled == pytest.approx(2.0 * low)
    assert zenith < doubled


def test_rain_polarization_reduces_to_hv_and_is_tilt_invariant_at_zenith() -> None:
    horizontal = _api().rain_specific_attenuation(20e9, 10.0, 0.0, 0.0)
    vertical = _api().rain_specific_attenuation(20e9, 10.0, 0.0, 90.0)
    zenith_h = _api().rain_specific_attenuation(20e9, 10.0, 90.0, 0.0)
    zenith_v = _api().rain_specific_attenuation(20e9, 10.0, 90.0, 90.0)

    assert horizontal.k != vertical.k
    assert horizontal.alpha != vertical.alpha
    assert zenith_h == zenith_v


@pytest.mark.parametrize(
    ("function_name", "args"),
    [
        ("rain_specific_attenuation", (True, 1.0, 30.0, 0.0)),
        ("rain_specific_attenuation", (20e9, float("nan"), 30.0, 0.0)),
        ("rain_specific_attenuation", (20e9, 1.0, float("inf"), 0.0)),
        ("rain_specific_attenuation", (20e9, 1.0, 30.0, "horizontal")),
        ("cloud_slant_attenuation", (True, 1.0, 30.0)),
        ("cloud_slant_attenuation", (20e9, float("nan"), 30.0)),
        ("cloud_slant_attenuation", (20e9, 1.0, float("inf"))),
    ],
)
def test_hydrometeor_inputs_reject_bools_non_numbers_and_nonfinite_values(
    function_name, args
) -> None:
    with pytest.raises(ValueError, match="finite and not a bool"):
        getattr(_api(), function_name)(*args)


@pytest.mark.parametrize(
    ("function_name", "args"),
    [
        ("rain_specific_attenuation", (10**400, 1.0, 30.0, 0.0)),
        ("rain_specific_attenuation", (20e9, 10**400, 30.0, 0.0)),
        ("cloud_slant_attenuation", (20e9, 10**400, 30.0)),
    ],
)
def test_hydrometeor_inputs_reject_huge_integers_as_nonfinite(function_name, args) -> None:
    with pytest.raises(ValueError, match="finite and not a bool"):
        getattr(_api(), function_name)(*args)


@pytest.mark.parametrize(
    ("function_name", "args", "message"),
    [
        ("rain_specific_attenuation", (1e9 - 1, 1.0, 30.0, 0.0), "frequency_hz"),
        ("rain_specific_attenuation", (1e12 + 1, 1.0, 30.0, 0.0), "frequency_hz"),
        ("rain_specific_attenuation", (20e9, -1.0, 30.0, 0.0), "rain_rate_mm_h"),
        ("rain_specific_attenuation", (20e9, 1.0, -1.0, 0.0), "elevation_deg"),
        ("rain_specific_attenuation", (20e9, 1.0, 91.0, 0.0), "elevation_deg"),
        ("rain_specific_attenuation", (20e9, 1.0, 30.0, -1.0), "polarization_tilt_deg"),
        ("rain_specific_attenuation", (20e9, 1.0, 30.0, 181.0), "polarization_tilt_deg"),
        ("cloud_slant_attenuation", (1e9 - 1, 1.0, 30.0), "frequency_hz"),
        ("cloud_slant_attenuation", (200e9 + 1, 1.0, 30.0), "frequency_hz"),
        ("cloud_slant_attenuation", (20e9, -1.0, 30.0), "liquid_water_kg_m2"),
        ("cloud_slant_attenuation", (20e9, 1.0, 4.9), "elevation_deg"),
        ("cloud_slant_attenuation", (20e9, 1.0, 91.0), "elevation_deg"),
    ],
)
def test_hydrometeor_inputs_enforce_model_domains(function_name, args, message) -> None:
    with pytest.raises(ValueError, match=message):
        getattr(_api(), function_name)(*args)


def test_hydrometeor_domains_include_documented_endpoints() -> None:
    _api().rain_specific_attenuation(1e9, 0.0, 0.0, 0.0)
    _api().rain_specific_attenuation(1e12, 0.0, 90.0, 180.0)
    _api().cloud_slant_attenuation(1e9, 0.0, 5.0)
    _api().cloud_slant_attenuation(200e9, 0.0, 90.0)


def test_hydrometeor_calculations_reject_nonfinite_outputs() -> None:
    with pytest.raises(ValueError, match="invalid attenuation"):
        _api().rain_specific_attenuation(20e9, 1e308, 30.0, 0.0)
    with pytest.raises(ValueError, match="invalid attenuation"):
        _api().cloud_slant_attenuation(200e9, 1e308, 30.0)


def test_reference_fixture_contains_only_tiny_replayable_inputs() -> None:
    reference = json.loads(REFERENCE_CASES.read_text(encoding="utf-8"))

    assert set(reference) == {"schema_version", "name", "rain_cases", "cloud_cases"}
    assert reference["schema_version"] == "1"
    assert len(reference["rain_cases"]) == len(reference["cloud_cases"]) == 3
    assert all(
        set(case)
        == {
            "id",
            "frequency_hz",
            "rain_rate_mm_h",
            "elevation_deg",
            "polarization_tilt_deg",
        }
        for case in reference["rain_cases"]
    )
    assert all(
        set(case) == {"id", "frequency_hz", "liquid_water_kg_m2", "elevation_deg"}
        for case in reference["cloud_cases"]
    )
    assert "expected" not in REFERENCE_CASES.read_text(encoding="utf-8").lower()
