import csv
import json
from copy import deepcopy
from functools import lru_cache
from hashlib import sha256
from io import StringIO
from pathlib import Path

import pytest

from openleo.workbench import load_experiment, render_workbench, write_workbench


@lru_cache(maxsize=1)
def _computed_document():
    from openleo.constellation import parse_constellation, simulate_constellation
    from openleo.network import add_network

    path = Path("examples/scenarios/iss_cartagena.json")
    original = json.loads(path.read_text(encoding="utf-8"))
    raw = {
        "name": "A <script>alert(1)</script> experiment",
        "orbit": original["orbit"],
        "stations": [
            original["ground_station"],
            {**original["ground_station"], "name": "Neighbor", "longitude_deg": -0.95},
        ],
        "time_window": {
            **original["time_window"],
            "start_utc": "2026-08-30T06:17:00Z",
            "stop_utc": "2026-08-30T06:17:20Z",
        },
        "radio_link": {**original["radio_link"], "eirp_dbw": -100.0},
        "adaptation": {
            "symbol_rate_baud": 500_000.0,
            "rolloff": 0.2,
            "implementation_margin_db": 1.0,
            "hysteresis_db": 0.5,
        },
        "network": {
            "source_station": "Cartagena",
            "target_station": "Neighbor",
            "isl_max_range_m": 5_000_000.0,
            "isl_capacity_bps": 10_000_000.0,
            "fixed_capacity_bps": 1_000_000.0,
        },
    }
    scenario = parse_constellation(raw, path, sha256(json.dumps(raw).encode("utf-8")).hexdigest())
    return add_network(simulate_constellation(scenario))


def document():
    return deepcopy(_computed_document())


def test_bundle_round_trip_and_hashes(tmp_path):
    source = document()
    before = json.dumps(source)
    first = write_workbench(source, tmp_path / "first")
    second = write_workbench(source, tmp_path / "second")
    assert {path.name for path in first} == {
        "experiment.json",
        "links.csv",
        "routes.csv",
        "manifest.json",
        "explorer.html",
    }
    assert [path.read_bytes() for path in first] == [path.read_bytes() for path in second]
    assert load_experiment(tmp_path / "first") == source
    manifest = json.loads((tmp_path / "first/manifest.json").read_text())
    for name, checksum in manifest["files"].items():
        assert sha256((tmp_path / "first" / name).read_bytes()).hexdigest() == checksum
    rows = list(csv.DictReader(StringIO((tmp_path / "first/links.csv").read_text())))
    assert float(rows[0]["range_m"]) == pytest.approx(source["links"][0][0]["range_m"])
    assert rows[0]["modcod"] == ""
    assert json.dumps(source) == before


def test_html_escapes_script_payload_and_works_without_remote_assets():
    html = render_workbench(document())
    assert "<script>alert(1)</script>" not in html
    assert r"\u003cscript\u003ealert(1)\u003c/script\u003e" in html
    assert 'id="experiment-data"' in html
    assert 'id="session-data"' in html
    assert '<script src="http' not in html
    assert '<link href="http' not in html
    assert "Content-Security-Policy" in html


@pytest.mark.parametrize("change", ["hash", "scenario", "missing"])
def test_canonical_scenario_provenance_rejects_inconsistent_records(tmp_path, change):
    source = document()
    encoded = json.dumps(source["scenario"], sort_keys=True, separators=(",", ":"))
    if change == "scenario":
        encoded = json.dumps({**source["scenario"], "name": "a different scenario"})
    provenance = {
        **source["provenance"],
        "scenario_hash_encoding": "canonical JSON sorted compact UTF-8",
        "scenario_canonical_json": encoded,
        "scenario_sha256": sha256(encoded.encode()).hexdigest(),
    }
    if change == "hash":
        provenance["scenario_sha256"] = "0" * 64
    if change == "missing":
        provenance.pop("scenario_canonical_json")
    with pytest.raises(ValueError, match="canonical"):
        write_workbench({**source, "provenance": provenance}, tmp_path / "bad")
    assert not (tmp_path / "bad").exists()


