"""``explain``: every assumption of a scenario, its evidence class, units and limitations."""

import dataclasses
from pathlib import Path

import pytest

from openleo.physics.adaptation import ETSI_SOURCE_URL, MODCODS
from openleo.scenario.explain import (
    FIXED_CAPACITY_THRESHOLD_WAIVER,
    ROUTE_CONVERGENCE_NOT_GATED,
    explain,
    limitations,
)
from openleo.scenario.model import ConvergenceSettings, EventSettings
from openleo.scenario.parse import load_scenario

ROOT = Path(__file__).resolve().parents[2]
V2_DIR = ROOT / "examples"

# Frozen expectations: the strings of OpenLEO v1.0.0's ``openleo.constellation._limitations`` and
# ``openleo.output.LIMITATIONS``, copied verbatim from tag v1.0.0 when the v1 engine was removed.
_V1_COMMON = (
    (
        "Archived GP elements are propagated predictions, not measured satellite positions; age is "
        "reported without an uncertainty distribution."
    ),
    (
        "Station coordinates and every RF/ISL parameter are declared experiment assumptions, not "
        "surveyed infrastructure or real constellation specifications."
    ),
    (
        "DVB-S2 reference rates are synthetic link adaptation, not Iridium waveform or receiver "
        "measurements."
    ),
)
_V1_ATMOSPHERE = {
    "hydrometeors": "Reference gaseous atmosphere with fixed declared clouds and uniform spherical "
    "rain layers; no measured local weather, P.618 exceedance statistics, scintillation, terrain, "
    "antenna patterns, interference, RF acquisition delay or tracking dynamics are modeled.",
    "reference": "Reference gaseous atmosphere only: no local weather, rain, cloud, fog, "
    "scintillation, terrain, antenna patterns, interference, RF acquisition delay or tracking "
    "dynamics are modeled.",
    "free_space": "No atmosphere, rain, scintillation, terrain, antenna patterns, interference, RF "
    "acquisition delay or tracking dynamics are modeled.",
}
_V1_TAIL = (
    "Contact boundaries and route choices are sampled; finer events between samples can be missed.",
    (
        "UTC datetime integration uses the POSIX convention; inserted leap seconds are not explicit "
        "grid samples or additional integration seconds."
    ),
    (
        "Snapshot path bottlenecks omit link scheduling, contention, queues, packet loss, TCP and "
        "protocol overhead; they are not measured throughput."
    ),
)
_V1_REFERENCE_ONLY = (
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
V1_OUTPUT_LIMITATIONS = (
    (
        "free-space-only propagation; no atmospheric gases, rain, cloud, fog, scintillation, or "
        "instantaneous weather"
    ),
    "geometric elevation only; no atmospheric refraction",
    "synthetic RF parameters; not a real commercial constellation performance claim",
    "Shannon-Hartley capacity is a theoretical upper bound, not throughput or achieved goodput",
    "sample-grid AOS/LOS estimates; true elevation-mask crossings are not interpolated",
    (
        "SGP4 propagation from public mean elements; GP element age is a quality indicator, not a "
        "covariance or accuracy guarantee"
    ),
    (
        "no calibrated-observation validation; SatNOGS observations are qualitative sanity checks "
        "unless a complete calibrated acquisition chain is documented"
    ),
)


def _example(name: str) -> Path:
    """A shipped example scenario: the ISS single pass or one of the Iridium constellations."""
    folder = "scenarios" if name == "iss_cartagena" else "constellations"
    return ROOT / "examples" / folder / f"{name}.json"


def v1_limitations(propagation) -> list[str]:
    """v1.0.0 ``openleo.constellation._limitations``, as frozen strings."""
    if propagation is None:
        atmosphere = _V1_ATMOSPHERE["free_space"]
    elif propagation.hydrometeors is not None:
        atmosphere = _V1_ATMOSPHERE["hydrometeors"]
    else:
        atmosphere = _V1_ATMOSPHERE["reference"]
    extra = _V1_REFERENCE_ONLY if propagation is not None else ()
    return [*_V1_COMMON, atmosphere, *_V1_TAIL, *extra]


EXAMPLES = [
    *sorted((V2_DIR / "constellations").glob("*.json")),
    V2_DIR / "scenarios" / "iss_cartagena.json",
]
FIXTURES = Path(__file__).parent / "fixtures" / "valid"


def _scenario(name: str):
    return load_scenario(_example(name))


def _with_time(scenario, **changes):
    return dataclasses.replace(scenario, time=dataclasses.replace(scenario.time, **changes))


def _sections(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith("## ")]


# --- evidence classes -----------------------------------------------------------------------------


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda path: path.stem)
def test_explain_lists_evidence_classes(path: Path) -> None:
    scenario = load_scenario(path)
    text = explain(scenario)
    for phrase in ("archived input", "declared assumption", "model choice"):
        assert phrase in text, phrase
    for station in scenario.ground_segment.stations:
        assert station.name in text
    assert "\n## Limitations\n" in text
    assert text.startswith(f"# {scenario.name}\n")
    assert text.endswith("\n") and not text.endswith("\n\n")


