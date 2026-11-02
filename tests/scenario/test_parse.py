"""Scenario v2 model and parser: the fixture corpus, defaults, round trip and metadata."""

import copy
import dataclasses
import json
import math
import re
import types
import typing
from hashlib import sha256
from pathlib import Path

import pytest

from openleo.physics.adaptation import AdaptationConfig
from openleo.physics.hydrometeor_path import STATION_KEYS, HydrometeorConfig, HydrometeorStation
from openleo.scenario import model
from openleo.scenario.model import (
    Adaptation,
    CatalogSource,
    ConvergenceSettings,
    EventSettings,
    GroundSegment,
    IslConfig,
    NetworkConfig,
    OutputConfig,
    Propagation,
    RadioConfig,
    RoutePair,
    Scenario,
    SpaceSegment,
    Station,
    Terminal,
    TimeConfig,
)
from openleo.scenario.parse import (
    HYDROMETEOR_STATE_BOUNDS,
    SCENARIO_SCHEMA_ID,
    load_scenario,
    parse_scenario,
    scenario_document,
)

FIXTURES = Path(__file__).parent / "fixtures"
VALID = sorted((FIXTURES / "valid").glob("*.json"))
INVALID = sorted((FIXTURES / "invalid").glob("*.json"))
SHA = "0" * 64


def _spec_example() -> dict:
    return json.loads((FIXTURES / "valid" / "spec_example_itu_hydrometeors.json").read_text())


def _free_space_base() -> dict:
    """A valid document whose numeric fields can move to their bounds independently."""
    document = _spec_example()
    document["propagation"] = {"model": "free_space"}
    document["radio"]["channel_bandwidth_hz"] = 1.0e7
    return document


def _set(document: dict, pointer: str, value) -> dict:
    *parents, last = (
        int(token) if token.isdigit() else token.replace("~1", "/").replace("~0", "~")
        for token in pointer.strip("/").split("/")
    )
    node = document
    for token in parents:
        node = node[token]
    node[last] = value
    return document


def _write(directory: Path, document, name: str = "scenario.json") -> Path:
    path = directory / name
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def test_corpus_is_present() -> None:
    assert len(VALID) >= 4
    assert len(INVALID) >= 90


@pytest.mark.parametrize("path", VALID, ids=lambda path: path.stem)
def test_valid_corpus_parses_and_round_trips(path: Path) -> None:
    source = path.read_bytes()
    digest = sha256(source).hexdigest()
    scenario = load_scenario(path)
    assert scenario.source_sha256 == digest
    assert scenario.source_path == path.resolve()

    document = scenario_document(scenario)
    assert parse_scenario(document, path, digest) == scenario
    # The document is plain JSON and a fixed point of normalisation.
    assert parse_scenario(json.loads(json.dumps(document)), path, digest) == scenario
    assert scenario_document(parse_scenario(document, path, digest)) == document


@pytest.mark.parametrize("path", INVALID, ids=lambda path: path.stem)
def test_invalid_corpus_rejected_with_pointer(path: Path, tmp_path: Path) -> None:
    fixture = json.loads(path.read_text(encoding="utf-8"))
    pointer = fixture["error_startswith"]
    text = (
        fixture["scenario_text"] if "scenario_text" in fixture else json.dumps(fixture["scenario"])
    )
    entry_points = [lambda: load_scenario(_write_text(tmp_path, text))]
    if "scenario" in fixture:
        entry_points.append(lambda: parse_scenario(fixture["scenario"], tmp_path / "s.json", SHA))

    for entry_point in entry_points:
        with pytest.raises(ValueError) as caught:
            entry_point()
        message = str(caught.value)
        assert type(caught.value) is ValueError
        assert message.startswith(pointer), message
        # The pointer must be the whole token, not a prefix of a longer name.
        assert message[len(pointer) : len(pointer) + 1] in {" ", "=", ":"}, message
        if "error_contains" in fixture:
            assert fixture["error_contains"] in message


def _write_text(directory: Path, text: str) -> Path:
    path = directory / "scenario.json"
    path.write_text(text, encoding="utf-8")
    return path


def test_v1_documents_name_the_upgrade_command_and_file(tmp_path: Path) -> None:
    fixture = json.loads((FIXTURES / "invalid" / "schema_v1_marker.json").read_text())
    path = _write(tmp_path, fixture["scenario"], "old scenario.json")
    with pytest.raises(
        ValueError, match=r"^/schema: .*`openleo scenario upgrade '.*old scenario\.json'`"
    ):
        load_scenario(path)
    assert SCENARIO_SCHEMA_ID == "openleo.scenario/2"


