"""Upgrade of OpenLEO 1.x scenarios to scenario v2 (design spec section 3.4).

The four v1 examples are upgraded, validated by the parser and the JSON Schema, and compared with
the committed ``examples/v2`` files. The mapping is then checked field by field, independently of
the implementation, and every error path is held to the pointer-first message style of the parser.
"""

import copy
import csv
import json
import math
import os
import re
import shutil
from hashlib import sha256
from pathlib import Path

import jsonschema
import pytest

from openleo.scenario import scenario_json_schema
from openleo.scenario import upgrade as upgrade_module
from openleo.scenario.parse import load_scenario, parse_scenario, scenario_document
from openleo.scenario.upgrade import upgrade_file, upgrade_v1

ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = ROOT / "examples"
V2_DIR = EXAMPLES / "v2"
V1_EXAMPLES = [
    *sorted((EXAMPLES / "constellations").glob("*.json")),
    EXAMPLES / "scenarios" / "iss_cartagena.json",
]
SHA = "0" * 64
VALIDATOR = jsonschema.Draft202012Validator(scenario_json_schema())
IRIDIUM_V1 = EXAMPLES / "constellations" / "iridium_global_hydrometeors.json"
ISS_V1 = EXAMPLES / "scenarios" / "iss_cartagena.json"