def test_the_sections_come_in_the_order_archived_declared_model_limitations() -> None:
    assert _sections(explain(_scenario("iridium_global_hydrometeors"))) == [
        "## Archived inputs",
        "## Declared assumptions",
        "## Model choices",
        "## Limitations",
    ]


def test_each_evidence_class_is_attached_to_the_right_things() -> None:
    text = explain(_scenario("iridium_global_hydrometeors"))
    _, rest = text.split("## Archived inputs")
    archived, rest = rest.split("## Declared assumptions")
    declared, rest = rest.split("## Model choices")
    model, _ = rest.split("## Limitations")
    assert "archived input" in archived
    assert "declared assumption" not in archived and "model choice" not in archived
    assert "declared assumption" in declared
    assert "archived input" not in declared and "model choice" not in declared
    assert "model choice" in model
    assert "archived input" not in model and "declared assumption" not in model

    # archived input: the catalog's provenance and every hash
    for item in (
        "e9dac20f80bb4d0ae0090996619827aab1abf16d448ed8b0edf47b965abf6273",
        "https://celestrak.org/NORAD/elements/gp.php?GROUP=iridium-NEXT&FORMAT=CSV",
        "https://celestrak.org/usage-policy.php",
        "2026-09-10T22:00:25Z",
        "../data/iridium_next_2026-09-10.csv",
        "celestrak_gp_csv",
    ):
        assert item in archived, item
    assert "| Scenario file SHA-256 |" in archived

    # declared assumption: stations, RF, terminals, rain and cloud, network, sampling
    for item in (
        "Madrid",
        "Quito",
        "12000000000.0",  # carrier
        "1200000.0",  # bandwidth
        "300.0",  # system temperature
        "2228124.0",  # fixed capacity
        "5000000.0",  # ISL range
        "2026-09-10T12:00:00Z",
        "60.0",  # step
        "2850.0",  # Quito AMSL
    ):
        assert item in declared, item
    # model choice: propagation, MODCOD table, events, convergence
    for item in ("itu_reference", "v1-reference-subset", "tolerance", "warn", "refinement factor"):
        assert item in model, item
    assert "Madrid" not in model


def test_scalar_rows_name_their_evidence_class_in_the_last_column() -> None:
    text = explain(_scenario("iridium_global_hydrometeors"))
    rows = {line.split("|")[1].strip(): line for line in text.splitlines() if line.count("|") == 5}
    assert rows["Carrier frequency"].endswith("| Hz | declared assumption |")
    assert rows["Channel bandwidth"].endswith("| Hz | declared assumption |")
    assert rows["Satellite EIRP"].endswith("| dBW | declared assumption |")
    assert rows["Sample step"].endswith("| s | declared assumption |")
    assert rows["Symbol rate"].endswith("| baud | declared assumption |")
    assert rows["Propagation model"].endswith("| model choice |")
    assert rows["Event tolerance"].endswith("| s | model choice |")
    assert rows["Catalog 1 SHA-256"].endswith("| archived input |")
    assert rows["Scenario file SHA-256"].endswith("| archived input |")
    assert rows["ISL capacity"].endswith("| bit/s | declared assumption |")