def test_spec_example_parses_to_the_documented_fields(tmp_path: Path) -> None:
    path = _write(tmp_path, _spec_example())
    scenario = load_scenario(path)

    assert scenario.name == "Global LEO declared hydrometeor sensitivity experiment"
    assert scenario.description == "optional free text"
    assert scenario.time.start_utc.isoformat() == "2026-09-10T12:00:00+00:00"
    assert scenario.time.stop_utc.utcoffset().total_seconds() == 0
    assert scenario.time.step_s == 60.0
    assert scenario.time.events == EventSettings(enabled=True, tolerance_s=0.001)
    assert scenario.time.convergence == ConvergenceSettings("error", 4, 0.01, 1.0)

    catalog = scenario.space_segment.catalogs[0]
    assert (
        catalog.path
        == (tmp_path / "../../../../examples/data/iridium_next_2026-09-10.csv").resolve()
    )
    assert catalog.format == "celestrak_gp_csv"
    assert catalog.retrieved_at_utc.isoformat() == "2026-09-10T22:00:25+00:00"
    assert scenario.space_segment.select_norad_ids is None
    assert scenario.space_segment.synthetic_rf is True

    assert scenario.terminals == (("default", Terminal(20.0, 300.0, 2.0)),)
    madrid, tromso = scenario.ground_segment.stations
    assert madrid == Station("Madrid", 40.4168, -3.7038, 650.0, 650.0, "default", 10.0)
    assert tromso.minimum_elevation_deg == 10.0  # inherited default, resolved at parse
    assert scenario.terminal_for(madrid) is scenario.terminals[0][1]

    assert scenario.radio == RadioConfig(12.0e9, 1.2e6, 15.0)
    assert scenario.adaptation == Adaptation(
        "dvb-s2", "v1-reference-subset", 1.0e6, 0.2, 2.0, 0.5, 0.0
    )
    assert scenario.adaptation.kernel() == AdaptationConfig(1.0e6, 0.2, 2.0, 0.5)

    assert scenario.propagation.model == "itu_reference"
    assert scenario.propagation.refinement == 1
    hydrometeors = scenario.propagation.hydrometeors
    assert isinstance(hydrometeors, HydrometeorConfig)
    assert [name for name, _ in hydrometeors.stations] == ["Madrid", "Tromso"]

    assert scenario.network == NetworkConfig(
        pairs=(RoutePair("Madrid", "Tromso"),),
        isl=IslConfig("all_pairs_within_range", 5.0e6, 1.0e9),
        fixed_capacity_bps=2228124.0,
        route_models=("minimum_delay", "maximum_rate", "fixed_capacity"),
    )
    assert scenario.outputs == OutputConfig(("csv", "olc"), "grid")


def test_omitted_optional_keys_take_defaults_and_normalise_to_explicit_values() -> None:
    path = FIXTURES / "valid" / "minimal_free_space.json"
    scenario = load_scenario(path)

    assert scenario.description == ""
    assert scenario.time.events == EventSettings()
    assert scenario.time.convergence == ConvergenceSettings()
    assert scenario.outputs == OutputConfig()
    assert scenario.network is None
    assert scenario.space_segment.select_norad_ids is None
    assert scenario.adaptation.feedback_delay_s == 0.0
    assert scenario.propagation == Propagation("free_space", 1, None)
    (station,) = scenario.ground_segment.stations
    assert (station.terminal, station.height_amsl_m, station.minimum_elevation_deg) == (
        "default",
        None,
        10.0,
    )
    # JSON integers become floats in float fields.
    assert type(scenario.time.step_s) is float
    assert type(scenario.terminals[0][1].miscellaneous_loss_db) is float

    assert scenario_document(scenario) == {
        "schema": "openleo.scenario/2",
        "name": "Minimal free-space scenario",
        "description": "",
        "time": {
            "start_utc": "2026-09-10T12:00:00Z",
            "stop_utc": "2026-09-10T12:30:00Z",
            "step_s": 30.0,
            "events": {"enabled": True, "tolerance_s": 0.001},
            "convergence": {
                "mode": "error",
                "refinement_factor": 4,
                "relative_tolerance": 0.01,
                "absolute_tolerance_bits": 1.0,
            },
        },
        "space_segment": {
            "catalogs": [
                {
                    "path": "../../../../examples/data/iridium_next_2026-09-10.csv",
                    "format": "celestrak_gp_csv",
                    "source_url": (
                        "https://celestrak.org/NORAD/elements/gp.php?GROUP=iridium-NEXT&FORMAT=CSV"
                    ),
                    "retrieved_at_utc": "2026-09-10T22:00:25Z",
                    "terms_url": "https://celestrak.org/usage-policy.php",
                    "sha256": "e9dac20f80bb4d0ae0090996619827aab1abf16d448ed8b0edf47b965abf6273",
                }
            ],
            "select_norad_ids": None,
            "synthetic_rf": False,
        },
        "terminals": {
            "default": {
                "receive_gain_dbi": 20.0,
                "system_noise_temperature_k": 300.0,
                "miscellaneous_loss_db": 0.0,
            }
        },
        "ground_segment": {
            "default_minimum_elevation_deg": 10.0,
            "stations": [
                {
                    "name": "Cartagena",
                    "latitude_deg": 37.6057,
                    "longitude_deg": -0.9913,
                    "height_ellipsoid_m": 20.0,
                    "height_amsl_m": None,
                    "terminal": "default",
                    "minimum_elevation_deg": 10.0,
                }
            ],
        },
        "radio": {
            "carrier_frequency_hz": 2.2e9,
            "channel_bandwidth_hz": 1.0e6,
            "eirp_dbw": 10.0,
        },
        "adaptation": {
            "standard": "dvb-s2",
            "table": "v1-reference-subset",
            "symbol_rate_baud": 5.0e5,
            "rolloff": 0.25,
            "implementation_margin_db": 1.0,
            "hysteresis_db": 0.0,
            "feedback_delay_s": 0.0,
        },
        "propagation": {"model": "free_space", "refinement": 1, "hydrometeors": None},
        "network": None,
        "outputs": {"formats": ["csv", "olc"], "satellite_positions": "grid"},
    }