def _v1(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _upgrade(raw: dict, path: Path = IRIDIUM_V1, output_dir: Path = V2_DIR) -> dict:
    return upgrade_v1(raw, source_dir=path.parent, output_dir=output_dir)


def _normalised_text(document: dict, output_dir: Path, name: str) -> str:
    scenario = parse_scenario(document, output_dir / name, SHA)
    return json.dumps(scenario_document(scenario), indent=2, ensure_ascii=False) + "\n"


def _example_tree(root: Path) -> Path:
    """The ISS v1 example and its catalog copied under ``root``, so that paths between the
    scenario, the catalog and an output folder never cross a drive (the checkout and the
    temporary directory can be on different drives on Windows). Returns the v1 file."""
    (root / "examples" / "scenarios").mkdir(parents=True)
    (root / "examples" / "data").mkdir()
    shutil.copyfile(
        EXAMPLES / "data" / "iss_2026-08-30.csv", root / "examples" / "data" / "iss_2026-08-30.csv"
    )
    v1 = root / "examples" / "scenarios" / "iss_cartagena.json"
    shutil.copyfile(ISS_V1, v1)
    return v1


# --- the four examples --------------------------------------------------------------------------


def test_the_v1_examples_are_the_four_the_task_names() -> None:
    assert [path.name for path in V1_EXAMPLES] == [
        "iridium_global.json",
        "iridium_global_hydrometeors.json",
        "iridium_global_reference.json",
        "iss_cartagena.json",
    ]


@pytest.mark.parametrize("path", V1_EXAMPLES, ids=lambda path: path.stem)
def test_upgrade_examples_validate(path: Path) -> None:
    text = upgrade_file(path, None)
    document = json.loads(text)
    errors = [error.message for error in VALIDATOR.iter_errors(document)]
    assert not errors, errors[:3]
    # The raw upgrade, before normalisation, is valid for the schema and the parser as well.
    raw = _upgrade(_v1(path), path)
    assert not [error.message for error in VALIDATOR.iter_errors(raw)]
    assert scenario_document(parse_scenario(raw, path, SHA)) == document
    scenario = parse_scenario(document, path, SHA)
    # The text is the normalised form: a fixed point of parse then scenario_document.
    assert scenario_document(scenario) == document
    assert text == json.dumps(document, indent=2, ensure_ascii=False) + "\n"
    assert text.endswith("}\n")
    assert '\n  "schema": "openleo.scenario/2",\n' in text


@pytest.mark.parametrize("path", V1_EXAMPLES, ids=lambda path: path.stem)
def test_committed_examples_are_the_normalised_upgrade(path: Path) -> None:
    committed = V2_DIR / path.name
    expected = _normalised_text(_upgrade(_v1(path), path), V2_DIR, path.name)
    assert committed.read_text(encoding="utf-8") == expected
    # They load like any v2 scenario, and their catalogs are the archived files next to them.
    scenario = load_scenario(committed)
    for catalog in scenario.space_segment.catalogs:
        assert sha256(catalog.path.read_bytes()).hexdigest() == catalog.sha256
    document = json.loads(expected)
    assert VALIDATOR.is_valid(document)
    assert document["space_segment"]["catalogs"][0]["path"].startswith("../data/")


def test_every_v1_example_has_a_v2_twin_and_only_known_folders_are_added() -> None:
    assert sorted(path.name for path in V2_DIR.glob("*.json")) == sorted(
        path.name for path in V1_EXAMPLES
    )
    # Besides the twins, the folder holds only the sweep and reproduce example folders (Task 14)
    # and the performance benchmark scenario (Task 18).
    assert sorted(path.name for path in V2_DIR.iterdir()) == sorted(
        [*(path.name for path in V1_EXAMPLES), "performance", "reproduce", "sweeps"]
    )
    assert sorted(path.name for path in (V2_DIR / "performance").iterdir()) == [
        "starlink_1000_24h.json"
    ]


@pytest.mark.parametrize("path", V1_EXAMPLES, ids=lambda path: path.stem)
def test_upgrade_is_deterministic(path: Path, tmp_path: Path) -> None:
    assert upgrade_file(path, None) == upgrade_file(path, None)
    assert upgrade_file(path, tmp_path / "a.json") == upgrade_file(path, tmp_path / "b.json")
    first = upgrade_v1(_v1(path), source_dir=path.parent, output_dir=V2_DIR)
    assert first == upgrade_v1(_v1(path), source_dir=path.parent, output_dir=V2_DIR)
    assert json.dumps(first) == json.dumps(
        upgrade_v1(_v1(path), source_dir=path.parent, output_dir=V2_DIR)
    )


# --- catalog paths --------------------------------------------------------------------------------


def test_upgrade_rewrites_catalog_path_relative_to_output(tmp_path: Path) -> None:
    v1 = _example_tree(tmp_path)
    folder = tmp_path / "examples" / "deep" / "nested"
    folder.mkdir(parents=True)
    output = folder / "iss.json"
    text = upgrade_file(v1, output)
    catalog = json.loads(text)["space_segment"]["catalogs"][0]
    assert catalog["path"] == "../../data/iss_2026-08-30.csv"
    assert (
        load_scenario(output).space_segment.catalogs[0].path
        == (tmp_path / "examples" / "data" / "iss_2026-08-30.csv").resolve()
    )
    # Without an output file the paths stay relative to the v1 file's own directory.
    assert json.loads(upgrade_file(v1, None))["space_segment"]["catalogs"][0]["path"] == (
        "../data/iss_2026-08-30.csv"
    )
    # An output in the catalog's own folder needs no "..".
    sibling = tmp_path / "examples" / "data" / "iss.json"
    assert json.loads(upgrade_file(v1, sibling))["space_segment"]["catalogs"][0]["path"] == (
        "iss_2026-08-30.csv"
    )


def test_upgrade_resolves_symlinks_before_relativising(tmp_path: Path) -> None:
    # v1 resolved the catalog path physically, so ".." after a symlink leaves the link's target.
    real = tmp_path / "real" / "scenarios"
    real.mkdir(parents=True)
    (real.parent / "data").mkdir()
    catalog = real.parent / "data" / "iss.csv"
    catalog.write_text("OBJECT_NAME\nISS (ZARYA)\n", encoding="utf-8")
    link = tmp_path / "link"
    try:
        os.symlink(real, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links are not available")
    raw = _v1(ISS_V1)
    raw["orbit"].update(path="../data/iss.csv", sha256=sha256(catalog.read_bytes()).hexdigest())
    result = upgrade_v1(raw, source_dir=link, output_dir=tmp_path / "other")
    assert result["space_segment"]["catalogs"][0]["path"] == "../real/data/iss.csv"


def _symlink(link: Path, target: Path) -> None:
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links are not available")


def test_a_symlinked_v1_file_resolves_its_catalog_from_the_real_file(tmp_path: Path) -> None:
    link = tmp_path / "link.json"
    _symlink(link, ISS_V1)
    catalog = json.loads(upgrade_file(link, None))["space_segment"]["catalogs"][0]
    assert catalog["path"] == "../data/iss_2026-08-30.csv"


def test_a_symlinked_output_gets_paths_relative_to_where_the_text_is_written(
    tmp_path: Path,
) -> None:
    (tmp_path / "a").mkdir()
    real = tmp_path / "b" / "c" / "d"
    real.mkdir(parents=True)
    _symlink(tmp_path / "a" / "out.json", real / "out.json")
    upgrade_file(ISS_V1, tmp_path / "a" / "out.json")
    written = real / "out.json"
    assert written.is_file()
    scenario = load_scenario(written)
    assert scenario.space_segment.catalogs[0].path == EXAMPLES / "data" / "iss_2026-08-30.csv"


def test_a_catalog_on_another_drive_keeps_its_absolute_posix_path(monkeypatch) -> None:
    def across_drives(path, start):
        raise ValueError("path is on mount 'C:', start on mount 'D:'")

    monkeypatch.setattr("openleo.scenario.parse.relpath", across_drives)
    result = _upgrade(_v1(ISS_V1), ISS_V1)
    path = result["space_segment"]["catalogs"][0]["path"]
    assert path == (EXAMPLES / "data" / "iss_2026-08-30.csv").as_posix()


# --- every v1 value -------------------------------------------------------------------------------


def _leaves(node, skip: frozenset = frozenset()):
    if isinstance(node, dict):
        for key, value in node.items():
            if key not in skip:
                yield from _leaves(value, skip)
    elif isinstance(node, list):
        for value in node:
            yield from _leaves(value, skip)
    else:
        yield node


def test_upgrade_preserves_every_v1_value() -> None:
    v1 = _v1(IRIDIUM_V1)
    v2 = _upgrade(copy.deepcopy(v1))
    radio_link, window = v1["radio_link"], v1["time_window"]
    heights = v1["propagation"]["station_heights_amsl_m"]

    assert v2["schema"] == "openleo.scenario/2"
    assert v2["name"] == v1["name"]

    (catalog,) = v2["space_segment"]["catalogs"]
    assert catalog["format"] == "celestrak_gp_csv"
    for key in ("source_url", "retrieved_at_utc", "terms_url", "sha256"):
        assert catalog[key] == v1["orbit"][key]
    assert catalog["path"] == "../data/iridium_next_2026-09-10.csv"
    assert v2["space_segment"]["select_norad_ids"] is None
    assert v2["space_segment"]["synthetic_rf"] is True

    for key in ("start_utc", "stop_utc", "step_s"):
        assert v2["time"][key] == window[key]
    assert v2["time"]["events"] == {"enabled": True, "tolerance_s": 0.001}
    assert v2["time"]["convergence"] == {
        "mode": "warn",
        "refinement_factor": 4,
        "relative_tolerance": 0.01,
        "absolute_tolerance_bits": 1.0,
    }

    assert v2["radio"] == {
        "carrier_frequency_hz": radio_link["carrier_frequency_hz"],
        "channel_bandwidth_hz": radio_link["channel_bandwidth_hz"],
        "eirp_dbw": radio_link["eirp_dbw"],
    }
    assert v2["terminals"] == {
        "default": {
            "receive_gain_dbi": radio_link["receiver_gain_dbi"],
            "system_noise_temperature_k": radio_link["system_noise_temperature_k"],
            "miscellaneous_loss_db": radio_link["miscellaneous_loss_db"],
        }
    }

    segment = v2["ground_segment"]
    assert segment["default_minimum_elevation_deg"] == window["minimum_elevation_deg"]
    assert len(segment["stations"]) == len(v1["stations"]) == 4
    for old, new in zip(v1["stations"], segment["stations"], strict=True):
        assert new == {
            "name": old["name"],
            "latitude_deg": old["latitude_deg"],
            "longitude_deg": old["longitude_deg"],
            "height_ellipsoid_m": old["height_m"],
            "height_amsl_m": heights[old["name"]],
            "terminal": "default",
            "minimum_elevation_deg": None,
        }

    assert v2["adaptation"] == {
        "standard": "dvb-s2",
        "table": "v1-reference-subset",
        **v1["adaptation"],
        "feedback_delay_s": 0.0,
    }
    assert v2["propagation"] == {
        "model": "itu_reference",
        "refinement": v1["propagation"]["refinement"],
        "hydrometeors": v1["propagation"]["hydrometeors"],
    }
    network = v1["network"]
    assert v2["network"] == {
        "pairs": [{"source": network["source_station"], "target": network["target_station"]}],
        "isl": {
            "rule": "all_pairs_within_range",
            "max_range_m": network["isl_max_range_m"],
            "capacity_bps": network["isl_capacity_bps"],
        },
        "fixed_capacity_bps": network["fixed_capacity_bps"],
        "route_models": ["minimum_delay", "maximum_rate", "fixed_capacity"],
    }
    assert v2["outputs"] == {"formats": ["csv", "olc"], "satellite_positions": "grid"}

    # Nothing is dropped: every value of the v1 file (but the catalog path, which is relocated)
    # is somewhere in the v2 document.
    kept = list(_leaves(v2))
    for value in _leaves(v1, skip=frozenset(("path",))):
        assert value in kept, value


def _get(document, pointer: str):
    for token in pointer.strip("/").split("/"):
        document = document[int(token) if isinstance(document, list) else token]
    return document


def _set_pointer(document, pointer: str, value) -> None:
    *parents, last = pointer.strip("/").split("/")
    node = _get(document, "/".join(parents)) if parents else document
    node[int(last) if isinstance(node, list) else last] = value


def _constellation_map() -> dict[str, str]:
    """v1 pointer -> v2 pointer, written out by hand for the hydrometeor example."""
    mapping = {
        "/name": "/name",
        "/orbit/source_url": "/space_segment/catalogs/0/source_url",
        "/orbit/retrieved_at_utc": "/space_segment/catalogs/0/retrieved_at_utc",
        "/orbit/terms_url": "/space_segment/catalogs/0/terms_url",
        "/time_window/start_utc": "/time/start_utc",
        "/time_window/stop_utc": "/time/stop_utc",
        "/time_window/step_s": "/time/step_s",
        "/time_window/minimum_elevation_deg": "/ground_segment/default_minimum_elevation_deg",
        "/radio_link/carrier_frequency_hz": "/radio/carrier_frequency_hz",
        "/radio_link/channel_bandwidth_hz": "/radio/channel_bandwidth_hz",
        "/radio_link/eirp_dbw": "/radio/eirp_dbw",
        "/radio_link/receiver_gain_dbi": "/terminals/default/receive_gain_dbi",
        "/radio_link/system_noise_temperature_k": "/terminals/default/system_noise_temperature_k",
        "/radio_link/miscellaneous_loss_db": "/terminals/default/miscellaneous_loss_db",
        "/adaptation/symbol_rate_baud": "/adaptation/symbol_rate_baud",
        "/adaptation/rolloff": "/adaptation/rolloff",
        "/adaptation/implementation_margin_db": "/adaptation/implementation_margin_db",
        "/adaptation/hysteresis_db": "/adaptation/hysteresis_db",
        "/network/isl_max_range_m": "/network/isl/max_range_m",
        "/network/isl_capacity_bps": "/network/isl/capacity_bps",
        "/network/fixed_capacity_bps": "/network/fixed_capacity_bps",
        "/propagation/refinement": "/propagation/refinement",
    }
    names = [station["name"] for station in _v1(IRIDIUM_V1)["stations"]]
    for index, name in enumerate(names):
        mapping[f"/stations/{index}/latitude_deg"] = (
            f"/ground_segment/stations/{index}/latitude_deg"
        )
        mapping[f"/stations/{index}/longitude_deg"] = (
            f"/ground_segment/stations/{index}/longitude_deg"
        )
        mapping[f"/stations/{index}/height_m"] = (
            f"/ground_segment/stations/{index}/height_ellipsoid_m"
        )
        mapping[f"/propagation/station_heights_amsl_m/{name}"] = (
            f"/ground_segment/stations/{index}/height_amsl_m"
        )
        for field in (
            "liquid_water_kg_m2",
            "rain_rate_mm_h",
            "rain_top_height_amsl_m",
            "polarization_tilt_deg",
        ):
            pointer = f"/propagation/hydrometeors/stations/{name}/{field}"
            mapping[pointer] = pointer
    return mapping


def test_every_v1_value_lands_in_its_own_v2_field() -> None:
    # Give each mapped v1 field a value of its own, so a mix-up cannot hide behind equal numbers.
    v1 = _v1(IRIDIUM_V1)
    mapping = _constellation_map()
    assert len(mapping) == 22 + 4 * 8  # 22 shared fields, 8 per station
    sentinels = {}
    for number, pointer in enumerate(mapping):
        text = isinstance(_get(v1, pointer), str)
        sentinels[pointer] = f"value-{number}" if text else 1000.25 + number
        _set_pointer(v1, pointer, sentinels[pointer])
    v2 = _upgrade(v1)
    for old, new in mapping.items():
        assert _get(v2, new) == sentinels[old], (old, new)
    # the pairs of the network are the station names, carried by name
    assert _get(v2, "/network/pairs/0") == {
        "source": v1["network"]["source_station"],
        "target": v1["network"]["target_station"],
    }
    # and nothing else of v2 carries a sentinel
    carried = {_get(v2, new) for new in mapping.values()}
    others = [leaf for leaf in _leaves(v2) if leaf in sentinels.values() and leaf not in carried]
    assert not others


def test_every_single_pass_value_lands_in_its_own_v2_field() -> None:
    v1 = _v1(ISS_V1)
    mapping = {
        "/ground_station/name": "/ground_segment/stations/0/name",
        "/ground_station/latitude_deg": "/ground_segment/stations/0/latitude_deg",
        "/ground_station/longitude_deg": "/ground_segment/stations/0/longitude_deg",
        "/ground_station/height_m": "/ground_segment/stations/0/height_ellipsoid_m",
        "/time_window/minimum_elevation_deg": "/ground_segment/default_minimum_elevation_deg",
        "/time_window/step_s": "/time/step_s",
        "/radio_link/receiver_gain_dbi": "/terminals/default/receive_gain_dbi",
        "/radio_link/system_noise_temperature_k": "/terminals/default/system_noise_temperature_k",
        "/radio_link/miscellaneous_loss_db": "/terminals/default/miscellaneous_loss_db",
        "/radio_link/carrier_frequency_hz": "/radio/carrier_frequency_hz",
        "/radio_link/eirp_dbw": "/radio/eirp_dbw",
    }
    sentinels = {}
    for number, pointer in enumerate(mapping):
        text = isinstance(_get(v1, pointer), str)
        sentinels[pointer] = f"value-{number}" if text else 17.5 + number
        _set_pointer(v1, pointer, sentinels[pointer])
    v2 = _upgrade(v1, ISS_V1)
    for old, new in mapping.items():
        assert _get(v2, new) == sentinels[old], (old, new)
    assert v2["ground_segment"]["stations"][0]["minimum_elevation_deg"] is None


@pytest.mark.parametrize("path", V1_EXAMPLES, ids=lambda path: path.stem)
def test_every_example_keeps_all_of_its_v1_values(path: Path) -> None:
    v1 = _v1(path)
    kept = list(_leaves(_upgrade(copy.deepcopy(v1), path)))
    for value in _leaves(v1, skip=frozenset(("path",))):
        assert value in kept, value


def test_a_constellation_without_propagation_is_free_space_with_no_amsl_heights() -> None:
    v2 = _upgrade(_v1(EXAMPLES / "constellations" / "iridium_global.json"))
    assert v2["propagation"] == {"model": "free_space", "refinement": 1, "hydrometeors": None}
    assert [station["height_amsl_m"] for station in v2["ground_segment"]["stations"]] == [None] * 4


def test_amsl_heights_follow_the_station_names_not_their_order() -> None:
    raw = _v1(EXAMPLES / "constellations" / "iridium_global_reference.json")
    raw["propagation"]["station_heights_amsl_m"] = dict(
        reversed(raw["propagation"]["station_heights_amsl_m"].items())
    )
    raw["propagation"]["refinement"] = 4
    v2 = _upgrade(raw)
    assert [s["height_amsl_m"] for s in v2["ground_segment"]["stations"]] == [
        650.0,
        0.0,
        0.0,
        2850.0,
    ]
    assert v2["propagation"] == {"model": "itu_reference", "refinement": 4, "hydrometeors": None}


def test_the_hydrometeor_block_is_copied_not_shared() -> None:
    raw = _v1(IRIDIUM_V1)
    v2 = _upgrade(raw)
    v2["propagation"]["hydrometeors"]["stations"]["Madrid"]["rain_rate_mm_h"] = 999.0
    assert raw["propagation"]["hydrometeors"]["stations"]["Madrid"]["rain_rate_mm_h"] == 25.0


def test_upgrade_does_not_change_its_input() -> None:
    raw = _v1(IRIDIUM_V1)
    before = copy.deepcopy(raw)
    _upgrade(raw)
    assert raw == before


# --- single pass ----------------------------------------------------------------------------------


def test_single_pass_becomes_one_station_without_network_and_with_upgrade_defaults() -> None:
    v1 = _v1(ISS_V1)
    v2 = _upgrade(copy.deepcopy(v1), ISS_V1)
    station = v1["ground_station"]
    assert v2["ground_segment"] == {
        "default_minimum_elevation_deg": v1["time_window"]["minimum_elevation_deg"],
        "stations": [
            {
                "name": station["name"],
                "latitude_deg": station["latitude_deg"],
                "longitude_deg": station["longitude_deg"],
                "height_ellipsoid_m": station["height_m"],
                "height_amsl_m": None,
                "terminal": "default",
                "minimum_elevation_deg": None,
            }
        ],
    }
    assert v2["network"] is None
    assert v2["propagation"] == {"model": "free_space", "refinement": 1, "hydrometeors": None}
    assert v2["space_segment"]["synthetic_rf"] is False
    assert v2["adaptation"] == {
        "standard": "dvb-s2",
        "table": "v1-reference-subset",
        "symbol_rate_baud": v1["radio_link"]["channel_bandwidth_hz"] / 1.2,
        "rolloff": 0.2,
        "implementation_margin_db": 0.0,
        "hysteresis_db": 0.0,
        "feedback_delay_s": 0.0,
    }
    description = v2["description"]
    assert "single-pass" in description
    for phrase in (
        "Upgrade defaults, not OpenLEO 1.x inputs",
        "adaptation dvb-s2 with the v1-reference-subset table",
        "symbol_rate_baud = channel_bandwidth_hz / 1.2",
        "rolloff 0.2",
        "implementation margin 0 dB",
        "hysteresis 0 dB",
        "feedback delay 0 s",
        "events enabled (tolerance 0.001 s)",
        "convergence mode warn",
    ):
        assert phrase in description, phrase
    assert "floating-point" not in description  # 1 MHz / 1.2 needs no correction


def test_a_constellation_description_names_its_upgrade_defaults_but_not_an_adaptation_one() -> None:
    description = _upgrade(_v1(IRIDIUM_V1))["description"]
    assert "constellation" in description
    assert "events enabled (tolerance 0.001 s)" in description
    assert "feedback delay 0 s" in description
    assert "symbol_rate_baud = " not in description  # the 1.x file declared its adaptation


@pytest.mark.parametrize("bandwidth", [1e5, 4e4, 1e6, 1.2e6, 123456.789, 3.3e6, 2e7, 5e3])
def test_the_default_symbol_rate_always_fits_the_channel(bandwidth: float, tmp_path: Path) -> None:
    raw = _v1(ISS_V1)
    raw["radio_link"]["channel_bandwidth_hz"] = bandwidth
    v2 = _upgrade(raw, ISS_V1)
    rate = v2["adaptation"]["symbol_rate_baud"]
    assert rate * (1 + 0.2) <= bandwidth  # the parser's own test
    steps = 0
    candidate = bandwidth / 1.2
    while candidate != rate:
        candidate = math.nextafter(candidate, 0.0)
        steps += 1
        assert steps <= 3
    assert ("floating-point" in v2["description"]) == (steps > 0)
    parse_scenario(v2, tmp_path / "s.json", SHA)  # and the whole document is valid


# --- the constellation / single-pass choice, schema key --------------------------------------------


def test_a_schema_key_of_the_first_scheme_is_accepted_and_dropped() -> None:
    raw = _v1(ISS_V1)
    raw["schema"] = "openleo.scenario/1"
    assert _upgrade(raw, ISS_V1) == _upgrade(_v1(ISS_V1), ISS_V1)


# --- the catalog decides synthetic_rf ---------------------------------------------------------------


def _write_catalog(directory: Path, names: list[str]) -> Path:
    path = directory / "catalog.csv"
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["OBJECT_NAME", "NORAD_CAT_ID"])
        for index, name in enumerate(names):
            writer.writerow([name, 1000 + index])
    return path


@pytest.mark.parametrize(
    ("names", "expected"),
    [
        (["ISS (ZARYA)"], False),
        (["NOAA 19", "ISS (ZARYA)"], False),
        (["IRIDIUM 106"], True),
        (["iridium 106"], True),
        (["Iridium NEXT"], True),
        (["STARLINK-1007"], True),
        (["starlink-1007"], True),
        (["ONEWEB-0012"], True),
        (["OneWeb-0012"], True),
        (["KUIPER-00001"], True),
        (["kuiper"], True),
        (["NOAA 19", "STARLINK-5"], True),  # any one object is enough
        (["NOAA 19", "NOAA 18", "NOAA 15", "IRIDIUM, 7"], True),  # a quoted comma
        (["STAR LINK", "ONE WEB", "IRID IUM", "KUIP ER"], False),
        (["Quipu"], False),
    ],
)
def test_synthetic_rf_is_true_when_an_object_names_a_real_operator(
    names: list[str], expected: bool, tmp_path: Path
) -> None:
    catalog = _write_catalog(tmp_path, names)
    raw = _v1(ISS_V1)
    raw["orbit"].update(path=catalog.name, sha256=sha256(catalog.read_bytes()).hexdigest())
    result = upgrade_v1(raw, source_dir=tmp_path, output_dir=tmp_path)
    assert result["space_segment"]["synthetic_rf"] is expected


def test_the_example_catalogs_decide_synthetic_rf() -> None:
    assert _upgrade(_v1(ISS_V1), ISS_V1)["space_segment"]["synthetic_rf"] is False
    for path in V1_EXAMPLES[:3]:
        assert _upgrade(_v1(path), path)["space_segment"]["synthetic_rf"] is True


# --- what is refused ------------------------------------------------------------------------------


def _pointer_error(raw, pointer: str, path: Path = IRIDIUM_V1, source_dir: Path | None = None):
    with pytest.raises(ValueError) as caught:
        upgrade_v1(raw, source_dir=source_dir or path.parent, output_dir=V2_DIR)
    message = str(caught.value)
    assert type(caught.value) is ValueError
    assert message.startswith(pointer), message
    assert message[len(pointer) : len(pointer) + 1] in {" ", "=", ":"}, message
    return message


def _drop(raw: dict, *pointer: str | int) -> dict:
    *parents, last = pointer
    node = raw
    for token in parents:
        node = node[token]
    del node[last]
    return raw


def _put(raw: dict, value, *pointer: str | int) -> dict:
    *parents, last = pointer
    node = raw
    for token in parents:
        node = node[token]
    node[last] = value
    return raw


@pytest.mark.parametrize(
    ("edit", "pointer"),
    [
        (lambda r: _put(r, 1, "extra"), "/extra"),
        (lambda r: _drop(r, "radio_link"), "/radio_link"),
        (lambda r: _drop(r, "adaptation"), "/adaptation"),
        (lambda r: _drop(r, "network"), "/network"),
        (lambda r: _drop(r, "orbit", "sha256"), "/orbit/sha256"),
        (lambda r: _put(r, 1, "orbit", "extra"), "/orbit/extra"),
        (lambda r: _drop(r, "time_window", "step_s"), "/time_window/step_s"),
        (lambda r: _put(r, 1, "time_window", "extra"), "/time_window/extra"),
        (lambda r: _drop(r, "radio_link", "eirp_dbw"), "/radio_link/eirp_dbw"),
        (lambda r: _put(r, 1, "radio_link", "extra"), "/radio_link/extra"),
        (lambda r: _drop(r, "adaptation", "hysteresis_db"), "/adaptation/hysteresis_db"),
        (lambda r: _drop(r, "network", "source_station"), "/network/source_station"),
        (lambda r: _put(r, 1, "network", "extra"), "/network/extra"),
        (lambda r: _drop(r, "stations", 0, "height_m"), "/stations/0/height_m"),
        (lambda r: _put(r, 1, "stations", 2, "extra"), "/stations/2/extra"),
        (lambda r: _put(r, 1, "propagation", "extra"), "/propagation/extra"),
        (lambda r: _drop(r, "propagation", "refinement"), "/propagation/refinement"),
        (
            lambda r: _drop(r, "propagation", "station_heights_amsl_m"),
            "/propagation/station_heights_amsl_m",
        ),
    ],
)
def test_wrong_keys_are_refused_with_their_pointer(edit, pointer: str) -> None:
    message = _pointer_error(edit(_v1(IRIDIUM_V1)), pointer)
    assert message in {f"{pointer} is required", f"{pointer} is not allowed"}


@pytest.mark.parametrize(
    ("edit", "start"),
    [
        (lambda r: [], "/ must be a JSON object"),
        (lambda r: "text", "/ must be a JSON object"),
        (lambda r: _drop(r, "stations"), "/ is not an OpenLEO 1.x scenario"),
        (lambda r: _put(r, {}, "ground_station"), "/ is not an OpenLEO 1.x scenario"),
        (lambda r: _put(r, {"name": "x"}, "stations", 0), "/stations/0/height_m is required"),
        (lambda r: _put(r, {}, "stations"), "/stations must be an array of ground stations"),
        (lambda r: _put(r, "no", "stations"), "/stations must be an array of ground stations"),
        (lambda r: _put(r, [1], "stations"), "/stations/0 must be a JSON object"),
        (lambda r: _put(r, 5, "stations", 0, "name"), "/stations/0/name"),
        (lambda r: _put(r, "  ", "stations", 1, "name"), "/stations/1/name"),
        (lambda r: _put(r, [], "radio_link"), "/radio_link must be a JSON object"),
        (lambda r: _put(r, "x", "orbit"), "/orbit must be a JSON object"),
        (lambda r: _put(r, 3, "orbit", "path"), "/orbit/path"),
        (lambda r: _put(r, "", "orbit", "path"), "/orbit/path"),
        (lambda r: _put(r, "free_space", "propagation", "model"), "/propagation/model="),
        (lambda r: _put(r, "x", "propagation", "model"), "/propagation/model="),
        (
            lambda r: _put(r, [], "propagation", "station_heights_amsl_m"),
            "/propagation/station_heights_amsl_m must be",
        ),
        (
            lambda r: _drop(r, "propagation", "station_heights_amsl_m", "Tromso"),
            "/propagation/station_heights_amsl_m/Tromso is required",
        ),
        (
            lambda r: _put(r, 0.0, "propagation", "station_heights_amsl_m", "Oslo"),
            "/propagation/station_heights_amsl_m/Oslo is not a station",
        ),
        (
            lambda r: _put(r, None, "propagation", "hydrometeors"),
            "/propagation/hydrometeors must be a JSON object",
        ),
        (
            lambda r: _put(r, [1], "propagation", "hydrometeors"),
            "/propagation/hydrometeors must be a JSON object",
        ),
        (lambda r: _put(r, "openleo.scenario/2", "schema"), "/schema"),
        (lambda r: _put(r, "openleo.scenario/3", "schema"), "/schema="),
        (lambda r: _put(r, None, "schema"), "/schema="),
        (lambda r: _put(r, "a\x00b", "orbit", "path"), "/orbit/path="),
        (lambda r: _put(r, "missing.csv", "orbit", "path"), "/orbit/path"),
        (lambda r: _put(r, "0" * 64, "orbit", "sha256"), "/orbit/sha256"),
    ],
)
def test_malformed_v1_documents_are_refused_with_a_pointer(edit, start: str) -> None:
    raw = edit(_v1(IRIDIUM_V1))
    with pytest.raises(ValueError) as caught:
        upgrade_v1(raw, source_dir=IRIDIUM_V1.parent, output_dir=V2_DIR)
    assert type(caught.value) is ValueError
    assert str(caught.value).startswith(start), str(caught.value)


def test_a_v2_document_is_not_upgraded() -> None:
    raw = json.loads((V2_DIR / "iss_cartagena.json").read_text(encoding="utf-8"))
    message = _pointer_error(raw, "/schema")
    assert "openleo.scenario/2" in message
    assert "nothing to upgrade" in message


def test_a_single_pass_bandwidth_that_cannot_give_a_default_symbol_rate_is_refused() -> None:
    for bad in (0, -1.0, "wide", None, True, float("inf"), 10**400):
        raw = _put(_v1(ISS_V1), bad, "radio_link", "channel_bandwidth_hz")
        with pytest.raises(ValueError, match="^/radio_link/channel_bandwidth_hz="):
            upgrade_v1(raw, source_dir=ISS_V1.parent, output_dir=V2_DIR)


def test_a_missing_catalog_is_named_with_the_reason(tmp_path: Path) -> None:
    raw = _put(_v1(ISS_V1), "nowhere/never.csv", "orbit", "path")
    with pytest.raises(ValueError) as caught:
        upgrade_v1(raw, source_dir=tmp_path, output_dir=tmp_path)
    message = str(caught.value)
    assert message.startswith("/orbit/path: could not read the catalog")
    assert "synthetic_rf" in message
    assert "never.csv" in message


def test_a_catalog_with_the_wrong_hash_is_refused_and_both_hashes_are_shown() -> None:
    wrong = "1" * 64
    raw = _put(_v1(ISS_V1), wrong, "orbit", "sha256")
    message = _pointer_error(raw, "/orbit/sha256", ISS_V1)
    assert wrong in message
    assert "ddb21d9a4a3a4812ee397751555b9d55d2767f30c33190afba90e4bbb8db13f9" in message


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        (b"\xff\xfe not utf-8", "UTF-8"),
        (b"NORAD_CAT_ID\n25544\n", "OBJECT_NAME"),
        (b"", "OBJECT_NAME"),
        (b'OBJECT_NAME\n"unterminated\n', "invalid"),
    ],
)
def test_an_unreadable_catalog_is_refused(content: bytes, reason: str, tmp_path: Path) -> None:
    (tmp_path / "bad.csv").write_bytes(content)
    raw = _v1(ISS_V1)
    raw["orbit"].update(path="bad.csv", sha256=sha256(content).hexdigest())
    with pytest.raises(ValueError) as caught:
        upgrade_v1(raw, source_dir=tmp_path, output_dir=tmp_path)
    assert str(caught.value).startswith("/orbit/path"), str(caught.value)
    assert reason in str(caught.value)


