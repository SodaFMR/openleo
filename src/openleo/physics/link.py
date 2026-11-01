"""Pure link-budget physics equations.

Every kernel accepts Python floats or numpy arrays. Floats take the v1.0.0 scalar path
unchanged and return ``float``. If any argument is an ``ndarray``, the same equation is
evaluated element-wise in float64 with the same operation order, the arguments broadcast
together, and an array is returned. Validation then applies to every element and raises
the scalar kernel's ``ValueError`` message, which names the argument.
"""

from math import exp, isfinite, log, log1p, log10, pi

import numpy as np
from numpy.typing import NDArray

SPEED_OF_LIGHT_MPS = 299_792_458.0
BOLTZMANN_J_PER_K = 1.380_649e-23

type FloatOrArray = float | NDArray[np.float64]


def _positive_finite(value: float, name: str) -> None:
    if not isfinite(value) or value <= 0.0:
        raise ValueError(f"{name} must be positive and finite")


def _finite(value: float, name: str) -> None:
    if not isfinite(value):
        raise ValueError(f"{name} must be finite")


def _is_array(*values: object) -> bool:
    return any(isinstance(value, np.ndarray) for value in values)


def _positive_finite_array(value: FloatOrArray, name: str) -> NDArray[np.float64]:
    array = np.asarray(value, dtype=np.float64)
    if not np.all(np.isfinite(array) & (array > 0.0)):
        raise ValueError(f"{name} must be positive and finite")
    return array


def _finite_array(value: FloatOrArray, name: str) -> NDArray[np.float64]:
    array = np.asarray(value, dtype=np.float64)
    if not np.all(np.isfinite(array)):
        raise ValueError(f"{name} must be finite")
    return array


def propagation_delay_s(range_m: FloatOrArray) -> FloatOrArray:
    if _is_array(range_m):
        return _positive_finite_array(range_m, "range_m") / SPEED_OF_LIGHT_MPS
    _positive_finite(range_m, "range_m")
    return range_m / SPEED_OF_LIGHT_MPS


def doppler_shift_hz(
    carrier_frequency_hz: FloatOrArray, range_rate_mps: FloatOrArray
) -> FloatOrArray:
    if _is_array(carrier_frequency_hz, range_rate_mps):
        frequency = _positive_finite_array(carrier_frequency_hz, "carrier_frequency_hz")
        range_rate = _finite_array(range_rate_mps, "range_rate_mps")
        return -frequency * range_rate / SPEED_OF_LIGHT_MPS
    _positive_finite(carrier_frequency_hz, "carrier_frequency_hz")
    _finite(range_rate_mps, "range_rate_mps")
    return -carrier_frequency_hz * range_rate_mps / SPEED_OF_LIGHT_MPS


def free_space_path_loss_db(
    range_m: FloatOrArray, carrier_frequency_hz: FloatOrArray
) -> FloatOrArray:
    if _is_array(range_m, carrier_frequency_hz):
        distance = _positive_finite_array(range_m, "range_m")
        frequency = _positive_finite_array(carrier_frequency_hz, "carrier_frequency_hz")
        return 20.0 * np.log10(4.0 * pi * distance * frequency / SPEED_OF_LIGHT_MPS)
    _positive_finite(range_m, "range_m")
    _positive_finite(carrier_frequency_hz, "carrier_frequency_hz")
    return 20.0 * log10(4.0 * pi * range_m * carrier_frequency_hz / SPEED_OF_LIGHT_MPS)