def test_explicit_nulls_equal_omitted_keys() -> None:
    source = json.loads((FIXTURES / "valid" / "minimal_free_space.json").read_text())
    explicit = copy.deepcopy(source)
    explicit["network"] = None
    explicit["propagation"]["hydrometeors"] = None
    explicit["space_segment"]["select_norad_ids"] = None
    station = explicit["ground_segment"]["stations"][0]
    station.update(height_amsl_m=None, minimum_elevation_deg=None, terminal="default")
    path = FIXTURES / "valid" / "minimal_free_space.json"
    assert parse_scenario(explicit, path, SHA) == parse_scenario(source, path, SHA)


def test_catalog_paths_resolve_relative_to_the_scenario_file(tmp_path: Path) -> None:
    folder = tmp_path / "scenarios"
    folder.mkdir()
    document = _spec_example()
    document["space_segment"]["catalogs"][0]["path"] = "../data/never read.csv"
    path = _write(folder, document)

    scenario = load_scenario(path)  # the catalog does not exist: parsing never reads it
    assert (
        scenario.space_segment.catalogs[0].path == (tmp_path / "data" / "never read.csv").resolve()
    )
    assert scenario_document(scenario)["space_segment"]["catalogs"][0]["path"] == (
        "../data/never read.csv"
    )


def test_catalog_path_falls_back_to_absolute_posix_across_drives(tmp_path: Path, monkeypatch):
    def across_drives(path, start):
        raise ValueError("path is on mount 'C:', start on mount 'D:'")

    monkeypatch.setattr("openleo.scenario.parse.relpath", across_drives)
    path = _write(tmp_path, _spec_example())
    scenario = load_scenario(path)
    catalog = scenario_document(scenario)["space_segment"]["catalogs"][0]
    assert catalog["path"] == scenario.space_segment.catalogs[0].path.as_posix()
    document = scenario_document(scenario)
    assert parse_scenario(document, path, scenario.source_sha256) == scenario


def test_relative_scenario_path_is_resolved_once(tmp_path: Path, monkeypatch) -> None:
    _write(tmp_path, _spec_example())
    monkeypatch.chdir(tmp_path)
    scenario = load_scenario("scenario.json")
    assert scenario.source_path == (tmp_path / "scenario.json").resolve()
    monkeypatch.chdir(tmp_path.parent)  # the stored absolute path no longer depends on cwd
    document = scenario_document(scenario)
    assert parse_scenario(document, scenario.source_path, scenario.source_sha256) == scenario


def test_documents_are_independent_of_the_scenario() -> None:
    scenario = load_scenario(FIXTURES / "valid" / "spec_example_itu_hydrometeors.json")
    document = scenario_document(scenario)
    document["time"]["events"]["enabled"] = False
    document["terminals"]["default"]["receive_gain_dbi"] = -1.0
    assert scenario_document(scenario)["time"]["events"]["enabled"] is True
    assert scenario.time.events.enabled is True