def test_an_oversized_catalog_is_refused(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(upgrade_module, "MAX_CATALOG_BYTES", 64)
    content = b"OBJECT_NAME\n" + b"ISS (ZARYA)\n" * 20
    (tmp_path / "big.csv").write_bytes(content)
    raw = _v1(ISS_V1)
    raw["orbit"].update(path="big.csv", sha256=sha256(content).hexdigest())
    with pytest.raises(ValueError, match=r"^/orbit/path: .*exceeds 64 bytes"):
        upgrade_v1(raw, source_dir=tmp_path, output_dir=tmp_path)


def test_the_catalog_size_limit_is_the_scenario_limit() -> None:
    assert upgrade_module.MAX_CATALOG_BYTES == 10_000_000


# --- files ----------------------------------------------------------------------------------------


def test_upgrade_file_writes_exactly_the_text_it_returns(tmp_path: Path) -> None:
    output = tmp_path / "iss.json"
    text = upgrade_file(ISS_V1, output)
    assert output.read_bytes() == text.encode("utf-8")
    assert load_scenario(output).name == _v1(ISS_V1)["name"]


def test_upgrade_file_writes_lf_line_endings_on_every_os(tmp_path: Path, monkeypatch) -> None:
    # Text mode would turn "\n" into "\r\n" on Windows and change the scenario's SHA-256
    # there. A Linux run cannot see that, so look at how the file is written as well as at it.
    calls = []
    original = Path.write_text

    def spy(self, data, encoding=None, errors=None, newline=None):
        calls.append(newline)
        return original(self, data, encoding=encoding, errors=errors, newline=newline)

    monkeypatch.setattr(Path, "write_text", spy)
    output = tmp_path / "iss.json"
    text = upgrade_file(ISS_V1, output)
    assert calls == ["\n"]
    assert b"\r" not in output.read_bytes()
    assert sha256(output.read_bytes()).hexdigest() == sha256(text.encode("utf-8")).hexdigest()


def test_upgrade_file_without_an_output_writes_nothing(tmp_path: Path) -> None:
    raw = _v1(ISS_V1)
    raw["orbit"]["path"] = str(EXAMPLES / "data" / "iss_2026-08-30.csv")
    source = tmp_path / "iss.json"
    source.write_text(json.dumps(raw), encoding="utf-8")
    upgrade_file(source, None)
    assert [path.name for path in tmp_path.iterdir()] == ["iss.json"]


def test_upgrade_file_keeps_non_ascii_text_readable(tmp_path: Path) -> None:
    raw = _v1(ISS_V1)
    raw["ground_station"]["name"] = "Cartagena – Tromsø"
    raw["orbit"]["path"] = str(EXAMPLES / "data" / "iss_2026-08-30.csv")
    source = tmp_path / "iss.json"
    source.write_text(json.dumps(raw), encoding="utf-8")
    output = tmp_path / "out.json"
    text = upgrade_file(source, output)
    assert "Cartagena – Tromsø" in text
    assert "\\u00f8" not in text
    assert load_scenario(output).ground_segment.stations[0].name == "Cartagena – Tromsø"


def test_an_absolute_v1_catalog_path_is_made_relative(tmp_path: Path) -> None:
    v1 = _example_tree(tmp_path)
    catalog_file = tmp_path / "examples" / "data" / "iss_2026-08-30.csv"
    raw = json.loads(v1.read_text(encoding="utf-8"))
    raw["orbit"]["path"] = str(catalog_file)  # absolute
    v1.write_text(json.dumps(raw), encoding="utf-8")
    (tmp_path / "out").mkdir()
    catalog = json.loads(upgrade_file(v1, tmp_path / "out" / "iss.json"))["space_segment"][
        "catalogs"
    ][0]
    assert catalog["path"] == "../examples/data/iss_2026-08-30.csv"


def test_upgrade_file_reports_an_upgrade_that_is_not_valid_v2_with_the_v2_pointer(
    tmp_path: Path,
) -> None:
    raw = _v1(ISS_V1)
    raw["ground_station"]["latitude_deg"] = 95.0
    raw["orbit"]["path"] = str(EXAMPLES / "data" / "iss_2026-08-30.csv")
    source = tmp_path / "iss.json"
    source.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError) as caught:
        upgrade_file(source, None)
    assert str(caught.value).startswith(
        f"{source}: the upgraded scenario is not valid scenario v2: "
        "/ground_segment/stations/0/latitude_deg=95.0"
    )


