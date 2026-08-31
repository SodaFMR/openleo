from math import isfinite

import pytest

from openleo.physics import (
    carrier_to_noise_density_db_hz,
    doppler_shift_hz,
    free_space_path_loss_db,
    noise_density_dbw_per_hz,
    propagation_delay_s,
    received_carrier_power_dbw,
    shannon_capacity_upper_bound_bps,
    signal_to_noise_ratio_db,
)


def test_reference_delay_doppler_and_fspl() -> None:
    assert propagation_delay_s(1_000_000.0) == pytest.approx(0.0033356409519815205, rel=1e-12)
    assert doppler_shift_hz(2.2e9, -6_000.0) == pytest.approx(44_030.46056615607, rel=1e-12)
    assert free_space_path_loss_db(1_000_000.0, 2.2e9) == pytest.approx(
        159.2962368383275, rel=1e-12
    )


def test_reference_receiver_chain() -> None:
    carrier_dbw = received_carrier_power_dbw(10.0, 20.0, 161.0, 2.0)
    assert carrier_dbw == -133.0
    noise_dbw_hz = noise_density_dbw_per_hz(200.0)
    assert noise_dbw_hz == pytest.approx(-205.58886721657785, abs=1e-9)
    cn0_db_hz = carrier_to_noise_density_db_hz(carrier_dbw, noise_dbw_hz)
    assert cn0_db_hz == pytest.approx(72.58886721657785, abs=1e-9)
    snr_db = signal_to_noise_ratio_db(cn0_db_hz, 1e6)
    assert snr_db == pytest.approx(12.588867216577847, abs=1e-9)


def test_charter_receiver_chain_matches_hand_derived_cn0() -> None:
    carrier_dbw = received_carrier_power_dbw(
        10.0,
        23.010299956639813,
        161.0,
        0.0,
    )
    noise_dbw_hz = noise_density_dbw_per_hz(200.0)

    assert carrier_to_noise_density_db_hz(carrier_dbw, noise_dbw_hz) == pytest.approx(
        77.59916717321767,
        abs=1e-12,
    )


def test_shannon_bound_at_zero_db_snr_equals_bandwidth() -> None:
    assert shannon_capacity_upper_bound_bps(0.0, 1e6) == pytest.approx(1e6)


@pytest.mark.parametrize(
    ("snr_db", "expected_bps"),
    [
        (-200.0, 1.4426950408889634e-14),
        (4000.0, 1.3287712379549448e9),
    ],
)
def test_shannon_bound_remains_finite_at_extreme_snr(snr_db, expected_bps) -> None:
    capacity_bps = shannon_capacity_upper_bound_bps(snr_db, 1e6)

    assert isfinite(capacity_bps)
    assert capacity_bps > 0.0
    assert capacity_bps == pytest.approx(
        expected_bps,
        rel=1e-12,
        abs=0.0,
    )


@pytest.mark.parametrize(
    ("function", "args"),
    [
        (propagation_delay_s, (0.0,)),
        (free_space_path_loss_db, (1_000.0, 0.0)),
        (noise_density_dbw_per_hz, (-1.0,)),
        (signal_to_noise_ratio_db, (10.0, 0.0)),
        (shannon_capacity_upper_bound_bps, (10.0, 0.0)),
    ],
)
def test_physics_rejects_non_positive_physical_inputs(function, args) -> None:
    with pytest.raises(ValueError, match="must be positive and finite"):
        function(*args)
