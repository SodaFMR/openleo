"""Catalog loading for the v2 engine: order, selection, duplicates, hashes and size limits."""

import csv
import json
from datetime import UTC, datetime
from hashlib import sha256
from math import tau
from pathlib import Path

import pytest
from skyfield.api import load

from openleo.scenario.model import CatalogSource, SpaceSegment
from openleo.scenario.parse import parse_scenario

ROOT = Path(__file__).resolve().parents[2]
IRIDIUM_CSV = ROOT / "examples" / "data" / "iridium_next_2026-09-10.csv"
ISS_CSV = ROOT / "examples" / "data" / "iss_2026-08-30.csv"
CATALOG_INVALID = sorted(
    (ROOT / "tests" / "scenario" / "fixtures" / "catalog_invalid").glob("*.json")
)
TIMESCALE = load.timescale(builtin=True)


def _source(path: Path, digest: str | None = None) -> CatalogSource:
    return CatalogSource(
        path=path,
        format="celestrak_gp_csv",
        source_url="https://celestrak.org/NORAD/elements/gp.php?GROUP=iridium-NEXT&FORMAT=CSV",
        retrieved_at_utc=datetime(2026, 9, 10, 22, 0, 25, tzinfo=UTC),
        terms_url="https://celestrak.org/usage-policy.php",
        sha256=digest or sha256(path.read_bytes()).hexdigest(),
    )


def _space(*paths: Path, select: tuple[int, ...] | None = None) -> SpaceSegment:
    return SpaceSegment(
        catalogs=tuple(_source(path) for path in paths), select_norad_ids=select, synthetic_rf=True
    )


def _rows(path: Path) -> list[dict]:
    with path.open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


def _write(path: Path, rows: list[dict]) -> Path:
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    return path


def _norad_ids(orbits) -> list[int]:
    return [orbit.satellite.model.satnum for orbit in orbits]


def test_catalogs_load_in_catalog_then_row_order(tmp_path: Path) -> None:
    from openleo.engine.catalogs import load_catalogs

    # Row order, not NORAD order: the rows are written in reverse.
    rows = _rows(IRIDIUM_CSV)[:5][::-1]
    reversed_csv = _write(tmp_path / "reversed.csv", rows)

    orbits = load_catalogs(_space(ISS_CSV, reversed_csv), TIMESCALE)

    assert isinstance(orbits, tuple)
    assert _norad_ids(orbits) == [25544, *(int(row["NORAD_CAT_ID"]) for row in rows)]
    assert [orbit.satellite.name for orbit in orbits[1:]] == [row["OBJECT_NAME"] for row in rows]
    assert all(orbit.epoch_utc.tzinfo is UTC for orbit in orbits)
    assert orbits[0].actual_sha256 == sha256(ISS_CSV.read_bytes()).hexdigest()


def test_select_norad_ids_keeps_catalog_order_not_selection_order() -> None:
    from openleo.engine.catalogs import load_catalogs

    space = _space(IRIDIUM_CSV, ISS_CSV, select=(25544, 41920, 41917))

    assert _norad_ids(load_catalogs(space, TIMESCALE)) == [41917, 41920, 25544]


def test_select_norad_ids_unknown_id_rejected_with_pointer() -> None:
    from openleo.engine.catalogs import load_catalogs

    space = _space(IRIDIUM_CSV, select=(41917, 99999))

    with pytest.raises(ValueError) as caught:
        load_catalogs(space, TIMESCALE)
    message = str(caught.value)
    assert message.startswith("/space_segment/select_norad_ids/1"), message
    assert "99999" in message


@pytest.mark.parametrize("path", CATALOG_INVALID, ids=lambda path: path.stem)
def test_catalog_invalid_fixtures_rejected_by_load_catalogs(path: Path) -> None:
    from openleo.engine.catalogs import load_catalogs

    fixture = json.loads(path.read_text(encoding="utf-8"))
    # The document itself is valid: only loading the catalogs finds the problem.
    scenario = parse_scenario(fixture["scenario"], path, "0" * 64)

    with pytest.raises(ValueError) as caught:
        load_catalogs(scenario.space_segment, TIMESCALE)
    message = str(caught.value)
    assert type(caught.value) is ValueError
    pointer = fixture["error_startswith"]
    assert message.startswith(pointer), message
    assert message[len(pointer) : len(pointer) + 1] in {" ", "=", ":"}, message
    assert fixture["error_contains"] in message


def test_catalog_invalid_corpus_present() -> None:
    assert {path.stem for path in CATALOG_INVALID} >= {
        "duplicate_norad_across_catalogs",
        "catalog_sha256_mismatch",
    }