def test_stations_are_a_matrix_with_units_in_the_header_and_the_class_in_the_heading() -> None:
    text = explain(_scenario("iridium_global_hydrometeors"))
    assert "### Ground stations (declared assumption)" in text
    assert (
        "| Station | Latitude [deg] | Longitude [deg] | Ellipsoid height [m] | "
        "AMSL height [m] | Terminal | Elevation mask [deg] |"
    ) in text
    assert "| Madrid | 40.4168 | -3.7038 | 650.0 | 650.0 | default | 10.0 |" in text
    assert "| Quito | -0.1807 | -78.4678 | 2850.0 | 2850.0 | default | 10.0 |" in text
    assert "### Terminals (declared assumption)" in text
    assert (
        "| Terminal | Receive gain [dBi] | System noise temperature [K] | Miscellaneous loss [dB] |"
    ) in text
    assert "| default | 20.0 | 300.0 | 2.0 |" in text
    assert "### Rain and cloud (declared assumption)" in text
    assert (
        "| Station | Liquid water [kg/m^2] | Rain rate [mm/h] | Rain top [m AMSL] | "
        "Polarisation tilt [deg] |"
    ) in text
    assert "| Madrid | 0.5 | 25.0 | 5000.0 | 0.0 |" in text
    assert "| Singapore | 1.0 | 50.0 | 5000.0 | 45.0 |" in text


def test_free_space_has_no_amsl_height_and_no_rain_and_cloud() -> None:
    text = explain(_scenario("iss_cartagena"))
    assert "| Cartagena | 37.6057 | -0.9913 | 20.0 | n/a | default | 10.0 |" in text
    assert "No rain or cloud is declared." in text
    assert "### Rain and cloud" not in text
    assert "| Propagation model | free_space |" in text
    assert "### Network" not in text


def test_the_reference_atmosphere_without_hydrometeors_says_so() -> None:
    text = explain(_scenario("iridium_global_reference"))
    assert "No rain or cloud is declared." in text
    assert "ITU-R P.676-13" in text
    assert "ITU-R P.453-14" in text
    assert "ITU-R P.835-7" in text
    assert "ITU-R P.840-9" not in text


def test_the_hydrometeor_models_are_named() -> None:
    text = explain(_scenario("iridium_global_hydrometeors"))
    assert "ITU-R P.840-9" in text
    assert "ITU-R P.838-3" in text
    assert "declared uniform layers" in text


def test_the_network_assumptions_are_listed() -> None:
    text = explain(_scenario("iridium_global"))
    assert "### Network" in text
    assert "| Route pair 1 | Madrid -> Tromso | - | declared assumption |" in text
    assert "| ISL rule | all_pairs_within_range | - | declared assumption |" in text
    assert "| ISL range | 5000000.0 | m | declared assumption |" in text
    assert "| ISL capacity | 1000000000.0 | bit/s | declared assumption |" in text
    assert "| Fixed-capacity baseline | 2228124.0 | bit/s | declared assumption |" in text
    assert (
        "| Route models | minimum_delay, maximum_rate, fixed_capacity | - | declared assumption |"
        in text
    )


def test_the_modcod_table_is_listed_with_its_source() -> None:
    text = explain(_scenario("iss_cartagena"))
    assert ETSI_SOURCE_URL in text
    assert "| MODCOD | Required Es/N0 [dB] | Efficiency [bit/symbol] |" in text
    for modcod in MODCODS:
        assert (
            f"| {modcod.name} | {modcod.required_esn0_db!r} | {modcod.efficiency_bits_per_symbol!r} |"
            in text
        )


def test_convergence_off_prints_a_banner() -> None:
    scenario = _scenario("iss_cartagena")
    off = _with_time(scenario, convergence=ConvergenceSettings(mode="off"))
    assert "Convergence is off" in explain(off)
    assert "Convergence is off" not in explain(scenario)
    assert "| Convergence mode | warn | - | model choice |" in explain(scenario)


def test_catalog_selection_is_listed() -> None:
    scenario = load_scenario(FIXTURES / "two_catalogs_terminals_and_masks.json")
    text = explain(scenario)
    assert "Catalog 1 file" in text and "Catalog 2 file" in text
    assert "| NORAD selection | 25544, 43478, 2147483647 | - | declared assumption |" in text
    assert text.count("SHA-256") >= 3
    # more than one terminal: one row each
    assert all(f"| {name} |" in text for name, _ in scenario.terminals)
    assert "| NORAD selection | all objects in the catalogs |" in explain(
        _scenario("iss_cartagena")
    )