def test_terminal_for_rejects_a_station_from_another_scenario() -> None:
    scenario = load_scenario(FIXTURES / "valid" / "two_catalogs_terminals_and_masks.json")
    dish = scenario.ground_segment.stations[1]
    assert scenario.terminal_for(dish).receive_gain_dbi == 38.5
    stranger = dataclasses.replace(dish, terminal="unlisted")
    with pytest.raises(ValueError, match="unlisted"):
        scenario.terminal_for(stranger)


def test_load_scenario_reports_file_level_errors_with_the_file_path(tmp_path: Path) -> None:
    missing = tmp_path / "missing.json"
    with pytest.raises(ValueError, match=f"^{re.escape(str(missing))}: could not load scenario"):
        load_scenario(missing)

    path = tmp_path / "bad.json"
    path.write_bytes(b"\xff\xfe{")
    with pytest.raises(ValueError, match=f"^{re.escape(str(path))}: scenario JSON must be UTF-8"):
        load_scenario(path)

    path.write_text("{", encoding="utf-8")
    with pytest.raises(ValueError, match=f"^{re.escape(str(path))}: invalid JSON"):
        load_scenario(path)

    path.write_text("[" * 100_000, encoding="utf-8")
    with pytest.raises(ValueError, match=f"^{re.escape(str(path))}: invalid JSON"):
        load_scenario(path)

    path.write_text("1" * 5000, encoding="utf-8")  # beyond Python's integer-digit limit
    with pytest.raises(ValueError, match=f"^{re.escape(str(path))}: invalid JSON"):
        load_scenario(path)


def test_load_scenario_rejects_files_over_one_megabyte(tmp_path: Path) -> None:
    document = _spec_example()
    document["description"] = "x" * 1_000_000
    path = _write(tmp_path, document)
    assert path.stat().st_size > 1_000_000
    with pytest.raises(
        ValueError, match=f"^{re.escape(str(path))}: scenario JSON exceeds 1000000 bytes"
    ):
        load_scenario(path)


def test_load_scenario_rejects_non_finite_literals(tmp_path: Path) -> None:
    text = json.dumps(_spec_example()).replace('"eirp_dbw": 15.0', '"eirp_dbw": NaN')
    assert "NaN" in text
    with pytest.raises(ValueError, match=r"^/radio/eirp_dbw=nan must be finite"):
        load_scenario(_write_text(tmp_path, text))


def test_parse_scenario_requires_a_sha256_for_the_source_bytes() -> None:
    with pytest.raises(ValueError, match="source_sha256"):
        parse_scenario(_spec_example(), "s.json", "not-a-digest")


# --- an unusable catalog path ------------------------------------------------------------------


@pytest.mark.parametrize("index", [0, 1])
@pytest.mark.parametrize("bad", ["a\x00b", "\x00", "data/\x00.csv", "caf\ud800.csv"])
def test_a_catalog_path_the_system_cannot_resolve_is_a_pointer_prefixed_value_error(
    index: int, bad: str, tmp_path: Path
) -> None:
    # Path.resolve raises a bare ValueError for a NUL and UnicodeEncodeError for a lone surrogate
    # (a Windows path may hold one, so there it is the NUL cases that run).
    try:
        (tmp_path / bad).resolve()
    except (OSError, RuntimeError, ValueError):
        pass
    else:
        pytest.skip("this platform resolves that path")
    document = json.loads(
        (FIXTURES / "valid" / "two_catalogs_terminals_and_masks.json").read_text()
    )
    document["space_segment"]["catalogs"][index]["path"] = bad
    pointer = f"/space_segment/catalogs/{index}/path"
    with pytest.raises(ValueError) as caught:
        parse_scenario(document, tmp_path / "s.json", SHA)
    assert type(caught.value) is ValueError
    message = str(caught.value)
    assert message.startswith(f"{pointer}="), message
    assert repr(bad)[:20] in message  # the value is shown, escaped by repr


# --- hydrometeor errors name their station ------------------------------------------------------

# A station name whose pointer token needs the RFC 6901 ~0 escape, and whose literal "~1" must
# not read as "/" (station names cannot hold "/": metric ids use it as a separator).
AWKWARD = "Tromsø~N~1"
AWKWARD_POINTER = "/propagation/hydrometeors/stations/Tromsø~0N~01"


def _awkward_document() -> dict:
    document = _spec_example()
    document["ground_segment"]["stations"][1]["name"] = AWKWARD
    document["network"]["pairs"][0]["target"] = AWKWARD
    stations = document["propagation"]["hydrometeors"]["stations"]
    stations[AWKWARD] = stations.pop("Tromso")
    return document


