"""A Markdown account of a scenario: every assumption, its evidence class, its units, its limits.

Each value is classed as one of three kinds of evidence:

- **archived input**: data fixed by a hashed file and its recorded provenance (the catalogs, the
  scenario file itself);
- **declared assumption**: a value the scenario declares: stations, RF, terminals, rain and
  cloud, the network and the sampling grid. It is neither measured nor a published specification;
- **model choice**: a method or setting of the engine that the scenario selects: the propagation
  model, the MODCOD table, event localisation and the convergence gate.

The output is deterministic and machine independent (file names, not absolute paths).
"""

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from openleo.physics.adaptation import ETSI_SOURCE_URL, MODCODS
from openleo.physics.hydrometeors import CLOUD_SOURCE, RAIN_SOURCE
from openleo.physics.reference_atmosphere import PROFILE_SOURCE
from openleo.scenario.model import Scenario
from openleo.scenario.parse import _relative_posix, _utc_text

ARCHIVED = "archived input"
DECLARED = "declared assumption"
MODEL = "model choice"

# Scope limitations. Verbatim the strings of openleo.constellation._limitations (the v1 engine),
# which holds a test to them, kept here so that v2 does not need the v1 engine to say them.
_ARCHIVED_GP = (
    "Archived GP elements are propagated predictions, not measured satellite positions; age is "
    "reported without an uncertainty distribution."
)
_DECLARED_RF = (
    "Station coordinates and every RF/ISL parameter are declared experiment assumptions, not "
    "surveyed infrastructure or real constellation specifications."
)
_SYNTHETIC_ADAPTATION = (
    "DVB-S2 reference rates are synthetic link adaptation, not Iridium waveform or receiver "
    "measurements."
)
_ATMOSPHERE = {
    "hydrometeors": (
        "Reference gaseous atmosphere with fixed declared clouds and uniform spherical rain "
        "layers; no measured local weather, P.618 exceedance statistics, scintillation, terrain, "
        "antenna patterns, interference, RF acquisition delay or tracking dynamics are modeled."
    ),
    "reference": (
        "Reference gaseous atmosphere only: no local weather, rain, cloud, fog, scintillation, "
        "terrain, antenna patterns, interference, RF acquisition delay or tracking dynamics are "
        "modeled."
    ),
    "free_space": (
        "No atmosphere, rain, scintillation, terrain, antenna patterns, interference, RF "
        "acquisition delay or tracking dynamics are modeled."
    ),
}
_SAMPLED_EVENTS = (
    "Contact boundaries and route choices are sampled; finer events between samples can be missed."
)
_POSIX_LEAP_SECONDS = (
    "UTC datetime integration uses the POSIX convention; inserted leap seconds are not explicit "
    "grid samples or additional integration seconds."
)
_NETWORK_SNAPSHOTS = (
    "Snapshot path bottlenecks omit link scheduling, contention, queues, packet loss, TCP and "
    "protocol overhead; they are not measured throughput."
)
_REFERENCE_ATMOSPHERE = (
    (
        "AMSL heights are explicit assumptions distinct from WGS84 ellipsoid heights; a 6371 km "
        "spherical ray approximates local WGS84 geometry."
    ),
    (
        "System noise temperature stays fixed; atmospheric sky emission, refractive Doppler and "
        "dispersive group delay are not modeled. Visibility retains the geometric elevation mask."
    ),
    (
        "Reference-profile agreement and layer-refinement convergence are numerical checks, not "
        "measured-weather or calibrated RF validation."
    ),
)


_LEGEND = (
    "Every value below carries one evidence class:\n\n"
    f"- **{ARCHIVED}**: data fixed by a hashed file and its recorded provenance.\n"
    f"- **{DECLARED}**: a value this scenario declares, neither measured nor a published "
    "specification.\n"
    f"- **{MODEL}**: a method or setting of the engine that this scenario selects.\n\n"
    "Units are in the Unit column, or in square brackets in a column heading; a dash means none."
)


def limitations(scenario: Scenario) -> list[str]:
    """The scope limitations that apply to this scenario, in the order the v1 engine states them.

    Which apply depends on the propagation model (and its hydrometeors), on whether events are
    located (a sampled-contact limit holds only when they are not) and on whether there is a
    network (the path-bottleneck limit).
    """
    propagation = scenario.propagation
    reference = propagation.model == "itu_reference"
    atmosphere = (
        "hydrometeors"
        if propagation.hydrometeors is not None
        else "reference"
        if reference
        else "free_space"
    )
    return [
        _ARCHIVED_GP,
        _DECLARED_RF,
        _SYNTHETIC_ADAPTATION,
        _ATMOSPHERE[atmosphere],
        *([] if scenario.time.events.enabled else [_SAMPLED_EVENTS]),
        _POSIX_LEAP_SECONDS,
        *([_NETWORK_SNAPSHOTS] if scenario.network is not None else []),
        *(_REFERENCE_ATMOSPHERE if reference else ()),
    ]