def test_the_synthetic_rf_flag_is_a_declared_assumption() -> None:
    assert "| Synthetic RF | true " in explain(_scenario("iridium_global"))
    assert "| Synthetic RF | false " in explain(_scenario("iss_cartagena"))
    assert "declared radio block" in explain(_scenario("iridium_global"))


# --- text hygiene -------------------------------------------------------------------------------


def test_the_description_is_quoted_and_names_are_kept_on_one_line() -> None:
    scenario = _scenario("iss_cartagena")
    station = dataclasses.replace(scenario.ground_segment.stations[0], name="Cartagena | N\nport")
    segment = dataclasses.replace(scenario.ground_segment, stations=(station,))
    scenario = dataclasses.replace(
        scenario,
        name="Line one\nLine two",
        description="First line.\n\nSecond line.",
        ground_segment=segment,
    )
    text = explain(scenario)
    assert text.startswith("# Line one Line two\n")
    assert "\n> First line.\n>\n> Second line.\n" in text
    assert "| Cartagena \\| N port |" in text


def test_an_empty_description_is_left_out() -> None:
    scenario = dataclasses.replace(_scenario("iss_cartagena"), description="")
    assert "\n> " not in explain(scenario)


def test_the_output_is_deterministic_and_machine_independent() -> None:
    scenario = _scenario("iridium_global_hydrometeors")
    assert explain(scenario) == explain(scenario)
    assert str(V2_DIR) not in explain(scenario)  # no absolute path of the file or the catalogs
    assert "iridium_global_hydrometeors.json" in explain(scenario)


# --- limitations ---------------------------------------------------------------------------------

V1_SAMPLED = (
    "Contact boundaries and route choices are sampled; finer events between samples can be missed."
)
V1_NETWORK = "Snapshot path bottlenecks omit link scheduling"
ROUTE_BREAKPOINTS = (
    "Route choices are evaluated at event-aware breakpoints and held between them; "
    "inter-satellite range and occlusion changes between breakpoints are not resolved."
)
SHANNON = (
    "Shannon-Hartley capacity is a theoretical upper bound, not throughput or achieved goodput"
)


@pytest.mark.parametrize(
    ("name", "model_phrase"),
    [
        ("iridium_global", "No atmosphere, rain"),
        ("iridium_global_reference", "Reference gaseous atmosphere only"),
        ("iridium_global_hydrometeors", "Reference gaseous atmosphere with fixed declared clouds"),
    ],
)
def test_limitations_follow_the_propagation_model(name: str, model_phrase: str) -> None:
    scenario = _scenario(name)
    items = limitations(scenario)
    assert sum(model_phrase in item for item in items) == 1
    text = explain(scenario)
    limitations_text = text.split("\n## Limitations\n")[1]
    for item in items:
        assert f"- {item}\n" in limitations_text
    # AMSL / refraction caveats belong to the reference atmosphere only
    assert any("AMSL heights are explicit assumptions" in item for item in items) == (
        name != "iridium_global"
    )


@pytest.mark.parametrize(
    "name", ["iridium_global", "iridium_global_reference", "iridium_global_hydrometeors"]
)
def test_limitations_are_the_v1_strings_verbatim_apart_from_the_shannon_caveat(name: str) -> None:
    scenario = _with_time(_scenario(name), events=EventSettings(enabled=False))
    assert scenario.network is not None
    propagation = None if scenario.propagation.model == "free_space" else scenario.propagation
    ours = limitations(scenario)
    s1_additions = (SHANNON, FIXED_CAPACITY_THRESHOLD_WAIVER, ROUTE_CONVERGENCE_NOT_GATED)
    assert [item for item in ours if item not in s1_additions] == v1_limitations(propagation)
    assert ours.count(SHANNON) == 1


def _no_network(scenario):
    return dataclasses.replace(scenario, network=None)


def test_events_disabled_keeps_the_v1_sampled_line_verbatim_with_or_without_a_network() -> None:
    scenario = _with_time(_scenario("iridium_global"), events=EventSettings(enabled=False))
    for variant in (scenario, _no_network(scenario)):
        items = limitations(variant)
        assert V1_SAMPLED in items
        assert ROUTE_BREAKPOINTS not in items
        assert items.count(V1_SAMPLED) == 1
    assert V1_SAMPLED == v1_limitations(None)[4]  # the line is the v1 one, not a paraphrase


