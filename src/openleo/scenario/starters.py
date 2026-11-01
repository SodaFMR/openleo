"""Starter scenario documents for ``openleo scenario new``.

JSON has no comments, so the guidance that a commented template would carry is in the
document's ``description``. A starter is a template for the repository root: its catalog paths
are ``examples/data/...``, relative to the directory the file is saved in. Everything else in it
is a declared assumption to replace.

Each starter is a valid, normalised scenario (the form ``scenario_document`` writes), checked by
the tests against the parser, the JSON Schema and the archived catalogs.
"""

import json
from typing import Any, Literal

from openleo.scenario.parse import SCENARIO_SCHEMA_ID

Preset = Literal["single-pass", "constellation"]

_DEFAULT_MASK_DEG = 10.0
_CELESTRAK_TERMS = "https://celestrak.org/usage-policy.php"
_TEMPLATE_NOTE = (
    "Template from `openleo scenario new --preset {preset}`: {summary} Every value is a declared "
    "assumption until you replace it with your own. Save the file at the repository root, where "
    "the catalog path examples/data/... resolves, or point space_segment.catalogs[0].path at "
    "your own archived catalog and change its source_url, retrieved_at_utc, terms_url and sha256 "
    "with it: the sha256 is checked when the catalog is loaded. {more}"
)


def _document(
    *,
    preset: str,
    summary: str,
    more: str,
    name: str,
    time: dict[str, Any],
    catalog: dict[str, Any],
    synthetic_rf: bool,
    stations: list[dict[str, Any]],
    radio: dict[str, Any],
    propagation: dict[str, Any],
    network: dict[str, Any] | None,
) -> dict[str, Any]:
    """A document in the key order and with the explicit defaults of ``scenario_document``."""
    return {
        "schema": SCENARIO_SCHEMA_ID,
        "name": name,
        "description": _TEMPLATE_NOTE.format(preset=preset, summary=summary, more=more),
        "time": {
            **time,
            "events": {"enabled": True, "tolerance_s": 0.001},
            "convergence": {
                "mode": "error",
                "refinement_factor": 4,
                "relative_tolerance": 0.01,
                "absolute_tolerance_bits": 1.0,
            },
        },
        "space_segment": {
            "catalogs": [catalog],
            "select_norad_ids": None,
            "synthetic_rf": synthetic_rf,
        },
        "terminals": {
            "default": {
                "receive_gain_dbi": 20.0,
                "system_noise_temperature_k": 300.0,
                "miscellaneous_loss_db": 2.0,
            }
        },
        "ground_segment": {
            "default_minimum_elevation_deg": _DEFAULT_MASK_DEG,
            "stations": stations,
        },
        "radio": radio,
        "adaptation": {
            "standard": "dvb-s2",
            "table": "v1-reference-subset",
            "symbol_rate_baud": 1.0e6,
            "rolloff": 0.2,
            "implementation_margin_db": 2.0,
            "hysteresis_db": 0.5,
            "feedback_delay_s": 0.0,
        },
        "propagation": propagation,
        "network": network,
        "outputs": {"formats": ["csv", "olc"], "satellite_positions": "grid"},
    }


def _station(name: str, latitude: float, longitude: float, height: float, amsl: float | None):
    return {
        "name": name,
        "latitude_deg": latitude,
        "longitude_deg": longitude,
        "height_ellipsoid_m": height,
        "height_amsl_m": amsl,
        "terminal": "default",
        # null in a hand-written file means the default mask; the normalised form states it
        "minimum_elevation_deg": _DEFAULT_MASK_DEG,
    }


def _single_pass() -> dict[str, Any]:
    return _document(
        preset="single-pass",
        summary="one pass of the ISS over one ground station in free space, from the archived "
        "ISS catalog of examples/data.",
        more="Add a second station and a network block, or switch propagation.model to "
        "itu_reference (every station then needs height_amsl_m and a mask of at least 5 deg), "
        "to grow it into the constellation preset.",
        name="single-pass-starter",
        time={
            "start_utc": "2026-08-30T06:14:00Z",
            "stop_utc": "2026-08-30T06:24:00Z",
            "step_s": 10.0,
        },
        catalog={
            "path": "examples/data/iss_2026-08-30.csv",
            "format": "celestrak_gp_csv",
            "source_url": "https://celestrak.org/NORAD/elements/gp.php?CATNR=25544&FORMAT=CSV",
            "retrieved_at_utc": "2026-08-30T22:01:11Z",
            "terms_url": _CELESTRAK_TERMS,
            "sha256": "ddb21d9a4a3a4812ee397751555b9d55d2767f30c33190afba90e4bbb8db13f9",
        },
        synthetic_rf=False,
        stations=[_station("Cartagena", 37.6057, -0.9913, 20.0, None)],
        radio={
            "carrier_frequency_hz": 2.2e9,
            "channel_bandwidth_hz": 1.2e6,
            "eirp_dbw": 10.0,
        },
        propagation={"model": "free_space", "refinement": 1, "hydrometeors": None},
        network=None,
    )


def _constellation() -> dict[str, Any]:
    return _document(
        preset="constellation",
        summary="the archived Iridium NEXT catalog of examples/data seen from two ground "
        "stations through the ITU-R reference atmosphere, with routing between them. The radio "
        "block is declared, not Iridium's real RF, so synthetic_rf is true.",
        more="Declare clouds and rain with propagation.hydrometeors, as in "
        "examples/v2/iridium_global_hydrometeors.json.",
        name="constellation-starter",
        time={
            "start_utc": "2026-09-10T12:00:00Z",
            "stop_utc": "2026-09-10T14:00:00Z",
            "step_s": 60.0,
        },
        catalog={
            "path": "examples/data/iridium_next_2026-09-10.csv",
            "format": "celestrak_gp_csv",
            "source_url": "https://celestrak.org/NORAD/elements/gp.php?GROUP=iridium-NEXT&FORMAT=CSV",
            "retrieved_at_utc": "2026-09-10T22:00:25Z",
            "terms_url": _CELESTRAK_TERMS,
            "sha256": "e9dac20f80bb4d0ae0090996619827aab1abf16d448ed8b0edf47b965abf6273",
        },
        synthetic_rf=True,
        stations=[
            _station("Madrid", 40.4168, -3.7038, 650.0, 650.0),
            _station("Tromso", 69.6492, 18.9553, 0.0, 0.0),
        ],
        radio={
            "carrier_frequency_hz": 12.0e9,
            "channel_bandwidth_hz": 1.2e6,
            "eirp_dbw": 15.0,
        },
        propagation={"model": "itu_reference", "refinement": 1, "hydrometeors": None},
        network={
            "pairs": [{"source": "Madrid", "target": "Tromso"}],
            "isl": {
                "rule": "all_pairs_within_range",
                "max_range_m": 5.0e6,
                "capacity_bps": 1.0e9,
            },
            "fixed_capacity_bps": 2228124.0,
            "route_models": ["minimum_delay", "maximum_rate", "fixed_capacity"],
        },
    )


_PRESETS = {"single-pass": _single_pass, "constellation": _constellation}


def starter(preset: Preset) -> str:
    """A valid scenario v2 document as JSON text (2-space indent, trailing newline)."""
    try:
        build = _PRESETS[preset]
    except KeyError:
        allowed = " or ".join(repr(name) for name in _PRESETS)
        raise ValueError(f"preset={preset!r} must be {allowed}") from None
    return json.dumps(build(), indent=2, ensure_ascii=False) + "\n"