def explain(scenario: Scenario) -> str:
    """Markdown listing each assumption of ``scenario`` with its evidence class and units."""
    blocks = [f"# {_line(scenario.name)}"]
    if scenario.description.strip():
        blocks.append(_quote(scenario.description))
    blocks += [
        _LEGEND,
        "## Archived inputs",
        _scalars(_archived_rows(scenario)),
        "## Declared assumptions",
        *_declared_blocks(scenario),
        "## Model choices",
        *_model_blocks(scenario),
        "## Limitations",
        f"Propagation model: {_propagation_name(scenario)}.",
        "\n".join(f"- {item}" for item in limitations(scenario)),
    ]
    return "\n\n".join(blocks) + "\n"


# --- sections -------------------------------------------------------------------------------------

Row = tuple[str, str, str, str]  # parameter, value, unit, evidence class


def _archived_rows(scenario: Scenario) -> list[Row]:
    rows = [
        ("Scenario file", _code(scenario.source_path.name), "-", ARCHIVED),
        ("Scenario file SHA-256", _code(scenario.source_sha256), "-", ARCHIVED),
    ]
    directory = scenario.source_path.parent
    for number, catalog in enumerate(scenario.space_segment.catalogs, start=1):
        prefix = f"Catalog {number}"
        rows += [
            (f"{prefix} file", _code(_relative_posix(catalog.path, directory)), "-", ARCHIVED),
            (f"{prefix} format", _text(catalog.format), "-", ARCHIVED),
            (f"{prefix} source URL", _code(catalog.source_url), "-", ARCHIVED),
            (f"{prefix} retrieved at", _text(catalog.retrieved_at_utc), "UTC", ARCHIVED),
            (f"{prefix} terms URL", _code(catalog.terms_url), "-", ARCHIVED),
            (f"{prefix} SHA-256", _code(catalog.sha256), "-", ARCHIVED),
        ]
    return rows


def _declared_blocks(scenario: Scenario) -> list[str]:
    time, radio, adaptation = scenario.time, scenario.radio, scenario.adaptation
    space = scenario.space_segment
    selection = (
        "all objects in the catalogs"
        if space.select_norad_ids is None
        else ", ".join(str(norad_id) for norad_id in space.select_norad_ids)
    )
    synthetic = (
        "true (declared radio block, not the named operator's real RF)"
        if space.synthetic_rf
        else "false"
    )
    blocks = [
        "### Sampling",
        _scalars(
            [
                ("Start", _text(time.start_utc), "UTC", DECLARED),
                ("Stop", _text(time.stop_utc), "UTC", DECLARED),
                ("Sample step", _text(time.step_s), "s", DECLARED),
            ]
        ),
        "### Space segment",
        _scalars(
            [
                ("NORAD selection", selection, "-", DECLARED),
                ("Synthetic RF", synthetic, "-", DECLARED),
            ]
        ),
        "### Radio",
        _scalars(
            [
                ("Carrier frequency", _text(radio.carrier_frequency_hz), "Hz", DECLARED),
                ("Channel bandwidth", _text(radio.channel_bandwidth_hz), "Hz", DECLARED),
                ("Satellite EIRP", _text(radio.eirp_dbw), "dBW", DECLARED),
            ]
        ),
        f"### Ground stations ({DECLARED})",
        _matrix(
            [
                "Station",
                "Latitude [deg]",
                "Longitude [deg]",
                "Ellipsoid height [m]",
                "AMSL height [m]",
                "Terminal",
                "Elevation mask [deg]",
            ],
            [
                [
                    station.name,
                    station.latitude_deg,
                    station.longitude_deg,
                    station.height_ellipsoid_m,
                    station.height_amsl_m,
                    station.terminal,
                    station.minimum_elevation_deg,
                ]
                for station in scenario.ground_segment.stations
            ],
        ),
        f"### Terminals ({DECLARED})",
        _matrix(
            [
                "Terminal",
                "Receive gain [dBi]",
                "System noise temperature [K]",
                "Miscellaneous loss [dB]",
            ],
            [
                [
                    name,
                    terminal.receive_gain_dbi,
                    terminal.system_noise_temperature_k,
                    terminal.miscellaneous_loss_db,
                ]
                for name, terminal in scenario.terminals
            ],
        ),
        "### Adaptation",
        _scalars(
            [
                ("Symbol rate", _text(adaptation.symbol_rate_baud), "baud", DECLARED),
                ("Roll-off", _text(adaptation.rolloff), "-", DECLARED),
                (
                    "Implementation margin",
                    _text(adaptation.implementation_margin_db),
                    "dB",
                    DECLARED,
                ),
                ("Hysteresis", _text(adaptation.hysteresis_db), "dB", DECLARED),
                ("Feedback delay", _text(adaptation.feedback_delay_s), "s", DECLARED),
            ]
        ),
    ]
    hydrometeors = scenario.propagation.hydrometeors
    if hydrometeors is None:
        blocks.append("No rain or cloud is declared.")
    else:
        blocks += [
            f"### Rain and cloud ({DECLARED})",
            _matrix(
                [
                    "Station",
                    "Liquid water [kg/m^2]",
                    "Rain rate [mm/h]",
                    "Rain top [m AMSL]",
                    "Polarisation tilt [deg]",
                ],
                [
                    [
                        name,
                        state.liquid_water_kg_m2,
                        state.rain_rate_mm_h,
                        state.rain_top_height_amsl_m,
                        state.polarization_tilt_deg,
                    ]
                    for name, state in hydrometeors.stations
                ],
            ),
        ]
    network = scenario.network
    if network is not None:
        blocks += [
            "### Network",
            _scalars(
                [
                    *(
                        (f"Route pair {number}", f"{pair.source} -> {pair.target}", "-", DECLARED)
                        for number, pair in enumerate(network.pairs, start=1)
                    ),
                    ("ISL rule", network.isl.rule, "-", DECLARED),
                    ("ISL range", _text(network.isl.max_range_m), "m", DECLARED),
                    ("ISL capacity", _text(network.isl.capacity_bps), "bit/s", DECLARED),
                    (
                        "Fixed-capacity baseline",
                        _text(network.fixed_capacity_bps),
                        "bit/s",
                        DECLARED,
                    ),
                    ("Route models", ", ".join(network.route_models), "-", DECLARED),
                ]
            ),
        ]
    return blocks


