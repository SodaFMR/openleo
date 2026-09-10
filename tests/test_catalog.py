import csv
from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

import pytest
from skyfield.api import load

from openleo.model import OrbitSource, Provenance

ISS_CSV = Path("examples/data/iss_2026-08-30.csv")
IRIDIUM_CSV = Path("examples/data/iridium_next_2026-09-10.csv")


def _source(path):
    return OrbitSource(
        path=path,
        provenance=Provenance(
            source_url="https://celestrak.org/NORAD/elements/gp.php?GROUP=iridium-NEXT&FORMAT=CSV",
            retrieved_at_utc=datetime(2026, 9, 10, 22, 0, 25, tzinfo=UTC),
            terms_url="https://celestrak.org/usage-policy.php",
            sha256=sha256(path.read_bytes()).hexdigest(),
        ),
    )


def _rows():
    with ISS_CSV.open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


def _write(tmp_path, rows):
    path = tmp_path / "catalog.csv"
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    return path


def test_loads_frozen_catalog_in_numeric_norad_order():
    from openleo.catalog import load_catalog

    source = _source(IRIDIUM_CSV)
    catalog = load_catalog(source, load.timescale(builtin=True))

    assert isinstance(catalog, tuple)
    assert len(catalog) == 80
    assert [orbit.satellite.model.satnum for orbit in catalog] == sorted(
        orbit.satellite.model.satnum for orbit in catalog
    )
    assert catalog[0].satellite.name == "IRIDIUM 106"
    assert all(orbit.epoch_utc.tzinfo is UTC for orbit in catalog)
    assert source.provenance.sha256 == (
        "e9dac20f80bb4d0ae0090996619827aab1abf16d448ed8b0edf47b965abf6273"
    )
    assert all(orbit.actual_sha256 == source.provenance.sha256 for orbit in catalog)


def test_single_record_matches_existing_orbit_loader():
    from openleo.catalog import load_catalog
    from openleo.orbit import load_orbit

    ts = load.timescale(builtin=True)
    source = _source(ISS_CSV)
    orbit = load_catalog(source, ts)[0]
    original = load_orbit(source, ts)

    assert orbit.epoch_utc == original.epoch_utc
    assert orbit.satellite.at(orbit.satellite.epoch).position.km == pytest.approx(
        [2541.22048664, -6305.90893063, -6.41569470], abs=1e-8
    )


def test_checksum_verified_before_csv_parse(tmp_path):
    from openleo.catalog import load_catalog

    path = tmp_path / "tampered.csv"
    path.write_bytes(b"not a catalog")
    source = _source(path)
    source = replace(source, provenance=replace(source.provenance, sha256="0" * 64))
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        load_catalog(source, load.timescale(builtin=True))


def test_duplicate_norad_ids_rejected_even_with_different_names(tmp_path):
    from openleo.catalog import load_catalog

    row = _rows()[0]
    path = _write(tmp_path, [row, {**row, "OBJECT_NAME": "SECOND NAME"}])
    with pytest.raises(ValueError, match="duplicate.*25544"):
        load_catalog(_source(path), load.timescale(builtin=True))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("MEAN_MOTION", "nan"),
        ("MEAN_MOTION", "0"),
        ("MEAN_MOTION", "1e400"),
        ("ECCENTRICITY", "1.0"),
        ("ECCENTRICITY", "-0.01"),
        ("INCLINATION", "181"),
        ("RA_OF_ASC_NODE", "-1"),
        ("ARG_OF_PERICENTER", "360"),
        ("MEAN_ANOMALY", "bad"),
        ("BSTAR", "inf"),
        ("MEAN_MOTION_DOT", "nan"),
        ("MEAN_MOTION_DDOT", "nan"),
        ("NORAD_CAT_ID", "0"),
        ("NORAD_CAT_ID", "2.5"),
        ("REV_AT_EPOCH", "-1"),
        ("ELEMENT_SET_NO", "none"),
        ("EPOCH", "2026-09-10T12:00:00+01:00"),
        ("EPOCH", "bad"),
        ("EPHEMERIS_TYPE", "9"),
        ("OBJECT_NAME", ""),
        ("CLASSIFICATION_TYPE", "bad"),
    ],
)
def test_catalog_rejects_invalid_elements_before_propagation(tmp_path, field, value):
    from openleo.catalog import load_catalog

    path = _write(tmp_path, [{**_rows()[0], field: value}])
    with pytest.raises(ValueError, match=field):
        load_catalog(_source(path), load.timescale(builtin=True))


def test_catalog_rejects_missing_header_empty_and_excess_records(tmp_path):
    from openleo.catalog import load_catalog

    row = _rows()[0]
    missing = {key: value for key, value in row.items() if key != "MEAN_MOTION"}
    path = _write(tmp_path, [missing])
    with pytest.raises(ValueError, match="missing required GP fields.*MEAN_MOTION"):
        load_catalog(_source(path), load.timescale(builtin=True))
    path.write_text(",".join(row) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="at least one"):
        load_catalog(_source(path), load.timescale(builtin=True))
    path = _write(tmp_path, [{**row, "NORAD_CAT_ID": str(10000 + i)} for i in range(129)])
    with pytest.raises(ValueError, match="128"):
        load_catalog(_source(path), load.timescale(builtin=True))


def test_catalog_rejects_bad_bytes_row_width_and_duplicate_header(tmp_path):
    from openleo.catalog import load_catalog

    path = tmp_path / "catalog.csv"
    for contents, message in (
        (b"\xff", "UTF-8"),
        (b" " * 1_000_001, "exceeds 1000000"),
        (ISS_CSV.read_bytes().replace(b"BSTAR,", b"EPOCH,"), "duplicate.*header"),
        (ISS_CSV.read_bytes().rstrip() + b",extra\n", "fields|columns"),
    ):
        path.write_bytes(contents)
        with pytest.raises(ValueError, match=message):
            load_catalog(_source(path), load.timescale(builtin=True))


def test_catalog_missing_file_reports_read_failure(tmp_path):
    from openleo.catalog import load_catalog

    source = replace(_source(ISS_CSV), path=tmp_path / "missing.csv")
    with pytest.raises(ValueError, match="could not read"):
        load_catalog(source, load.timescale(builtin=True))