def test_events_enabled_with_a_network_says_routes_are_held_between_breakpoints() -> None:
    scenario = _scenario("iridium_global")
    assert scenario.time.events.enabled and scenario.network is not None
    items = limitations(scenario)
    assert ROUTE_BREAKPOINTS in items
    assert items.count(ROUTE_BREAKPOINTS) == 1
    assert V1_SAMPLED not in items
    text = explain(scenario).split("\n## Limitations\n")[1]
    assert f"- {ROUTE_BREAKPOINTS}\n" in text
    assert ROUTE_BREAKPOINTS == (
        "Route choices are evaluated at event-aware breakpoints and held between them; "
        "inter-satellite range and occlusion changes between breakpoints are not resolved."
    )


def test_events_enabled_without_a_network_has_neither_route_line() -> None:
    scenario = _scenario("iss_cartagena")
    assert scenario.time.events.enabled and scenario.network is None
    items = limitations(scenario)
    assert V1_SAMPLED not in items
    assert ROUTE_BREAKPOINTS not in items
    assert not any("Route choices" in item or "route choices" in item for item in items)


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda path: path.stem)
@pytest.mark.parametrize("events", [True, False])
@pytest.mark.parametrize("network", [True, False])
def test_every_scenario_carries_the_shannon_caveat_once(path: Path, events: bool, network: bool):
    scenario = _with_time(load_scenario(path), events=EventSettings(enabled=events))
    scenario = scenario if network else _no_network(scenario)
    assert limitations(scenario).count(SHANNON) == 1
    assert f"- {SHANNON}\n" in explain(scenario).split("\n## Limitations\n")[1]


def test_the_shannon_caveat_is_the_wording_of_the_v1_summary() -> None:
    assert SHANNON in V1_OUTPUT_LIMITATIONS
    assert SHANNON == (
        "Shannon-Hartley capacity is a theoretical upper bound, not throughput or achieved goodput"
    )


def test_network_limits_name_the_threshold_waiver_and_the_ungated_route_convergence() -> None:
    scenario = _scenario("iridium_global")
    assert "fixed_capacity" in scenario.network.route_models
    items = limitations(scenario)
    assert items.count(FIXED_CAPACITY_THRESHOLD_WAIVER) == 1
    assert items.count(ROUTE_CONVERGENCE_NOT_GATED) == 1
    assert "regardless of the RF threshold (MODCOD lock)" in FIXED_CAPACITY_THRESHOLD_WAIVER
    adaptive = dataclasses.replace(
        scenario,
        network=dataclasses.replace(
            scenario.network, route_models=("minimum_delay",), fixed_capacity_bps=None
        ),
    )
    assert FIXED_CAPACITY_THRESHOLD_WAIVER not in limitations(adaptive)
    assert ROUTE_CONVERGENCE_NOT_GATED in limitations(adaptive)
    alone = limitations(_no_network(scenario))
    assert FIXED_CAPACITY_THRESHOLD_WAIVER not in alone
    assert ROUTE_CONVERGENCE_NOT_GATED not in alone


def test_the_path_bottleneck_limit_applies_only_with_a_network() -> None:
    assert any(item.startswith(V1_NETWORK) for item in limitations(_scenario("iridium_global")))
    assert not any(item.startswith(V1_NETWORK) for item in limitations(_scenario("iss_cartagena")))


def test_the_limitations_heading_carries_the_model_and_every_item_is_a_bullet() -> None:
    text = explain(_scenario("iridium_global_hydrometeors"))
    body = text.split("\n## Limitations\n")[1]
    assert body.startswith("\nPropagation model: itu_reference with declared hydrometeors.\n\n- ")
    assert all(line.startswith("- ") for line in body.strip().splitlines()[2:])
    free = explain(_scenario("iss_cartagena")).split("\n## Limitations\n")[1]
    assert free.startswith("\nPropagation model: free_space.\n\n- ")
    gas = explain(_scenario("iridium_global_reference")).split("\n## Limitations\n")[1]
    assert gas.startswith("\nPropagation model: itu_reference.\n\n- ")
