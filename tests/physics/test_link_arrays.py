"""Array forms of the link kernels reproduce the scalar v1 kernels element-wise."""

import numpy as np
import pytest

from openleo.physics.link import (
    carrier_to_noise_density_db_hz,
    doppler_shift_hz,
    free_space_path_loss_db,
    noise_density_dbw_per_hz,
    propagation_delay_s,
    received_carrier_power_dbw,
    shannon_capacity_upper_bound_bps,
    signal_to_noise_ratio_db,
)

SEED = 20261101
SAMPLES = 10_000


def _log_uniform(rng, low, high):
    return 10.0 ** rng.uniform(np.log10(low), np.log10(high), SAMPLES)


def _in_domain_cases():
    rng = np.random.default_rng(SEED)
    range_m = _log_uniform(rng, 1e3, 1e8)
    frequency_hz = _log_uniform(rng, 1e8, 3e11)
    bandwidth_hz = _log_uniform(rng, 1e3, 1e9)
    return (
        (propagation_delay_s, (range_m,)),
        (doppler_shift_hz, (frequency_hz, rng.uniform(-8e3, 8e3, SAMPLES))),
        (free_space_path_loss_db, (range_m, frequency_hz)),
        (
            received_carrier_power_dbw,
            (
                rng.uniform(-20.0, 80.0, SAMPLES),
                rng.uniform(-10.0, 70.0, SAMPLES),
                rng.uniform(100.0, 250.0, SAMPLES),
                rng.uniform(0.0, 20.0, SAMPLES),
            ),
        ),
        (noise_density_dbw_per_hz, (_log_uniform(rng, 1.0, 1e4),)),
        (
            carrier_to_noise_density_db_hz,
            (rng.uniform(-250.0, -50.0, SAMPLES), rng.uniform(-235.0, -180.0, SAMPLES)),
        ),
        (signal_to_noise_ratio_db, (rng.uniform(-20.0, 140.0, SAMPLES), bandwidth_hz)),
        (shannon_capacity_upper_bound_bps, (rng.uniform(-200.0, 400.0, SAMPLES), bandwidth_hz)),
    )


CASES = _in_domain_cases()


@pytest.mark.parametrize(("kernel", "arguments"), CASES, ids=[k.__name__ for k, _ in CASES])
def test_array_kernels_equal_scalar_kernels(kernel, arguments):
    result = kernel(*arguments)
    expected = np.array([kernel(*map(float, values)) for values in zip(*arguments, strict=True)])

    assert isinstance(result, np.ndarray)
    assert result.dtype == np.float64
    assert result.shape == (SAMPLES,)
    np.testing.assert_allclose(result, expected, rtol=0, atol=1e-12)
    assert type(kernel(*(float(values[0]) for values in arguments))) is float


@pytest.mark.parametrize(
    ("kernel", "arguments", "array_position"),
    [
        (doppler_shift_hz, (2.2e9, -6_000.0), 1),
        (free_space_path_loss_db, (1_000_000.0, 2.2e9), 0),
        (received_carrier_power_dbw, (10.0, 20.0, 161.0, 2.0), 2),
        (carrier_to_noise_density_db_hz, (-133.0, -205.0), 0),
        (signal_to_noise_ratio_db, (72.5, 1e6), 0),
        (shannon_capacity_upper_bound_bps, (12.5, 1e6), 0),
    ],
)
def test_one_array_argument_broadcasts_against_float_arguments(kernel, arguments, array_position):
    column = np.array([0.5, 1.0, 2.0]) * arguments[array_position]
    mixed = list(arguments)
    mixed[array_position] = column

    result = kernel(*mixed)

    expected = []
    for value in column:
        scalar = list(arguments)
        scalar[array_position] = float(value)
        expected.append(kernel(*scalar))
    np.testing.assert_allclose(result, expected, rtol=0, atol=1e-12)


def test_array_kernels_keep_the_input_shape():
    range_m = np.full((2, 3), 1_000_000.0)

    assert propagation_delay_s(range_m).shape == (2, 3)
    assert free_space_path_loss_db(range_m, 2.2e9).shape == (2, 3)


@pytest.mark.parametrize(
    ("kernel", "arguments", "message"),
    [
        (propagation_delay_s, ([1.0, 0.0],), "range_m must be positive and finite"),
        (propagation_delay_s, ([1.0, np.nan],), "range_m must be positive and finite"),
        (doppler_shift_hz, ([2e9, -1.0], [0.0, 0.0]), "carrier_frequency_hz must be positive"),
        (doppler_shift_hz, (2e9, [0.0, np.inf]), "range_rate_mps must be finite"),
        (free_space_path_loss_db, ([1.0, -1.0], 2e9), "range_m must be positive and finite"),
        (free_space_path_loss_db, (1.0, [2e9, 0.0]), "carrier_frequency_hz must be positive"),
        (received_carrier_power_dbw, ([1.0, np.nan], 0.0, 0.0, 0.0), "eirp_dbw must be finite"),
        (received_carrier_power_dbw, (1.0, [0.0, np.inf], 0.0, 0.0), "receiver_gain_dbi must be"),
        (received_carrier_power_dbw, (1.0, 0.0, [0.0, np.nan], 0.0), "path_loss_db must be finite"),
        (
            received_carrier_power_dbw,
            (1.0, 0.0, 0.0, [0.0, -1.0]),
            "miscellaneous_loss_db must be finite and non-negative",
        ),
        (noise_density_dbw_per_hz, ([200.0, 0.0],), "system_noise_temperature_k must be positive"),
        (carrier_to_noise_density_db_hz, ([1.0, np.nan], 0.0), "carrier_power_dbw must be finite"),
        (carrier_to_noise_density_db_hz, (1.0, [0.0, np.inf]), "noise_density_dbw_per_hz must be"),
        (signal_to_noise_ratio_db, ([1.0, np.nan], 1e6), "carrier_to_noise_density_db_hz must be"),
        (signal_to_noise_ratio_db, (1.0, [1e6, 0.0]), "channel_bandwidth_hz must be positive"),
        (shannon_capacity_upper_bound_bps, ([1.0, np.inf], 1e6), "signal_to_noise_ratio_db must"),
        (shannon_capacity_upper_bound_bps, (1.0, [1e6, -1e6]), "channel_bandwidth_hz must be"),
    ],
)
def test_array_validation_rejects_any_invalid_element_by_name(kernel, arguments, message):
    with pytest.raises(ValueError, match=message) as array_error:
        kernel(*(np.array(value) if isinstance(value, list) else value for value in arguments))
    with pytest.raises(ValueError) as scalar_error:
        kernel(*(value[-1] if isinstance(value, list) else value for value in arguments))

    assert str(array_error.value) == str(scalar_error.value)
