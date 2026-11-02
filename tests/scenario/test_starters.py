"""Starter scenario documents (``openleo scenario new``): valid templates for the repository root."""

import json
from hashlib import sha256
from pathlib import Path

import jsonschema
import pytest

from openleo.scenario import scenario_json_schema
from openleo.scenario.parse import parse_scenario, scenario_document
from openleo.scenario.starters import starter

ROOT = Path(__file__).resolve().parents[2]
SHA = "0" * 64
PRESETS = ["single-pass", "constellation"]


@pytest.mark.parametrize("preset", PRESETS)
def test_a_starter_is_a_valid_normalised_scenario_at_the_repository_root(preset: str) -> None:
    text = starter(preset)
    document = json.loads(text)
    errors = [
        e.message
        for e in jsonschema.Draft202012Validator(scenario_json_schema()).iter_errors(document)
    ]
    assert not errors, errors[:3]
    scenario = parse_scenario(document, ROOT / "scenario.json", SHA)
    assert scenario_document(scenario) == document
    assert text == json.dumps(document, indent=2, ensure_ascii=False) + "\n"
    assert text == starter(preset)  # deterministic


@pytest.mark.parametrize("preset", PRESETS)
def test_a_starter_names_a_catalog_that_exists_with_the_declared_hash(preset: str) -> None:
    scenario = parse_scenario(json.loads(starter(preset)), ROOT / "scenario.json", SHA)
    for catalog in scenario.space_segment.catalogs:
        assert catalog.path.parent == ROOT / "examples" / "data"
        assert sha256(catalog.path.read_bytes()).hexdigest() == catalog.sha256


@pytest.mark.parametrize("preset", PRESETS)
def test_a_starter_says_it_is_a_template_and_where_it_expects_to_live(preset: str) -> None:
    description = json.loads(starter(preset))["description"]
    for phrase in ("Template", "repository root", "examples/data", "declared assumption"):
        assert phrase in description, phrase


def test_the_single_pass_starter_is_one_station_in_free_space_without_a_network() -> None:
    document = json.loads(starter("single-pass"))
    assert len(document["ground_segment"]["stations"]) == 1
    assert document["propagation"]["model"] == "free_space"
    assert document["network"] is None
    assert document["space_segment"]["catalogs"][0]["path"] == "examples/data/iss_2026-08-30.csv"
    assert document["space_segment"]["synthetic_rf"] is False


def test_the_constellation_starter_routes_between_stations_through_the_reference_atmosphere() -> (
    None
):
    document = json.loads(starter("constellation"))
    names = [station["name"] for station in document["ground_segment"]["stations"]]
    assert len(names) >= 2
    assert document["propagation"]["model"] == "itu_reference"
    assert all(s["height_amsl_m"] is not None for s in document["ground_segment"]["stations"])
    pair = document["network"]["pairs"][0]
    assert {pair["source"], pair["target"]} <= set(names)
    assert document["space_segment"]["catalogs"][0]["path"] == (
        "examples/data/iridium_next_2026-09-10.csv"
    )
    assert document["space_segment"]["synthetic_rf"] is True


def test_an_unknown_preset_is_refused_with_the_allowed_ones() -> None:
    with pytest.raises(ValueError, match="single-pass.*constellation|constellation.*single-pass"):
        starter("orbit")  # type: ignore[arg-type]