@pytest.mark.parametrize(
    ("edit", "pointer", "tail"),
    [
        ({"rain_rate_mm_h": -1.0}, "rain_rate_mm_h", "=-1.0 must be non-negative"),
        ({"liquid_water_kg_m2": -0.5}, "liquid_water_kg_m2", "=-0.5 must be non-negative"),
        ({"rain_rate_mm_h": "x"}, "rain_rate_mm_h", "='x' must be finite"),
        ({"rain_rate_mm_h": True}, "rain_rate_mm_h", "=True must be finite"),
        ({"liquid_water_kg_m2": None}, "liquid_water_kg_m2", "=None must be finite"),
        ({"rain_top_height_amsl_m": 20_000.5}, "rain_top_height_amsl_m", "=20000.5 must be in"),
        ({"rain_top_height_amsl_m": -1.0}, "rain_top_height_amsl_m", "=-1.0 must be in"),
        ({"polarization_tilt_deg": 180.5}, "polarization_tilt_deg", "=180.5 must be in"),
        ({"polarization_tilt_deg": -0.5}, "polarization_tilt_deg", "=-0.5 must be in"),
        ({"polarization_tilt_deg": 10**400}, "polarization_tilt_deg", "must be finite"),
    ],
)
def test_a_bad_hydrometeor_field_names_the_station_and_the_field(edit, pointer, tail) -> None:
    for name, station_pointer in (
        ("Madrid", "/propagation/hydrometeors/stations/Madrid"),
        (AWKWARD, AWKWARD_POINTER),
    ):
        document = _awkward_document()
        document["propagation"]["hydrometeors"]["stations"][name].update(edit)
        with pytest.raises(ValueError) as caught:
            parse_scenario(document, "s.json", SHA)
        assert type(caught.value) is ValueError
        message = str(caught.value)
        assert message.startswith(f"{station_pointer}/{pointer}"), message
        assert tail in message, message


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda s: s.pop("rain_rate_mm_h"), "/rain_rate_mm_h is required"),
        (lambda s: s.update(snow_rate_mm_h=1.0), "/snow_rate_mm_h is not allowed"),
    ],
)
def test_a_hydrometeor_station_with_the_wrong_keys_names_the_station(mutate, expected) -> None:
    document = _awkward_document()
    mutate(document["propagation"]["hydrometeors"]["stations"][AWKWARD])
    with pytest.raises(ValueError) as caught:
        parse_scenario(document, "s.json", SHA)
    assert str(caught.value) == f"{AWKWARD_POINTER}{expected}"


def test_a_hydrometeor_station_that_is_not_an_object_names_the_station() -> None:
    document = _awkward_document()
    document["propagation"]["hydrometeors"]["stations"][AWKWARD] = [1, 2]
    with pytest.raises(ValueError) as caught:
        parse_scenario(document, "s.json", SHA)
    assert str(caught.value) == f"{AWKWARD_POINTER} must be a JSON object"


@pytest.mark.parametrize(
    ("mutate", "start"),
    [
        (lambda h: h.update(model="uniform"), "/propagation/hydrometeors/model='uniform'"),
        (lambda h: h.pop("model"), "/propagation/hydrometeors/model is required"),
        (lambda h: h.pop("stations"), "/propagation/hydrometeors/stations is required"),
        (lambda h: h.update(extra=1), "/propagation/hydrometeors/extra is not allowed"),
        (lambda h: h.update(stations=[]), "/propagation/hydrometeors/stations must be"),
        (lambda h: h.update(stations={}), "/propagation/hydrometeors/stations must name every"),
        (
            lambda h: h["stations"].pop("Madrid"),
            "/propagation/hydrometeors/stations must name every ground station exactly once",
        ),
        (
            lambda h: h["stations"].update(Oslo=h["stations"]["Madrid"]),
            "/propagation/hydrometeors/stations must name every ground station exactly once",
        ),
    ],
)
def test_hydrometeor_structure_errors_carry_their_own_pointer(mutate, start) -> None:
    document = _spec_example()
    mutate(document["propagation"]["hydrometeors"])
    with pytest.raises(ValueError) as caught:
        parse_scenario(document, "s.json", SHA)
    assert type(caught.value) is ValueError
    assert str(caught.value).startswith(start), str(caught.value)


