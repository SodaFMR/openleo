from dataclasses import FrozenInstanceError
from itertools import pairwise
from math import exp, isfinite

import pytest

from openleo.reference_atmosphere import reference_atmosphere


# Literal cached values from ITU's P.676-13 validation workbook, sheet
# P.676-13 A_Gas_A1_2.2.1a, columns L/M/N/O/Q/R. The workbook labels the
# equivalent P.835-6 global profile; P.835-7 Annex 1 retains these equations.
# Workbook SHA256: e2d8d864c80f59752318548cdd75d818792b44574da6e41dbdc5cb722aab7546.
@pytest.mark.parametrize(
    ("height", "pressure", "temperature", "density", "vapour_pressure", "index"),
    [
        (
            0.00005,
            1013.2439934445522,
            288.14967500000256,
            7.4998125023437305,
            9.9726282192492022,
            1.0003177179887659,
        ),
        (
            5.0171241287354311,
            539.24921032551606,
            255.56441157757112,
            0.61038886346739474,
            0.71986004026556139,
            1.0001678558724172,
        ),
        (
            10.012605860033755,
            264.4897419867088,
            223.17041191047082,
            0.050217088116797905,
            0.05171651241333733,
            1.0000923555009416,
        ),
        (
            19.972175383755257,
            55.535201069072144,
            216.65,
            0.00034526971238344286,
            0.000345190047013719,
            1.0000198944249257,
        ),
        (
            30.099471954832175,
            11.793961127335542,
            226.60762174428086,
            2.2556623264663992e-05,
            2.3587922254671086e-05,
            1.0000040389210154,
        ),
        (
            39.828723847613219,
            2.938632021786836,
            249.87603913847704,
            5.0969397571433633e-6,
            5.8772640435736723e-6,
            1.000000912639055,
        ),
        (
            50.13100532173321,
            0.7849392541077961,
            270.65,
            1.2569468787375533e-06,
            1.569878508215592e-06,
            1.0000002250635598,
        ),
        (
            60.01967233112866,
            0.21901018488423474,
            246.96682769423828,
            3.8433912365892118e-7,
            4.3802036976846949e-7,
            1.0000000688183639,
        ),
        (
            74.791565553274452,
            0.024692612073467728,
            208.80634746326734,
            5.1252168349544738e-8,
            4.9385224146935448e-8,
            1.0000000091770922,
        ),
        (
            80.21528740714372,
            0.01015165630470888,
            198.21865185626345,
            2.219633622395562e-08,
            2.0303312609417763e-08,
            1.0000000039744334,
        ),
        (
            86.032273468265799,
            0.0037125872710230211,
            186.8673,
            8.6105772559531661e-9,
            7.425174542046041e-9,
            1.000000001541798,
        ),
        (
            90.44375260438714,
            0.0016972318966374327,
            186.8673,
            3.936377868159187e-09,
            3.3944637932748655e-09,
            1.0000000007048424,
        ),
        (
            91.352827429780362,
            0.0014449977599876825,
            186.87924561797652,
            3.3511588036848289e-9,
            2.889995519975365e-9,
            1.0000000006000538,
        ),
        (
            99.956851559396512,
            0.0003224699088496863,
            194.99808538009387,
            7.167170806986861e-10,
            6.449398176993726e-10,
            1.000000000128334,
        ),
    ],
)
def test_profile_matches_official_workbook_states(
    height, pressure, temperature, density, vapour_pressure, index
):
    state = reference_atmosphere(height)

    assert state.temperature_k == pytest.approx(temperature, rel=5e-13)
    assert state.total_pressure_hpa == pytest.approx(pressure, rel=5e-13)
    assert state.water_vapour_density_g_m3 == pytest.approx(density, rel=5e-13, abs=0)
    assert state.water_vapour_pressure_hpa == pytest.approx(vapour_pressure, rel=5e-13, abs=0)
    assert state.dry_pressure_hpa == pytest.approx(pressure - vapour_pressure, rel=5e-13)
    assert state.refractive_index == pytest.approx(index, abs=3e-16, rel=0)