def received_carrier_power_dbw(
    eirp_dbw: FloatOrArray,
    receiver_gain_dbi: FloatOrArray,
    path_loss_db: FloatOrArray,
    miscellaneous_loss_db: FloatOrArray,
) -> FloatOrArray:
    if _is_array(eirp_dbw, receiver_gain_dbi, path_loss_db, miscellaneous_loss_db):
        eirp = _finite_array(eirp_dbw, "eirp_dbw")
        gain = _finite_array(receiver_gain_dbi, "receiver_gain_dbi")
        path_loss = _finite_array(path_loss_db, "path_loss_db")
        miscellaneous = np.asarray(miscellaneous_loss_db, dtype=np.float64)
        if not np.all(np.isfinite(miscellaneous) & (miscellaneous >= 0.0)):
            raise ValueError("miscellaneous_loss_db must be finite and non-negative")
        return eirp + gain - path_loss - miscellaneous
    _finite(eirp_dbw, "eirp_dbw")
    _finite(receiver_gain_dbi, "receiver_gain_dbi")
    _finite(path_loss_db, "path_loss_db")
    if not isfinite(miscellaneous_loss_db) or miscellaneous_loss_db < 0.0:
        raise ValueError("miscellaneous_loss_db must be finite and non-negative")
    return eirp_dbw + receiver_gain_dbi - path_loss_db - miscellaneous_loss_db


def noise_density_dbw_per_hz(system_noise_temperature_k: FloatOrArray) -> FloatOrArray:
    if _is_array(system_noise_temperature_k):
        temperature = _positive_finite_array(
            system_noise_temperature_k, "system_noise_temperature_k"
        )
        return 10.0 * np.log10(BOLTZMANN_J_PER_K * temperature)
    _positive_finite(system_noise_temperature_k, "system_noise_temperature_k")
    return 10.0 * log10(BOLTZMANN_J_PER_K * system_noise_temperature_k)


def carrier_to_noise_density_db_hz(
    carrier_power_dbw: FloatOrArray,
    noise_density_dbw_per_hz: FloatOrArray,
) -> FloatOrArray:
    if _is_array(carrier_power_dbw, noise_density_dbw_per_hz):
        carrier = _finite_array(carrier_power_dbw, "carrier_power_dbw")
        noise = _finite_array(noise_density_dbw_per_hz, "noise_density_dbw_per_hz")
        return carrier - noise
    _finite(carrier_power_dbw, "carrier_power_dbw")
    _finite(noise_density_dbw_per_hz, "noise_density_dbw_per_hz")
    return carrier_power_dbw - noise_density_dbw_per_hz


def signal_to_noise_ratio_db(
    carrier_to_noise_density_db_hz: FloatOrArray,
    channel_bandwidth_hz: FloatOrArray,
) -> FloatOrArray:
    if _is_array(carrier_to_noise_density_db_hz, channel_bandwidth_hz):
        cn0 = _finite_array(carrier_to_noise_density_db_hz, "carrier_to_noise_density_db_hz")
        bandwidth = _positive_finite_array(channel_bandwidth_hz, "channel_bandwidth_hz")
        return cn0 - 10.0 * np.log10(bandwidth)
    _finite(carrier_to_noise_density_db_hz, "carrier_to_noise_density_db_hz")
    _positive_finite(channel_bandwidth_hz, "channel_bandwidth_hz")
    return carrier_to_noise_density_db_hz - 10.0 * log10(channel_bandwidth_hz)


def shannon_capacity_upper_bound_bps(
    signal_to_noise_ratio_db: FloatOrArray,
    channel_bandwidth_hz: FloatOrArray,
) -> FloatOrArray:
    if _is_array(signal_to_noise_ratio_db, channel_bandwidth_hz):
        snr = _finite_array(signal_to_noise_ratio_db, "signal_to_noise_ratio_db")
        bandwidth = _positive_finite_array(channel_bandwidth_hz, "channel_bandwidth_hz")
        return bandwidth * _softplus_array(snr * log(10.0) / 10.0) / log(2.0)
    _finite(signal_to_noise_ratio_db, "signal_to_noise_ratio_db")
    _positive_finite(channel_bandwidth_hz, "channel_bandwidth_hz")
    x = signal_to_noise_ratio_db * log(10.0) / 10.0
    softplus = x + log1p(exp(-x)) if x > 0.0 else log1p(exp(x))
    return channel_bandwidth_hz * softplus / log(2.0)


def _softplus_array(x: NDArray[np.float64]) -> NDArray[np.float64]:
    """The scalar kernel's two softplus branches, selected per element."""
    softplus = np.empty_like(x)
    positive = x > 0.0
    softplus[positive] = x[positive] + np.log1p(np.exp(-x[positive]))
    softplus[~positive] = np.log1p(np.exp(x[~positive]))
    return softplus