@pytest.mark.parametrize(
    "filename", ["experiment.json", "links.csv", "routes.csv", "explorer.html"]
)
def test_bundle_rejects_changed_artifact(tmp_path, filename):
    write_workbench(document(), tmp_path)
    path = tmp_path / filename
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ValueError, match="SHA-256"):
        load_experiment(tmp_path)


def test_bundle_rejects_manifest_path_traversal(tmp_path):
    write_workbench(document(), tmp_path)
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    manifest["files"]["../outside"] = "0" * 64
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="files"):
        load_experiment(tmp_path)


def test_bundle_rejects_nonfinite_numbers_without_creating_outputs(tmp_path):
    source = {**document(), "bad": float("nan")}
    with pytest.raises(ValueError):
        write_workbench(source, tmp_path / "bad")
    assert not (tmp_path / "bad").exists()


def test_reader_rejects_oversized_and_missing_manifest(tmp_path):
    with pytest.raises(ValueError, match="manifest"):
        load_experiment(tmp_path)
    (tmp_path / "manifest.json").write_bytes(b" " * 1_000_001)
    with pytest.raises(ValueError, match="exceeds"):
        load_experiment(tmp_path)


_MISSING = object()


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("satellites", 0, "positions_ecef_m"), _MISSING),
        (("satellites", 0, "positions_ecef_m"), [[1.0, 2.0, 3.0]]),
        (("satellites", 0, "positions_ecef_m", 0), [1.0, 2.0]),
        (("satellites", 0, "positions_ecef_m", 0), [True, 2.0, 3.0]),
        (("satellites", 0, "positions_ecef_m", 0), [float("inf"), 2.0, 3.0]),
        (("satellites", 0, "norad_id"), True),
        (("satellites", 0, "epoch_utc"), "not a timestamp"),
        (("stations", 0, "position_ecef_m"), _MISSING),
        (("stations", 0, "position_ecef_m"), [float("nan"), 0.0, 0.0]),
        (("stations", 0, "latitude_deg"), 91.0),
        (("stations", 0, "name"), ""),
        (("timestamps_utc", 1), "2026-08-30T06:17:00Z"),
        (("timestamps_utc", 1), "not a timestamp"),
        (("timestamps_utc", 1), "2026-08-30T06:17:10"),
        (("links", 0, 0, "station_index"), 2),
        (("links", 0, 0, "satellite_index"), 1),
        (("links", 0, 0, "station_index"), False),
        (("links", 0, 0, "delay_s"), _MISSING),
        (("links", 0, 0, "delay_s"), 0.0),
        (("links", 0, 0, "range_rate_mps"), "100"),
        (("links", 0, 0, "modcod"), "unknown"),
        (("links", 0, 0, "modcod"), "QPSK 1/4"),
        (("links", 0, 0, "rate_bps"), 10.0),
        (("links", 0, 0, "margin_db"), 1.0),
        (("links", 0, 0, "contact_truncated"), "true"),
        (("links", 0, 0, "remaining_contact_s"), -1.0),
        (("network", "source_station_index"), 2),
        (("network", "target_station_index"), 0),
        (("network", "frames", 0, "fixed_capacity", "nodes"), [1, 99, 2]),
        (("network", "frames", 0, "fixed_capacity", "nodes"), [1, 0, 0, 2]),
        (("network", "frames", 0, "fixed_capacity", "nodes"), [2, 0, 1]),
        (("network", "frames", 0, "fixed_capacity", "nodes"), [1, 2]),
        (("network", "frames", 0, "fixed_capacity", "delay_s"), "0.01"),
        (("network", "frames", 0, "fixed_capacity", "bottleneck_bps"), 0.0),
        (("network", "frames", 0, "isl_edges"), [[0, 1, 0.1]]),
        (("network", "frames", 0, "isl_edges"), [[0, 0, 0.1]]),
        (("network", "summary"), []),
        (("network", "summary", "minimum_delay", "connected_sample_fraction"), 2.0),
        (("network", "summary", "minimum_delay", "integrated_bottleneck_bits"), -1.0),
        (("network", "summary", "minimum_delay", "route_changes"), 0.5),
        (("statistics",), []),
        (("statistics", 0, "station_index"), 1),
        (("statistics", 0, "usable_sample_fraction"), True),
        (("statistics", 0, "best_link_integrated_bits"), -1.0),
        (("statistics", 0, "handover_count"), 3),
        (("models", "coordinates"), _MISSING),
        (("models", "coordinates", "frame"), "TEME"),
        (("models", "adaptation", "modcod_table"), []),
        (("provenance", "scenario_sha256"), "not a checksum"),
        (("provenance", "orbit", "retrieved_at_utc"), "not a timestamp"),
        (("provenance", "software"), _MISSING),
        (("warnings",), "a warning"),
        (("limitations",), [None]),
    ],
    ids=[
        "missing_positions",
        "wrong_position_count",
        "short_position",
        "boolean_geometry",
        "nonfinite_geometry",
        "boolean_norad",
        "invalid_epoch",
        "missing_station_position",
        "nonfinite_station",
        "bad_latitude",
        "empty_station_name",
        "unordered_timestamp",
        "bad_timestamp",
        "non_utc_timestamp",
        "bad_station_index",
        "bad_link_index",
        "boolean_index",
        "missing_metric",
        "zero_delay",
        "string_metric",
        "unknown_modcod",
        "locked_zero_rate",
        "unlocked_positive_rate",
        "unlocked_positive_margin",
        "string_contact_flag",
        "negative_contact",
        "bad_source_index",
        "same_endpoints",
        "bad_route_node",
        "cyclic_route",
        "reversed_endpoints",
        "direct_ground_route",
        "string_route_delay",
        "zero_bottleneck",
        "bad_isl_node",
        "self_isl",
        "malformed_summary",
        "bad_fraction",
        "negative_integral",
        "fractional_changes",
        "missing_statistics",
        "wrong_statistics_index",
        "boolean_fraction",
        "negative_statistics",
        "too_many_handovers",
        "missing_model",
        "wrong_frame",
        "missing_modcod_table",
        "bad_provenance_hash",
        "bad_retrieval_time",
        "missing_software",
        "string_warnings",
        "bad_limitations",
    ],
)
def test_rejects_invalid_consumed_fields_before_rendering_or_writing(tmp_path, path, value):
    source = document()
    target = source
    for key in path[:-1]:
        target = target[key]
    if value is _MISSING:
        target.pop(path[-1])
    else:
        target[path[-1]] = value
    with pytest.raises(ValueError):
        render_workbench(source)
    with pytest.raises(ValueError):
        write_workbench(source, tmp_path / "invalid")
    assert not (tmp_path / "invalid").exists()


