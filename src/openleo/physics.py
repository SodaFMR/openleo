"""Pure link-budget physics equations."""

from math import exp, isfinite, log, log1p, log10, pi

SPEED_OF_LIGHT_MPS = 299_792_458.0
BOLTZMANN_J_PER_K = 1.380_649e-23


def _positive_finite(value: float, name: str) -> None:
    if not isfinite(value) or value <= 0.0:
        raise ValueError(f"{name} must be positive and finite")


def _finite(value: float, name: str) -> None:
    if not isfinite(value):
        raise ValueError(f"{name} must be finite")


def propagation_delay_s(range_m: float) -> float:
    _positive_finite(range_m, "range_m")
    return range_m / SPEED_OF_LIGHT_MPS


def doppler_shift_hz(carrier_frequency_hz: float, range_rate_mps: float) -> float:
    _positive_finite(carrier_frequency_hz, "carrier_frequency_hz")
    _finite(range_rate_mps, "range_rate_mps")
    return -carrier_frequency_hz * range_rate_mps / SPEED_OF_LIGHT_MPS


def free_space_path_loss_db(range_m: float, carrier_frequency_hz: float) -> float:
    _positive_finite(range_m, "range_m")
    _positive_finite(carrier_frequency_hz, "carrier_frequency_hz")
    return 20.0 * log10(4.0 * pi * range_m * carrier_frequency_hz / SPEED_OF_LIGHT_MPS)


def received_carrier_power_dbw(
    eirp_dbw: float,
    receiver_gain_dbi: float,
    path_loss_db: float,
    miscellaneous_loss_db: float,
) -> float:
    _finite(eirp_dbw, "eirp_dbw")
    _finite(receiver_gain_dbi, "receiver_gain_dbi")
    _finite(path_loss_db, "path_loss_db")
    if not isfinite(miscellaneous_loss_db) or miscellaneous_loss_db < 0.0:
        raise ValueError("miscellaneous_loss_db must be finite and non-negative")
    return eirp_dbw + receiver_gain_dbi - path_loss_db - miscellaneous_loss_db


def noise_density_dbw_per_hz(system_noise_temperature_k: float) -> float:
    _positive_finite(system_noise_temperature_k, "system_noise_temperature_k")
    return 10.0 * log10(BOLTZMANN_J_PER_K * system_noise_temperature_k)


def carrier_to_noise_density_db_hz(
    carrier_power_dbw: float,
    noise_density_dbw_per_hz: float,
) -> float:
    _finite(carrier_power_dbw, "carrier_power_dbw")
    _finite(noise_density_dbw_per_hz, "noise_density_dbw_per_hz")
    return carrier_power_dbw - noise_density_dbw_per_hz


def signal_to_noise_ratio_db(
    carrier_to_noise_density_db_hz: float,
    channel_bandwidth_hz: float,
) -> float:
    _finite(carrier_to_noise_density_db_hz, "carrier_to_noise_density_db_hz")
    _positive_finite(channel_bandwidth_hz, "channel_bandwidth_hz")
    return carrier_to_noise_density_db_hz - 10.0 * log10(channel_bandwidth_hz)


def shannon_capacity_upper_bound_bps(
    signal_to_noise_ratio_db: float,
    channel_bandwidth_hz: float,
) -> float:
    _finite(signal_to_noise_ratio_db, "signal_to_noise_ratio_db")
    _positive_finite(channel_bandwidth_hz, "channel_bandwidth_hz")
    x = signal_to_noise_ratio_db * log(10.0) / 10.0
    softplus = x + log1p(exp(-x)) if x > 0.0 else log1p(exp(x))
    return channel_bandwidth_hz * softplus / log(2.0)