def _model_blocks(scenario: Scenario) -> list[str]:
    propagation, time = scenario.propagation, scenario.time
    reference = propagation.model == "itu_reference"
    method = (
        "Gaseous attenuation by ITU-R P.676-13 and refraction by ITU-R P.453-14, ray-traced "
        f"through the {PROFILE_SOURCE['recommendation']} reference atmosphere."
        if reference
        else "Free-space path loss only."
    )
    rows: list[Row] = [
        ("Propagation model", propagation.model, "-", MODEL),
        ("Propagation method", method, "-", MODEL),
    ]
    if reference:
        rows.append(("Ray-trace refinement", _text(propagation.refinement), "-", MODEL))
    if propagation.hydrometeors is not None:
        rows.append(
            (
                "Hydrometeor model",
                (
                    f"declared uniform layers: {CLOUD_SOURCE['recommendation']} cloud attenuation "
                    f"and {RAIN_SOURCE['recommendation']} rain attenuation"
                ),
                "-",
                MODEL,
            )
        )
    rows += [
        ("Adaptation standard", scenario.adaptation.standard, "-", MODEL),
        ("MODCOD table", scenario.adaptation.table, "-", MODEL),
        (
            "MODCOD source",
            (
                "ETSI EN 302 307-1 V1.4.1, Table 13: normal 64800-bit frames, no pilots, AWGN, "
                f"{_code(ETSI_SOURCE_URL)}"
            ),
            "-",
            MODEL,
        ),
        ("Events enabled", _text(time.events.enabled), "-", MODEL),
        ("Event tolerance", _text(time.events.tolerance_s), "s", MODEL),
        ("Convergence mode", time.convergence.mode, "-", MODEL),
        ("Convergence refinement factor", _text(time.convergence.refinement_factor), "-", MODEL),
        ("Convergence relative tolerance", _text(time.convergence.relative_tolerance), "-", MODEL),
        (
            "Convergence absolute tolerance",
            _text(time.convergence.absolute_tolerance_bits),
            "bit",
            MODEL,
        ),
    ]
    blocks = [
        _scalars(rows),
        _matrix(
            ["MODCOD", "Required Es/N0 [dB]", "Efficiency [bit/symbol]"],
            [
                [modcod.name, modcod.required_esn0_db, modcod.efficiency_bits_per_symbol]
                for modcod in MODCODS
            ],
        ),
    ]
    if time.convergence.mode == "off":
        blocks.append(
            "**Convergence is off**: the sample-grid refinement gate is not checked, and every "
            "artefact records that."
        )
    return blocks


def _propagation_name(scenario: Scenario) -> str:
    propagation = scenario.propagation
    suffix = " with declared hydrometeors" if propagation.hydrometeors is not None else ""
    return f"{propagation.model}{suffix}"


# --- Markdown -------------------------------------------------------------------------------------


def _line(value: object) -> str:
    """One line of text: line breaks of any kind become a space."""
    return " ".join(str(value).splitlines())


def _text(value: Any) -> str:
    """A value as a reader should see it: shortest exact floats, JSON booleans, n/a for none."""
    if value is None:
        return "n/a"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, datetime):
        return _utc_text(value)
    return _line(value)


def _code(text: str) -> str:
    return f"`{text}`" if "`" not in text else text


def _cell(value: Any) -> str:
    return _text(value).replace("|", "\\|")


def _table(header: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    lines = [header, ["---"] * len(header), *rows]
    return "\n".join(f"| {' | '.join(row)} |" for row in lines)


def _scalars(rows: Sequence[Row]) -> str:
    return _table(
        ["Parameter", "Value", "Unit", "Evidence class"],
        [[_cell(item) for item in row] for row in rows],
    )


def _matrix(header: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    return _table(header, [[_cell(item) for item in row] for row in rows])


def _quote(text: str) -> str:
    return "\n".join(f"> {line}".rstrip() for line in text.splitlines())