@pytest.mark.parametrize(
    ("field", "limit"), [("satellites", 129), ("stations", 17), ("timestamps_utc", 1442)]
)
def test_rejects_excessive_dimensions_before_outputs(tmp_path, field, limit):
    source = document()
    source[field] = [source[field][0]] * limit
    with pytest.raises(ValueError):
        write_workbench(source, tmp_path / "invalid")
    assert not (tmp_path / "invalid").exists()


def test_reader_rejects_malformed_document_even_with_matching_checksum(tmp_path):
    write_workbench(document(), tmp_path)
    source = document()
    source["network"]["frames"][0]["fixed_capacity"]["nodes"] = [1, 99, 2]
    raw = json.dumps(source).encode("utf-8")
    (tmp_path / "experiment.json").write_bytes(raw)
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    manifest["files"]["experiment.json"] = sha256(raw).hexdigest()
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        load_experiment(tmp_path)


def test_portable_validation_needs_no_catalog_and_recomputes_no_models(tmp_path, monkeypatch):
    source = document()
    source["scenario"]["orbit"]["path"] = "catalog-not-present.csv"

    def forbidden(*args, **kwargs):
        pytest.fail("artifact validation must not load or propagate an orbit")

    monkeypatch.setattr("openleo.constellation.load_catalog", forbidden)
    monkeypatch.setattr("openleo.constellation.simulate_constellation", forbidden)
    write_workbench(source, tmp_path / "portable")
    assert load_experiment(tmp_path / "portable") == source