def test_upgrade_file_reports_file_level_errors_with_the_path(tmp_path: Path) -> None:
    missing = tmp_path / "missing.json"
    with pytest.raises(ValueError, match=f"^{re.escape(str(missing))}: could not load scenario"):
        upgrade_file(missing, None)
    bad = tmp_path / "bad.json"
    bad.write_text("{", encoding="utf-8")
    with pytest.raises(ValueError, match=f"^{re.escape(str(bad))}: invalid JSON"):
        upgrade_file(bad, None)
    output = tmp_path / "no" / "such" / "out.json"
    with pytest.raises(
        ValueError, match=f"^{re.escape(str(output))}: could not write the upgraded scenario"
    ):
        upgrade_file(ISS_V1, output)


def test_upgrade_file_rejects_repeated_keys(tmp_path: Path) -> None:
    text = ISS_V1.read_text(encoding="utf-8").replace(
        '"name": "iss-cartagena-s-band-free-space",',
        '"name": "a", "name": "b",',
    )
    source = tmp_path / "dup.json"
    source.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match="^/name is a duplicate JSON object key"):
        upgrade_file(source, None)


def test_upgrade_file_refuses_a_v2_scenario() -> None:
    with pytest.raises(ValueError, match="^/schema: .*nothing to upgrade"):
        upgrade_file(V2_DIR / "iss_cartagena.json", None)