def test_the_hydrometeor_bounds_table_is_exactly_the_physics_kernels() -> None:
    assert set(HYDROMETEOR_STATE_BOUNDS) == STATION_KEYS
    valid = {
        "liquid_water_kg_m2": 0.5,
        "rain_rate_mm_h": 25.0,
        "rain_top_height_amsl_m": 5000.0,
        "polarization_tilt_deg": 45.0,
    }
    for key, bounds in HYDROMETEOR_STATE_BOUNDS.items():
        lower, upper = bounds["minimum"], bounds.get("maximum", math.inf)
        probes = [lower, math.nextafter(lower, -math.inf), 1e300, -1e300]
        if upper != math.inf:
            probes += [upper, math.nextafter(upper, math.inf), upper / 2]
        for value in probes:
            try:
                HydrometeorStation(**{**valid, key: value})
                kernel = True
            except ValueError:
                kernel = False
            assert kernel == (lower <= value <= upper), (key, value)


def test_hydrometeor_states_follow_the_ground_station_order_not_the_document_order() -> None:
    document = _spec_example()
    stations = document["propagation"]["hydrometeors"]["stations"]
    document["propagation"]["hydrometeors"]["stations"] = dict(reversed(stations.items()))
    scenario = parse_scenario(document, "s.json", SHA)
    names = [station.name for station in scenario.ground_segment.stations]
    assert [name for name, _ in scenario.propagation.hydrometeors.stations] == names


# --- field metadata: the contract with the JSON Schema generator -----------------------------

MODEL_CLASSES = (
    CatalogSource,
    EventSettings,
    ConvergenceSettings,
    TimeConfig,
    SpaceSegment,
    Terminal,
    Station,
    GroundSegment,
    RadioConfig,
    Adaptation,
    Propagation,
    IslConfig,
    RoutePair,
    NetworkConfig,
    OutputConfig,
    Scenario,
)
BOUND_KEYS = ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum")
ARRAY_KEYS = ("minItems", "maxItems", "uniqueItems", "items")
KNOWN_KEYS = {"unit", "description", "default", "enum", *BOUND_KEYS, *ARRAY_KEYS}

# Where each numeric field sits in a document (station fields use station 0).
NUMERIC_POINTERS = {
    (TimeConfig, "step_s"): "/time/step_s",
    (EventSettings, "tolerance_s"): "/time/events/tolerance_s",
    (ConvergenceSettings, "refinement_factor"): "/time/convergence/refinement_factor",
    (ConvergenceSettings, "relative_tolerance"): "/time/convergence/relative_tolerance",
    (ConvergenceSettings, "absolute_tolerance_bits"): "/time/convergence/absolute_tolerance_bits",
    (Terminal, "receive_gain_dbi"): "/terminals/default/receive_gain_dbi",
    (Terminal, "system_noise_temperature_k"): "/terminals/default/system_noise_temperature_k",
    (Terminal, "miscellaneous_loss_db"): "/terminals/default/miscellaneous_loss_db",
    (Station, "latitude_deg"): "/ground_segment/stations/0/latitude_deg",
    (Station, "longitude_deg"): "/ground_segment/stations/0/longitude_deg",
    (Station, "height_ellipsoid_m"): "/ground_segment/stations/0/height_ellipsoid_m",
    (Station, "height_amsl_m"): "/ground_segment/stations/0/height_amsl_m",
    (Station, "minimum_elevation_deg"): "/ground_segment/stations/0/minimum_elevation_deg",
    (
        GroundSegment,
        "default_minimum_elevation_deg",
    ): "/ground_segment/default_minimum_elevation_deg",
    (RadioConfig, "carrier_frequency_hz"): "/radio/carrier_frequency_hz",
    (RadioConfig, "channel_bandwidth_hz"): "/radio/channel_bandwidth_hz",
    (RadioConfig, "eirp_dbw"): "/radio/eirp_dbw",
    (Adaptation, "symbol_rate_baud"): "/adaptation/symbol_rate_baud",
    (Adaptation, "rolloff"): "/adaptation/rolloff",
    (Adaptation, "implementation_margin_db"): "/adaptation/implementation_margin_db",
    (Adaptation, "hysteresis_db"): "/adaptation/hysteresis_db",
    (Adaptation, "feedback_delay_s"): "/adaptation/feedback_delay_s",
    (Propagation, "refinement"): "/propagation/refinement",
    (IslConfig, "max_range_m"): "/network/isl/max_range_m",
    (IslConfig, "capacity_bps"): "/network/isl/capacity_bps",
    (NetworkConfig, "fixed_capacity_bps"): "/network/fixed_capacity_bps",
}


# Lower-bound neighbours that another rule rejects: whole microseconds; bandwidth >= Rs(1 + a).
CROSS_CHECKED = {"step_s", "channel_bandwidth_hz"}


def _scalar_kind(annotation) -> type | None:
    """``float`` or ``int`` for a numeric field, looking through ``X | None``."""
    if typing.get_origin(annotation) in (typing.Union, types.UnionType):
        members = [arg for arg in typing.get_args(annotation) if arg is not type(None)]
        annotation = members[0] if len(members) == 1 else None
    return annotation if annotation in (float, int) else None


