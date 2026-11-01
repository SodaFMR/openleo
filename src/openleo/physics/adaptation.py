"""DVB-S2 reference MODCOD selection; no measured receiver-performance claim."""

from dataclasses import dataclass
from math import isfinite

from openleo.input import _finite_number, _non_negative_number, _positive_number, _range

ETSI_SOURCE_URL = (
    "https://www.etsi.org/deliver/etsi_en/302300_302399/30230701/"
    "01.04.01_60/en_30230701v010401p.pdf"
)


@dataclass(frozen=True, slots=True)
class Modcod:
    name: str
    efficiency_bits_per_symbol: float
    required_esn0_db: float


# ETSI EN 302 307-1 V1.4.1, Table 13: normal 64800-bit frames, no pilots, AWGN.
MODCODS = (
    Modcod("QPSK 1/4", 0.490243, -2.35),
    Modcod("QPSK 1/2", 0.988858, 1.00),
    Modcod("QPSK 3/4", 1.487473, 4.03),
    Modcod("8PSK 2/3", 1.980636, 6.62),
    Modcod("8PSK 3/4", 2.228124, 7.91),
)


@dataclass(frozen=True, slots=True)
class AdaptationConfig:
    symbol_rate_baud: float
    rolloff: float
    implementation_margin_db: float
    hysteresis_db: float

    def __post_init__(self):
        _positive_number(self.symbol_rate_baud, "adaptation.symbol_rate_baud")
        if not isfinite(self.symbol_rate_baud * MODCODS[-1].efficiency_bits_per_symbol):
            raise ValueError("adaptation.symbol_rate_baud produces a non-finite rate")
        _range(self.rolloff, "adaptation.rolloff", 0.0, 1.0)
        _non_negative_number(self.implementation_margin_db, "adaptation.implementation_margin_db")
        _non_negative_number(self.hysteresis_db, "adaptation.hysteresis_db")


def select_modcod(
    esn0_db: float, config: AdaptationConfig, previous: Modcod | None = None
) -> Modcod | None:
    """Acquire at nominal threshold; apply hysteresis only to upgrades of a lock.

    Falling below the current nominal requirement immediately downgrades or loses
    lock. Callers reset ``previous`` to None whenever geometric contact is lost.
    """
    _finite_number(esn0_db, "esn0_db")
    if previous is not None and previous not in MODCODS:
        raise ValueError("previous must be a reference MODCOD or None")
    available_db = esn0_db - config.implementation_margin_db
    retained = previous if previous and available_db >= previous.required_esn0_db else None
    eligible = (
        modcod
        for modcod in MODCODS
        if available_db
        >= modcod.required_esn0_db
        + (
            config.hysteresis_db
            if retained and modcod.required_esn0_db > retained.required_esn0_db
            else 0.0
        )
    )
    return max(eligible, key=lambda item: item.required_esn0_db, default=None)