def test_geometric_height_is_converted_to_geopotential_height():
    # Independent 50-digit Decimal evaluation of P.835-7 equations at Z=5 km.
    state = reference_atmosphere(5)

    assert state.temperature_k == pytest.approx(255.6755432218035055, rel=1e-13)
    assert state.total_pressure_hpa == pytest.approx(540.4828091231087921, rel=1e-13)
    assert state.dry_pressure_hpa == pytest.approx(539.7564434119807469, rel=1e-13)
    assert state.water_vapour_density_g_m3 == pytest.approx(0.6156374896792409638, rel=1e-13)
    assert state.water_vapour_pressure_hpa == pytest.approx(0.7263657111280451434, rel=1e-13)
    assert state.refractive_index == pytest.approx(1.0001681927036141407, abs=3e-16, rel=0)


def test_sea_level_uses_total_pressure_and_returns_an_immutable_state():
    state = reference_atmosphere(0)

    assert state.temperature_k == 288.15
    assert state.total_pressure_hpa == 1013.25
    assert state.water_vapour_density_g_m3 == 7.5
    assert state.water_vapour_pressure_hpa == pytest.approx(9.97288878634056)
    assert state.dry_pressure_hpa == pytest.approx(1003.2771112136594)
    with pytest.raises(FrozenInstanceError):
        state.temperature_k = 1.0


@pytest.mark.parametrize(
    ("geopotential_height", "lower_pressure", "upper_pressure"),
    [
        (20, 54.74934893, 54.74980),
        (32, 8.680329184, 8.680422),
        (47, 1.109092749, 1.109106),
        (51, 0.6694145988, 0.6694167),
        (71, 0.03956584013, 0.03956649),
    ],
)
def test_layer_boundary_keeps_published_lower_branch_and_rounded_pressure_jump(
    geopotential_height, lower_pressure, upper_pressure
):
    # P.835-7 (1b) maps each exact geopotential boundary to the API's geometric height.
    height = 6356.766 * geopotential_height / (6356.766 - geopotential_height)
    state = reference_atmosphere(height)
    just_above = reference_atmosphere(height + 1e-8)

    assert state.total_pressure_hpa == pytest.approx(lower_pressure, rel=5e-10, abs=0)
    assert just_above.total_pressure_hpa == pytest.approx(upper_pressure, rel=3e-9, abs=0)
    assert just_above.total_pressure_hpa > state.total_pressure_hpa


def test_eighty_six_km_uses_the_geometric_upper_profile_with_published_jump():
    below = reference_atmosphere(85.999999)
    boundary = reference_atmosphere(86)

    assert below.temperature_k == pytest.approx(186.9459103, rel=5e-10)
    assert boundary.temperature_k == 186.8673
    assert boundary.total_pressure_hpa == pytest.approx(0.003733965950, rel=5e-10, abs=0)
    assert below.total_pressure_hpa > boundary.total_pressure_hpa


@pytest.mark.parametrize("height", [30, 50, 86, 100])
def test_upper_atmosphere_retains_the_published_minimum_vapour_mixing_ratio(height):
    state = reference_atmosphere(height)

    assert state.water_vapour_pressure_hpa / state.total_pressure_hpa == pytest.approx(2e-6)
    assert state.water_vapour_density_g_m3 > 7.5 * exp(-height / 2)


def test_entire_altitude_domain_has_finite_physical_states_and_decreasing_pressure():
    states = tuple(reference_atmosphere(index / 10) for index in range(1001))

    for state in states:
        assert isfinite(state.temperature_k) and state.temperature_k > 0
        assert isfinite(state.refractive_index) and state.refractive_index > 1
        assert 0 < state.water_vapour_pressure_hpa < state.total_pressure_hpa
        assert 0 < state.dry_pressure_hpa < state.total_pressure_hpa
        assert state.dry_pressure_hpa + state.water_vapour_pressure_hpa == pytest.approx(
            state.total_pressure_hpa
        )
    assert all(a.total_pressure_hpa > b.total_pressure_hpa for a, b in pairwise(states))


@pytest.mark.parametrize(
    "height",
    [
        True,
        False,
        None,
        "5",
        1j,
        [],
        {},
        float("nan"),
        float("inf"),
        -float("inf"),
        -0.000001,
        100.000001,
        10**1000,
    ],
)
def test_rejects_non_real_non_finite_and_out_of_range_heights(height):
    with pytest.raises(ValueError, match="height_km"):
        reference_atmosphere(height)
