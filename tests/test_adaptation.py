from dataclasses import FrozenInstanceError

import pytest


def _config(**changes):
    from openleo.adaptation import AdaptationConfig

    return AdaptationConfig(
        **{
            "symbol_rate_baud": 800_000.0,
            "rolloff": 0.2,
            "implementation_margin_db": 0.0,
            "hysteresis_db": 0.5,
            **changes,
        }
    )


@pytest.mark.parametrize(
    ("esn0", "name", "efficiency"),
    [
        (-2.350001, None, 0.0),
        (-2.35, "QPSK 1/4", 0.490243),
        (0.999999, "QPSK 1/4", 0.490243),
        (1.0, "QPSK 1/2", 0.988858),
        (4.03, "QPSK 3/4", 1.487473),
        (6.62, "8PSK 2/3", 1.980636),
        (7.91, "8PSK 3/4", 2.228124),
        (100.0, "8PSK 3/4", 2.228124),
    ],
)
def test_selects_etsi_normal_frame_thresholds(esn0, name, efficiency):
    from openleo.adaptation import select_modcod

    selected = select_modcod(esn0, _config())

    assert (selected.name if selected else None) == name
    assert (selected.efficiency_bits_per_symbol if selected else 0.0) == efficiency


def test_hysteresis_only_delays_upgrades_and_downgrades_immediately():
    from openleo.adaptation import select_modcod

    config = _config()
    quarter = select_modcod(0.0, config)
    assert select_modcod(1.49, config, quarter) == quarter
    half = select_modcod(1.5, config, quarter)
    assert half.name == "QPSK 1/2"
    assert select_modcod(0.999, config, half) == quarter
    assert select_modcod(-2.351, config, half) is None
    assert select_modcod(7.91, config).name == "8PSK 3/4"


def test_implementation_margin_shifts_lock_and_rate_thresholds():
    from openleo.adaptation import select_modcod

    config = _config(implementation_margin_db=2.0)
    assert select_modcod(-0.351, config) is None
    assert select_modcod(-0.35, config).name == "QPSK 1/4"
    assert select_modcod(3.0, config).name == "QPSK 1/2"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("symbol_rate_baud", 0),
        ("symbol_rate_baud", True),
        ("symbol_rate_baud", float("nan")),
        ("symbol_rate_baud", 1e308),
        ("rolloff", -0.01),
        ("rolloff", 1.01),
        ("rolloff", float("inf")),
        ("implementation_margin_db", -1),
        ("hysteresis_db", -1),
    ],
)
def test_rejects_invalid_programmatic_adaptation(field, value):
    with pytest.raises(ValueError, match=field):
        _config(**{field: value})


def test_selection_and_config_reject_unsafe_state():
    from openleo.adaptation import Modcod, select_modcod

    config = _config()
    with pytest.raises(FrozenInstanceError):
        config.rolloff = 0.35
    with pytest.raises(ValueError, match="esn0_db"):
        select_modcod(float("nan"), config)
    with pytest.raises(ValueError, match="previous"):
        select_modcod(10.0, config, Modcod("invented", 100.0, -10.0))
