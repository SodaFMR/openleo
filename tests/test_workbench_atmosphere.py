"""Portable atmospheric output and routing preserve corrected link quantities."""

import csv
from copy import deepcopy
from functools import lru_cache
from io import StringIO

import pytest

from openleo.network import add_network
from openleo.workbench import (
    LINK_FIELDS,
    links_csv,
    load_experiment,
    render_workbench,
    write_workbench,
)

ATMOSPHERIC_FIELDS = (
    "gaseous_dry_attenuation_db",
    "gaseous_water_attenuation_db",
    "gaseous_attenuation_db",
    "free_space_cn0_db_hz",
    "geometric_delay_s",
    "atmospheric_excess_delay_s",
    "apparent_elevation_deg",
)


@lru_cache(maxsize=1)
def _reference_document():
    from test_constellation_atmosphere import reference_scenario

    from openleo.constellation import simulate_constellation

    return add_network(simulate_constellation(reference_scenario()))


def _replace(value, path, replacement):
    if not path:
        return replacement
    if isinstance(value, list):
        return [
            _replace(item, path[1:], replacement) if index == path[0] else item
            for index, item in enumerate(value)
        ]
    return {**value, path[0]: _replace(value[path[0]], path[1:], replacement)}


def test_atmospheric_bundle_round_trip_requires_no_orbit_or_atmosphere_recomputation(
    tmp_path, monkeypatch
):
    source = deepcopy(_reference_document())

    def forbidden(*args, **kwargs):
        pytest.fail("portable validation must not recompute orbit or atmospheric models")

    monkeypatch.setattr("openleo.constellation.load_catalog", forbidden)
    monkeypatch.setattr("openleo.constellation.simulate_constellation", forbidden)
    monkeypatch.setattr("openleo.constellation.build_reference_column", forbidden)
    monkeypatch.setattr("openleo.atmospheric_path.build_reference_column", forbidden)
    write_workbench(source, tmp_path / "reference")
    assert load_experiment(tmp_path / "reference") == source
    rows = csv.DictReader(StringIO((tmp_path / "reference/links.csv").read_text()))
    assert rows.fieldnames == list(LINK_FIELDS + ATMOSPHERIC_FIELDS)
    row = next(rows)
    for field in ATMOSPHERIC_FIELDS:
        assert float(row[field]) == pytest.approx(source["links"][0][0][field])


@pytest.mark.parametrize("field", ATMOSPHERIC_FIELDS)
@pytest.mark.parametrize("value", [True, float("inf"), "1"])
def test_atmospheric_links_require_finite_numeric_fields(field, value):
    source = _replace(_reference_document(), ("links", 0, 0, field), value)
    with pytest.raises(ValueError, match=field):
        render_workbench(source)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("gaseous_dry_attenuation_db", -0.1),
        ("gaseous_water_attenuation_db", -0.1),
        ("gaseous_attenuation_db", -0.1),
        ("gaseous_attenuation_db", 100.0),
        ("free_space_cn0_db_hz", 100.0),
        ("cn0_db_hz", 100.0),
        ("snr_db", 100.0),
        ("esn0_db", 100.0),
        ("geometric_delay_s", 0.0),
        ("geometric_delay_s", 1.0),
        ("atmospheric_excess_delay_s", -1e-9),
        ("atmospheric_excess_delay_s", 1.0),
        ("delay_s", 1.0),
        ("apparent_elevation_deg", 0.0),
        ("apparent_elevation_deg", 91.0),
    ],
)
def test_atmospheric_bundle_rejects_inconsistent_link_quantities(tmp_path, field, value):
    source = _replace(_reference_document(), ("links", 0, 0, field), value)
    with pytest.raises(ValueError):
        write_workbench(source, tmp_path / "bad")
    assert not (tmp_path / "bad").exists()