def _numeric_fields() -> list[tuple[type, dataclasses.Field, type]]:
    found = []
    for owner in MODEL_CLASSES:
        hints = typing.get_type_hints(owner)
        for item in dataclasses.fields(owner):
            kind = _scalar_kind(hints[item.name])
            if kind is not None:
                found.append((owner, item, kind))
    return found


def test_model_classes_are_frozen_slotted_dataclasses() -> None:
    for owner in MODEL_CLASSES:
        assert dataclasses.is_dataclass(owner)
        assert owner.__dataclass_params__.frozen
        assert hasattr(owner, "__slots__")


def test_every_field_has_a_description_and_every_numeric_field_a_unit() -> None:
    for owner in MODEL_CLASSES:
        for item in dataclasses.fields(owner):
            where = f"{owner.__name__}.{item.name}"
            if where.startswith("Scenario.source_"):
                continue  # provenance of the file, not part of the document
            assert item.metadata.get("description"), where
            assert set(item.metadata) <= KNOWN_KEYS, where
    for owner, item, _ in _numeric_fields():
        assert item.metadata.get("unit"), f"{owner.__name__}.{item.name} has no unit"


def test_every_numeric_field_is_located_in_the_pointer_table() -> None:
    declared = {(owner, item.name) for owner, item, _ in _numeric_fields()}
    assert declared == set(NUMERIC_POINTERS)


def test_bound_metadata_is_well_formed() -> None:
    for owner, item, kind in _numeric_fields():
        meta = item.metadata
        where = f"{owner.__name__}.{item.name}"
        assert not ("minimum" in meta and "exclusiveMinimum" in meta), where
        assert not ("maximum" in meta and "exclusiveMaximum" in meta), where
        lower = meta.get("minimum", meta.get("exclusiveMinimum", -math.inf))
        upper = meta.get("maximum", meta.get("exclusiveMaximum", math.inf))
        assert lower < upper, where
        if kind is int:
            assert all(isinstance(meta[key], int) for key in BOUND_KEYS if key in meta), where


def _outside(value, kind: type, direction: int):
    if kind is int:
        return value + direction
    return math.nextafter(value, direction * math.inf)


@pytest.mark.parametrize(
    ("owner", "item", "kind"),
    _numeric_fields(),
    ids=lambda value: getattr(value, "name", getattr(value, "__name__", "")),
)
def test_metadata_bounds_are_exactly_what_the_parser_enforces(
    owner, item, kind, tmp_path: Path
) -> None:
    pointer = NUMERIC_POINTERS[(owner, item.name)]
    meta = item.metadata
    path = tmp_path / "s.json"

    def parse(value):
        return parse_scenario(_set(_free_space_base(), pointer, value), path, SHA)

    def rejected(value):
        with pytest.raises(ValueError) as caught:
            parse(value)
        assert str(caught.value).startswith(pointer), str(caught.value)

    for bad in (math.nan, math.inf, -math.inf, True, "1"):
        rejected(bad)
    if kind is int:
        rejected(2.5)
        assert parse(2.0) == parse(2)  # integral floats are valid JSON-Schema integers

    if "minimum" in meta:
        parse(meta["minimum"])
        rejected(_outside(meta["minimum"], kind, -1))
    if "maximum" in meta:
        parse(meta["maximum"])
        rejected(_outside(meta["maximum"], kind, +1))
    if "exclusiveMinimum" in meta:
        rejected(meta["exclusiveMinimum"])
        if item.name not in CROSS_CHECKED:
            parse(_outside(meta["exclusiveMinimum"], kind, +1))
    if "exclusiveMaximum" in meta:
        rejected(meta["exclusiveMaximum"])
        parse(_outside(meta["exclusiveMaximum"], kind, -1))
    if "enum" in meta:
        for allowed in meta["enum"]:
            parse(allowed)
        rejected(max(meta["enum"]) + 1)
        rejected(min(meta["enum"]) - 1)
    if not any(key in meta for key in (*BOUND_KEYS, "enum")):
        parse(-1.0e9)  # finite and unbounded: any finite value is accepted
        parse(1.0e9)


def test_enumerations_come_from_the_type_annotations() -> None:
    hints = {owner: typing.get_type_hints(owner) for owner in MODEL_CLASSES}
    assert typing.get_args(hints[CatalogSource]["format"]) == ("celestrak_gp_csv",)
    assert typing.get_args(hints[ConvergenceSettings]["mode"]) == ("error", "warn", "off")
    assert typing.get_args(hints[Adaptation]["table"]) == ("v1-reference-subset",)
    assert typing.get_args(typing.get_args(hints[OutputConfig]["formats"])[0]) == (
        "csv",
        "olc",
        "parquet",
    )
    assert model.ROUTE_MODELS == ("minimum_delay", "maximum_rate", "fixed_capacity")