def test_duplicate_norad_within_one_catalog_names_pointer_and_id(tmp_path: Path) -> None:
    from openleo.engine.catalogs import load_catalogs

    row = _rows(ISS_CSV)[0]
    path = _write(tmp_path / "twice.csv", [row, {**row, "OBJECT_NAME": "SECOND NAME"}])

    with pytest.raises(ValueError, match=r"^/space_segment/catalogs/1: .*25544"):
        load_catalogs(_space(IRIDIUM_CSV, path), TIMESCALE)


def test_catalog_errors_carry_the_catalog_pointer(tmp_path: Path) -> None:
    from openleo.engine.catalogs import load_catalogs

    row = _rows(ISS_CSV)[0]
    path = _write(tmp_path / "bad.csv", [{**row, "ECCENTRICITY": "1.0"}])
    with pytest.raises(ValueError, match=r"^/space_segment/catalogs/1: .*ECCENTRICITY"):
        load_catalogs(_space(IRIDIUM_CSV, path), TIMESCALE)

    missing = SpaceSegment(
        (_source(ISS_CSV), _source(tmp_path / "missing.csv", "0" * 64)), None, True
    )
    with pytest.raises(ValueError, match=r"^/space_segment/catalogs/1: .*could not read"):
        load_catalogs(missing, TIMESCALE)


def test_catalog_over_10_mb_rejected(tmp_path: Path) -> None:
    from openleo.engine.catalogs import MAX_CATALOG_BYTES, load_catalogs

    assert MAX_CATALOG_BYTES == 10_000_000
    path = tmp_path / "huge.csv"
    path.write_bytes(ISS_CSV.read_bytes().ljust(MAX_CATALOG_BYTES + 1, b"\n"))

    with pytest.raises(ValueError, match=r"^/space_segment/catalogs/0: .*exceeds 10000000 bytes"):
        load_catalogs(_space(path), TIMESCALE)


def test_catalog_size_limit_is_inclusive(monkeypatch) -> None:
    from openleo.engine import catalogs

    size = len(ISS_CSV.read_bytes())
    monkeypatch.setattr(catalogs, "MAX_CATALOG_BYTES", size)
    assert _norad_ids(catalogs.load_catalogs(_space(ISS_CSV), TIMESCALE)) == [25544]
    monkeypatch.setattr(catalogs, "MAX_CATALOG_BYTES", size - 1)
    with pytest.raises(ValueError, match=rf"exceeds {size - 1} bytes"):
        catalogs.load_catalogs(_space(ISS_CSV), TIMESCALE)


def test_catalog_over_20000_objects_rejected(tmp_path: Path) -> None:
    from openleo.engine.catalogs import MAX_SATELLITES, load_catalogs

    assert MAX_SATELLITES == 20_000
    row = _rows(ISS_CSV)[0]
    rows = [{**row, "NORAD_CAT_ID": str(10_000 + index)} for index in range(MAX_SATELLITES + 1)]
    path = _write(tmp_path / "many.csv", rows)

    with pytest.raises(ValueError, match=r"^/space_segment/catalogs/0: .*exceeds 20000"):
        load_catalogs(_space(path), TIMESCALE)


def test_selected_satellites_over_the_limit_rejected(monkeypatch) -> None:
    from openleo.engine import catalogs

    # Each catalog is within the limit, but together they exceed it.
    monkeypatch.setattr(catalogs, "MAX_SATELLITES", 80)
    with pytest.raises(ValueError, match=r"^/space_segment/catalogs: 81 satellites.*80"):
        catalogs.load_catalogs(_space(IRIDIUM_CSV, ISS_CSV), TIMESCALE)
    # A selection brings the run back within the limit.
    selected = _space(IRIDIUM_CSV, ISS_CSV, select=(25544,))
    assert _norad_ids(catalogs.load_catalogs(selected, TIMESCALE)) == [25544]


def test_minimum_period_is_two_pi_over_the_largest_mean_motion() -> None:
    from openleo.engine.catalogs import load_catalogs, minimum_period_s

    orbits = load_catalogs(_space(IRIDIUM_CSV, ISS_CSV), TIMESCALE)
    fastest_rev_per_day = max(float(row["MEAN_MOTION"]) for row in _rows(ISS_CSV))

    # The ISS (15.49 rev/day) is faster than every Iridium NEXT satellite (14.34 rev/day).
    assert minimum_period_s(orbits) == pytest.approx(86_400.0 / fastest_rev_per_day, rel=1e-12)
    assert minimum_period_s(orbits[:80]) == pytest.approx(
        86_400.0 / max(float(row["MEAN_MOTION"]) for row in _rows(IRIDIUM_CSV)), rel=1e-12
    )
    assert minimum_period_s(orbits[:1]) == pytest.approx(
        tau / (orbits[0].satellite.model.no_kozai / 60.0), rel=1e-15
    )
    with pytest.raises(ValueError, match="at least one"):
        minimum_period_s(())