@pytest.mark.parametrize("schema_version", ["1", "2"])
def test_schema_version_must_match_the_propagation_configuration(schema_version):
    from test_workbench import document

    source = _reference_document() if schema_version == "1" else document()
    with pytest.raises(ValueError, match="propagation"):
        render_workbench({**source, "schema_version": schema_version})


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("models", "propagation", "model"), "unknown"),
        (("models", "propagation", "refinement"), True),
        (("models", "propagation", "profile", "sha256"), "0" * 64),
        (("models", "propagation", "ray", "earth_radius_km"), 7000.0),
        (("models", "propagation"), {}),
        (("scenario", "propagation", "refinement"), 2),
        (("scenario", "propagation", "station_heights_amsl_m", "Cartagena"), 1000.0),
    ],
)
def test_propagation_metadata_must_match_the_declared_configuration(path, value):
    source = _replace(_reference_document(), path, value)
    with pytest.raises(ValueError, match="propagation"):
        render_workbench(source)


def test_schema_one_rejects_atmospheric_metadata():
    from test_workbench import document

    original = document()
    source = {
        **original,
        "models": {
            **original["models"],
            "propagation": _reference_document()["models"]["propagation"],
        },
    }
    with pytest.raises(ValueError, match="propagation"):
        render_workbench(source)


def test_schema_two_links_require_all_atmospheric_fields():
    original = _reference_document()
    link = {
        key: value
        for key, value in original["links"][0][0].items()
        if key != "gaseous_attenuation_db"
    }
    with pytest.raises(ValueError, match="link"):
        render_workbench(_replace(original, ("links", 0, 0), link))


@pytest.mark.parametrize("schema_version", ["1", "2"])
def test_link_csv_appends_atmospheric_columns_only_for_schema_two(schema_version):
    link = dict(zip(ATMOSPHERIC_FIELDS, (0.1, 0.2, 0.3, 80.0, 0.001, 1e-8, 30.1), strict=True))
    document = {
        "schema_version": schema_version,
        "timestamps_utc": ["2026-09-10T12:00:00Z"],
        "links": [[link]],
    }
    rows = csv.DictReader(StringIO(links_csv(document)))
    expected = LINK_FIELDS + (ATMOSPHERIC_FIELDS if schema_version == "2" else ())
    assert rows.fieldnames == list(expected)
    row = next(rows)
    if schema_version == "2":
        assert {field: float(row[field]) for field in ATMOSPHERIC_FIELDS} == link


def test_schema_two_network_uses_corrected_ground_delays_and_rates():
    document = {
        "schema_version": "2",
        "kind": "openleo.constellation",
        "scenario": {
            "network": {
                "source_station": "Source",
                "target_station": "Target",
                "isl_max_range_m": 0.0,
                "isl_capacity_bps": 100.0,
                "fixed_capacity_bps": 2.0,
            }
        },
        "timestamps_utc": ["2026-09-10T12:00:00Z"],
        "stations": [{"name": "Source"}, {"name": "Target"}],
        "satellites": [{"positions_ecef_m": [[7_000_000.0, 0.0, 0.0]]}],
        "links": [
            [
                {
                    "station_index": station,
                    "satellite_index": 0,
                    "delay_s": delay,
                    "rate_bps": rate,
                    "geometric_delay_s": geometric,
                    "atmospheric_excess_delay_s": excess,
                }
                for station, delay, rate, geometric, excess in (
                    (0, 0.00100001, 4.0, 0.001, 1e-8),
                    (1, 0.00200002, 3.0, 0.002, 2e-8),
                )
            ]
        ],
    }
    result = add_network(document)
    assert "network" not in document
    for model in ("minimum_delay", "maximum_rate", "fixed_capacity"):
        assert result["network"]["frames"][0][model] == {
            "nodes": [1, 0, 2],
            "delay_s": pytest.approx(0.00300003),
            "bottleneck_bps": 2.0 if model == "fixed_capacity" else 3.0,
        }