@pytest.mark.parametrize(
    ("owner", "name", "pointer", "limits"),
    [
        (SpaceSegment, "catalogs", "/space_segment/catalogs", (1, None, False)),
        (SpaceSegment, "select_norad_ids", "/space_segment/select_norad_ids", (1, None, True)),
        (GroundSegment, "stations", "/ground_segment/stations", (1, 16, False)),
        (NetworkConfig, "pairs", "/network/pairs", (1, 16, True)),
        (NetworkConfig, "route_models", "/network/route_models", (1, 3, True)),
        (OutputConfig, "formats", "/outputs/formats", (1, 3, True)),
    ],
)
def test_array_metadata_declares_the_limits_the_parser_enforces(owner, name, pointer, limits):
    meta = {item.name: item.metadata for item in dataclasses.fields(owner)}[name]
    minimum, maximum, unique = limits
    assert meta["minItems"] == minimum
    assert meta.get("maxItems") == maximum
    assert meta.get("uniqueItems", False) is unique


def test_metadata_defaults_match_dataclass_defaults() -> None:
    for owner in MODEL_CLASSES:
        for item in dataclasses.fields(owner):
            if item.default is not dataclasses.MISSING:
                expected = list(item.default) if isinstance(item.default, tuple) else item.default
                assert item.metadata["default"] == expected, f"{owner.__name__}.{item.name}"


def test_norad_id_bounds_are_declared_on_the_items() -> None:
    meta = {item.name: item.metadata for item in dataclasses.fields(SpaceSegment)}
    assert meta["select_norad_ids"]["items"] == {"minimum": 1, "maximum": 2_147_483_647}


# --- robustness: no malformed document escapes as anything but a pointer-prefixed ValueError ---

WRONG_VALUES = (None, True, False, 0, -1, 1.5, 1e999, "", "x", [], [None], {}, {"a": 1}, 10**400)
_ABSENT = object()


def _all_pointers(node, pointer: str = "") -> list[str]:
    if isinstance(node, dict):
        children = [
            (f"{pointer}/{key.replace('~', '~0').replace('/', '~1')}", v) for key, v in node.items()
        ]
    elif isinstance(node, list):
        children = [(f"{pointer}/{index}", value) for index, value in enumerate(node)]
    else:
        return []
    return [entry for entry, value in children] + [
        found for entry, value in children for found in _all_pointers(value, entry)
    ]


def _replace(document: dict, pointer: str, value) -> dict:
    if value is not _ABSENT:
        return _set(document, pointer, value)
    parent, _, last = pointer.rpartition("/")
    node = document
    for token in parent.strip("/").split("/") if parent else []:
        node = node[int(token) if token.isdigit() else token.replace("~1", "/").replace("~0", "~")]
    del node[int(last) if isinstance(node, list) else last.replace("~1", "/").replace("~0", "~")]
    return document


@pytest.mark.parametrize("pointer", _all_pointers(_spec_example()))
def test_malformed_values_only_raise_pointer_prefixed_value_errors(pointer: str) -> None:
    for wrong in (*WRONG_VALUES, _ABSENT):
        document = _replace(_spec_example(), pointer, wrong)
        try:
            parse_scenario(document, "scenario.json", SHA)
        except ValueError as error:
            assert type(error) is ValueError
            assert str(error).startswith("/"), (pointer, wrong, str(error))


@pytest.mark.parametrize(
    "stamp", ["2026-09-10T24:00:00Z", "2026-09-10T24:00Z", "2026-09-10T24Z", "20260910T240000Z"]
)
def test_hour_24_is_rejected_on_every_python(stamp: str) -> None:
    from openleo._validation import _utc

    with pytest.raises(ValueError, match="valid ISO 8601 UTC timestamp"):
        _utc(stamp, "/time/start_utc")


def test_hour_24_lookalikes_are_not_rejected() -> None:
    from openleo._validation import _utc

    assert _utc("2026-09-24T10:00:00Z", "/x").day == 24
    assert _utc("2026-09-10T10:24:00Z", "/x").minute == 24


def test_hour_24_check_does_not_touch_minute_24() -> None:
    from openleo._validation import _utc

    assert _utc("2026-08-30T06:24:00Z", "/x").minute == 24
    assert _utc("2026-09-10T00:00:00Z", "/x").hour == 0
