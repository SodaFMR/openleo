"""Writing, loading, CSV/Parquet, manifests and atomic publication of results v2."""

import hashlib
import json
import sys
from pathlib import Path

import numpy as np
import pytest

from openleo.results.io import load_results, publish_atomically, write_results
from openleo.results.manifest import (
    build_manifest,
    manifest_digest,
    verify_manifest,
    write_manifest,
)
from openleo.results.model import Column, Results, Table

ALL = ("csv", "olc")


def _write(results: Results, directory: Path, formats=ALL, inputs=()) -> Path:
    return write_results(results, directory, formats, inputs)


def _tree(directory: Path) -> dict[str, bytes]:
    return {
        p.relative_to(directory).as_posix(): p.read_bytes()
        for p in sorted(directory.rglob("*"))
        if p.is_file()
    }


def test_write_results_layout(sample_results: Results, tmp_path: Path) -> None:
    run = tmp_path / "run"
    assert _write(sample_results, run) == run
    assert sorted(_tree(run)) == [
        "manifest.json",
        "results.json",
        "tables/all_dtypes.csv",
        "tables/all_dtypes.olc",
        "tables/events.csv",
        "tables/events.olc",
    ]
    meta = json.loads((run / "results.json").read_text(encoding="utf-8"))
    assert meta["note"] == "café"
    assert "café" in (run / "results.json").read_text(encoding="utf-8")
    entry = meta["tables"]["events"]
    assert entry["rows"] == 4
    assert entry["columns"][2] == {
        "name": "t_offset_s",
        "dtype": "f64",
        "unit": "s",
        "description": "event time",
    }
    files = entry["files"]
    assert set(files) == {"csv", "olc"}
    assert files["csv"]["path"] == "tables/events.csv"
    digest = hashlib.sha256((run / "tables/events.olc").read_bytes()).hexdigest()
    assert files["olc"] == {"path": "tables/events.olc", "sha256": digest}
    text = (run / "results.json").read_text(encoding="utf-8")
    assert text.endswith("\n") and "\r" not in text
    assert list(json.loads(text)) == sorted(json.loads(text))


def test_outputs_are_byte_identical(sample_results: Results, tmp_path: Path) -> None:
    _write(sample_results, tmp_path / "a")
    _write(sample_results, tmp_path / "b")
    assert _tree(tmp_path / "a") == _tree(tmp_path / "b")


def test_csv_determinism_and_nan_empty(events: Table, tmp_path: Path) -> None:
    results = Results({"time": {"start_utc": "2026-09-10T12:00:00Z"}}, {"events": events})
    _write(results, tmp_path / "a")
    _write(results, tmp_path / "b")
    raw = (tmp_path / "a/tables/events.csv").read_bytes()
    assert raw == (tmp_path / "b/tables/events.csv").read_bytes()
    assert b"\r" not in raw
    lines = raw.decode("utf-8").split("\n")
    assert lines[0] == ("station_index,satellite_index,t_offset_s,t_utc,kind,rate_bps,truncated")
    # ordered by station_index, satellite_index, then the offset; NaN offset and rate are empty
    assert lines[1:-1] == [
        "0,1,90.25,2026-09-10T12:01:30.250000Z,culmination,2500000.0,false",
        "0,1,,,aos,0.0,true",
        "0,2,120.0,2026-09-10T12:02:00Z,los,,true",
        "1,0,5.5,2026-09-10T12:00:05.500000Z,aos,1000000.0,false",
    ]
    assert lines[-1] == ""


def test_csv_floats_use_repr_and_booleans_words(all_dtypes: Table, tmp_path: Path) -> None:
    run = tmp_path / "run"
    _write(Results({}, {"all_dtypes": all_dtypes}), run)
    lines = (run / "tables/all_dtypes.csv").read_text(encoding="utf-8").split("\n")
    assert lines[0] == "a_f64,b_f32,c_i64,d_i32,e_u8,f_str"
    assert lines[1] == f"0.1,0.1,{-(2**63)},{-(2**31)},false,"
    assert lines[2].startswith("-2.5,-2.5,0,-1,true,ascii")
    assert lines[3].startswith("1e+300,3e+38,")
    # key-less table keeps its row order; NaN float is an empty cell, inf is spelled inf
    assert lines[4].startswith(",inf,")


def test_csv_key_column_rule(tmp_path: Path) -> None:
    columns = (
        Column("value", "i32", "1", ""),
        Column("end_offset_s", "f64", "s", ""),
        Column("b_index", "i32", "1", ""),
        Column("a_index", "i32", "1", ""),
    )
    data = {
        "value": np.array([0, 1, 2, 3, 4], dtype=np.int32),
        "end_offset_s": np.array([9.0, 1.0, 5.0, 3.0, 2.0]),
        "b_index": np.array([1, 1, 0, 0, 1], dtype=np.int32),
        "a_index": np.array([0, 0, 0, 0, 0], dtype=np.int32),
    }
    results = Results(
        {"time": {"start_utc": "2026-01-01T00:00:00Z"}}, {"k": Table("k", columns, data)}
    )
    _write(results, tmp_path / "run")
    rows = (tmp_path / "run/tables/k.csv").read_text(encoding="utf-8").split("\n")[1:-1]
    # keys: b_index, a_index (column order), then the first *_offset_s column; stable otherwise
    assert [r.split(",")[0] for r in rows] == ["3", "2", "1", "4", "0"]


def test_offset_columns_need_a_start_epoch(events: Table, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="start_utc"):
        _write(Results({}, {"events": events}), tmp_path / "run")
    assert not (tmp_path / "run").exists()
    with pytest.raises(ValueError, match="start_utc"):
        _write(Results({"time": {"start_utc": "not a date"}}, {"events": events}), tmp_path / "r2")
    with pytest.raises(ValueError, match="UTC"):
        _write(
            Results({"time": {"start_utc": "2026-01-01T00:00:00"}}, {"events": events}),
            tmp_path / "r3",
        )


def test_existing_utc_column_is_not_duplicated(tmp_path: Path) -> None:
    columns = (Column("t_offset_s", "f64", "s", ""), Column("t_utc", "str", "", ""))
    table = Table("t", columns, {"t_offset_s": np.array([1.0]), "t_utc": ("given",)})
    results = Results({"time": {"start_utc": "2026-01-01T00:00:00Z"}}, {"t": table})
    _write(results, tmp_path / "run")
    assert (tmp_path / "run/tables/t.csv").read_text(encoding="utf-8") == (
        "t_offset_s,t_utc\n1.0,given\n"
    )


def test_load_results_round_trip_olc_and_csv(sample_results: Results, tmp_path: Path) -> None:
    _write(sample_results, tmp_path / "both")
    loaded = load_results(tmp_path / "both")
    assert loaded.metadata == sample_results.metadata
    assert set(loaded.tables) == {"all_dtypes", "events"}
    assert loaded.tables["all_dtypes"] == sample_results.tables["all_dtypes"]
    # written rows are key-sorted, which is what every format stores
    expected_events = loaded.tables["events"]
    assert expected_events.data["t_offset_s"].tolist()[:1] == [90.25]

    _write(sample_results, tmp_path / "csv_only", formats=("csv",))
    from_csv = load_results(tmp_path / "csv_only")
    assert from_csv.tables == loaded.tables
    # a loaded Results can be written again, identically
    _write(loaded, tmp_path / "again")
    assert _tree(tmp_path / "again") == _tree(tmp_path / "both")


def test_load_results_prefers_olc(sample_results: Results, tmp_path: Path) -> None:
    run = tmp_path / "run"
    _write(sample_results, run)
    # corrupt the CSV consistently (and its hashes); load must still return OLC data
    csv = run / "tables/events.csv"
    csv.write_text("garbage\n", encoding="utf-8")
    meta = json.loads((run / "results.json").read_text(encoding="utf-8"))
    meta["tables"]["events"]["files"]["csv"]["sha256"] = hashlib.sha256(
        csv.read_bytes()
    ).hexdigest()
    (run / "results.json").write_text(json.dumps(meta), encoding="utf-8")
    write_manifest(run)
    assert load_results(run).tables["events"].rows == 4


def test_load_results_rejects_inconsistent_olc(sample_results: Results, tmp_path: Path) -> None:
    run = tmp_path / "run"
    _write(sample_results, run)
    meta_path = run / "results.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["tables"]["events"]["files"]["olc"]["sha256"] = "0" * 64
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    write_manifest(run)
    with pytest.raises(ValueError, match="sha256"):
        load_results(run)


def test_load_results_rejects_bad_results_json(sample_results: Results, tmp_path: Path) -> None:
    for mutate, match in (
        (lambda m: m.pop("tables"), "tables"),
        (lambda m: m["tables"]["events"]["files"]["csv"].update(path="../evil.csv"), "path"),
        (lambda m: m["tables"]["events"].update(rows=99), "rows"),
        (lambda m: m["tables"]["events"]["columns"][0].update(dtype="f64"), "columns"),
        (lambda m: m["tables"]["events"]["files"].clear(), "files"),
    ):
        run = tmp_path / f"run{len(list(tmp_path.iterdir()))}"
        _write(sample_results, run)
        meta = json.loads((run / "results.json").read_text(encoding="utf-8"))
        mutate(meta)
        (run / "results.json").write_text(json.dumps(meta), encoding="utf-8")
        write_manifest(run)
        with pytest.raises(ValueError, match=match):
            load_results(run)


def test_load_results_rejects_bad_csv(sample_results: Results, tmp_path: Path) -> None:
    run = tmp_path / "run"
    _write(sample_results, run, formats=("csv",))
    original = (run / "tables/events.csv").read_text(encoding="utf-8")
    for text, match in (
        (original.replace("station_index", "station"), "header"),
        (original + "1,2\n", "fields"),
        (original.replace("culmination", "culmination,extra", 1), "fields"),
        (original.replace("0,1,90.25", "x,1,90.25", 1), "i32"),
        (original.replace(",true\n", ",maybe\n", 1), "boolean"),
        ("", "header"),
    ):
        (run / "tables/events.csv").write_text(text, encoding="utf-8")
        meta_path = run / "results.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["tables"]["events"]["files"]["csv"]["sha256"] = hashlib.sha256(
            text.encode()
        ).hexdigest()
        meta_path.write_text(json.dumps(meta), encoding="utf-8")
        write_manifest(run)
        with pytest.raises(ValueError, match=match):
            load_results(run)


def test_csv_round_trips_strings_with_every_awkward_character(tmp_path: Path) -> None:
    text = ("a,b", 'say "hi"', "line\nbreak", "cr\r\nlf", " lead", "", "日本", "﻿bom")
    table = Table("s", (Column("v", "str", "", ""),), {"v": text})
    _write(Results({}, {"s": table}), tmp_path / "run", formats=("csv",))
    assert load_results(tmp_path / "run").tables["s"].data["v"] == text


def test_csv_float32_and_special_values_round_trip(tmp_path: Path) -> None:
    values = np.array([0.1, -0.0, np.inf, -np.inf, np.nan, 3.4e38, 1e-45], dtype=np.float32)
    columns = (Column("f", "f32", "", ""), Column("d", "f64", "", ""))
    table = Table("f", columns, {"f": values, "d": values.astype(np.float64)})
    _write(Results({}, {"f": table}), tmp_path / "run", formats=("csv",))
    loaded = load_results(tmp_path / "run").tables["f"]
    assert loaded == table
    assert (tmp_path / "run/tables/f.csv").read_text(encoding="utf-8").startswith("f,d\n0.1,")


def test_csv_rejects_non_boolean_flag_values(tmp_path: Path) -> None:
    table = Table("b", (Column("flag", "u8", "bool", ""),), {"flag": np.array([2], np.uint8)})
    with pytest.raises(ValueError, match="boolean"):
        _write(Results({}, {"b": table}), tmp_path / "run")


def test_empty_table_round_trips_in_both_formats(tmp_path: Path) -> None:
    columns = (Column("i", "i32", "1", ""), Column("s", "str", "", ""))
    table = Table("none", columns, {"i": np.zeros(0, np.int32), "s": ()})
    _write(Results({}, {"none": table}), tmp_path / "run")
    assert (tmp_path / "run/tables/none.csv").read_text(encoding="utf-8") == "i,s\n"
    assert load_results(tmp_path / "run").tables["none"] == table
    _write(Results({}, {"none": table}), tmp_path / "csv", formats=("csv",))
    assert load_results(tmp_path / "csv").tables["none"] == table


def test_input_files_are_copied(sample_results: Results, tmp_path: Path) -> None:
    scenario = tmp_path / "scenario.json"
    scenario.write_bytes(b'{"a": 1}\n')
    catalog = tmp_path / "elements.csv"
    catalog.write_bytes(b"x,y\n")
    run = tmp_path / "run"
    _write(
        sample_results,
        run,
        inputs=[(scenario, "scenario.json"), (catalog, "catalogs/elements.csv")],
    )
    assert (run / "inputs/scenario.json").read_bytes() == b'{"a": 1}\n'
    assert (run / "inputs/catalogs/elements.csv").read_bytes() == b"x,y\n"
    manifest = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
    assert "inputs/catalogs/elements.csv" in manifest["files"]
    assert "manifest.json" not in manifest["files"]
    verify_manifest(run)


@pytest.mark.parametrize(
    "name",
    ["/abs.csv", "../x.csv", "a/../b.csv", "a/b/c.csv", "", ".", "a//b", "a\\b", "x/", "./x"],
)
def test_bad_archive_names_are_rejected(sample_results: Results, tmp_path: Path, name: str) -> None:
    source = tmp_path / "s.txt"
    source.write_bytes(b"x")
    with pytest.raises(ValueError, match="archive name"):
        _write(sample_results, tmp_path / "run", inputs=[(source, name)])
    assert not (tmp_path / "run").exists()


def test_input_file_problems_are_rejected(sample_results: Results, tmp_path: Path) -> None:
    source = tmp_path / "s.txt"
    source.write_bytes(b"x")
    with pytest.raises(ValueError, match="duplicate"):
        _write(sample_results, tmp_path / "r1", inputs=[(source, "a"), (source, "a")])
    with pytest.raises(ValueError, match="not a file"):
        _write(sample_results, tmp_path / "r2", inputs=[(tmp_path / "missing", "a")])


def test_write_results_argument_validation(sample_results: Results, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="format"):
        _write(sample_results, tmp_path / "r1", formats=("csv", "xml"))
    with pytest.raises(ValueError, match="tables"):
        _write(Results({"tables": {}}, {}), tmp_path / "r2")
    with pytest.raises(ValueError, match="JSON"):
        _write(Results({"bad": float("nan")}, {}), tmp_path / "r3")
    with pytest.raises(ValueError, match="JSON"):
        _write(Results({"bad": object()}, {}), tmp_path / "r4")
    with pytest.raises(ValueError, match="JSON"):
        _write(Results({1: 2, "a": 3}, {}), tmp_path / "r5")  # type: ignore[dict-item]
    assert list(tmp_path.iterdir()) == []


def test_csv_is_always_written(sample_results: Results, tmp_path: Path) -> None:
    _write(sample_results, tmp_path / "run", formats=("olc",))
    assert (tmp_path / "run/tables/events.csv").is_file()
    assert (tmp_path / "run/tables/events.olc").is_file()


def test_manifest_detects_tamper(sample_results: Results, tmp_path: Path) -> None:
    run = tmp_path / "run"
    _write(sample_results, run)
    verify_manifest(run)
    assert set(build_manifest(run)["files"]) == set(_tree(run)) - {"manifest.json"}

    target = run / "tables/events.csv"
    original = target.read_bytes()
    target.write_bytes(original + b" ")
    with pytest.raises(ValueError, match="tables/events.csv"):
        verify_manifest(run)
    with pytest.raises(ValueError, match="tables/events.csv"):
        load_results(run)
    target.write_bytes(original)
    verify_manifest(run)

    extra = run / "inputs" / "extra.txt"
    extra.parent.mkdir()
    extra.write_bytes(b"x")
    with pytest.raises(ValueError, match="unlisted.*inputs/extra.txt"):
        verify_manifest(run)
    extra.unlink()
    extra.parent.rmdir()

    target.unlink()
    with pytest.raises(ValueError, match="missing.*tables/events.csv"):
        verify_manifest(run)
    target.write_bytes(original)

    manifest = run / "manifest.json"
    manifest.unlink()
    with pytest.raises(ValueError, match="manifest.json"):
        verify_manifest(run)
    manifest.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="manifest"):
        verify_manifest(run)
    manifest.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="manifest"):
        verify_manifest(run)
    manifest.write_text(
        json.dumps({"kind": "openleo.manifest", "schema_version": "2", "files": {}}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="manifest"):
        verify_manifest(run)


def test_manifest_rejects_symlinks(sample_results: Results, tmp_path: Path) -> None:
    run = tmp_path / "run"
    _write(sample_results, run)
    (run / "link.txt").symlink_to(run / "results.json")
    with pytest.raises(ValueError, match="symlink"):
        build_manifest(run)


def test_manifest_digest_is_the_hash_of_manifest_json(
    sample_results: Results, tmp_path: Path
) -> None:
    run = tmp_path / "run"
    _write(sample_results, run)
    assert manifest_digest(run) == hashlib.sha256((run / "manifest.json").read_bytes()).hexdigest()
    manifest = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
    assert (
        manifest["files"]["results.json"]
        == hashlib.sha256((run / "results.json").read_bytes()).hexdigest()
    )
    assert "manifest_sha256" not in json.loads((run / "results.json").read_text(encoding="utf-8"))
    with pytest.raises(ValueError, match="manifest.json"):
        manifest_digest(tmp_path)


def test_atomic_publish_leaves_no_partial_on_failure(tmp_path: Path) -> None:
    target = tmp_path / "run"
    seen: list[Path] = []

    def build(directory: Path) -> None:
        seen.append(directory)
        (directory / "half.txt").write_text("partial")
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        publish_atomically(target, build)
    assert not target.exists()
    assert not seen[0].exists()
    assert list(tmp_path.iterdir()) == []
    assert seen[0].parent == tmp_path
    assert seen[0].name.startswith("run.tmp-")


def test_atomic_publish_renames_into_place(tmp_path: Path) -> None:
    target = tmp_path / "deep" / "run"

    def build(directory: Path) -> None:
        (directory / "f.txt").write_text("x")

    publish_atomically(target, build)
    assert (target / "f.txt").read_text() == "x"
    assert [p.name for p in (tmp_path / "deep").iterdir()] == ["run"]


def test_existing_target_refused(tmp_path: Path) -> None:
    target = tmp_path / "run"
    target.mkdir()
    (target / "keep.txt").write_text("keep")
    called = []
    with pytest.raises(FileExistsError):
        publish_atomically(target, lambda d: called.append(d))
    assert not called
    assert (target / "keep.txt").read_text() == "keep"
    assert [p.name for p in tmp_path.iterdir()] == ["run"]


def test_target_created_during_build_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "run"

    def build(directory: Path) -> None:
        target.mkdir()
        (target / "other.txt").write_text("racer")

    with pytest.raises(FileExistsError):
        publish_atomically(target, build)
    assert (target / "other.txt").read_text() == "racer"
    assert [p.name for p in tmp_path.iterdir()] == ["run"]


def test_write_results_refuses_existing_directory(sample_results: Results, tmp_path: Path) -> None:
    (tmp_path / "run").mkdir()
    with pytest.raises(FileExistsError):
        _write(sample_results, tmp_path / "run")


def test_parquet_round_trip(sample_results: Results, tmp_path: Path) -> None:
    pytest.importorskip("pyarrow")
    from openleo.results.io import read_parquet

    run = tmp_path / "run"
    _write(sample_results, run, formats=("csv", "olc", "parquet"))
    loaded = load_results(run)
    for name, table in loaded.tables.items():
        assert read_parquet(run / "tables" / f"{name}.parquet") == table
    meta = json.loads((run / "results.json").read_text(encoding="utf-8"))
    assert meta["tables"]["events"]["files"]["parquet"]["path"] == "tables/events.parquet"
    verify_manifest(run)
    # NaN stays NaN (not null) and empty tables survive
    empty = Table(
        "none",
        (Column("x", "f64", "m", "d"), Column("s", "str", "", "")),
        {"x": np.zeros(0), "s": ()},
    )
    _write(Results({}, {"none": empty}), tmp_path / "e", formats=("parquet",))
    assert read_parquet(tmp_path / "e/tables/none.parquet") == empty
    again = tmp_path / "again"
    _write(sample_results, again, formats=("csv", "olc", "parquet"))
    assert (again / "tables/events.parquet").read_bytes() == (
        run / "tables/events.parquet"
    ).read_bytes()


def test_parquet_requires_the_optional_extra(
    sample_results: Results, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "pyarrow", None)
    monkeypatch.setitem(sys.modules, "pyarrow.parquet", None)
    with pytest.raises(ValueError, match=r"openleo\[parquet\]"):
        _write(sample_results, tmp_path / "run", formats=("csv", "parquet"))
    assert not (tmp_path / "run").exists()
    from openleo.results.io import read_parquet

    with pytest.raises(ValueError, match=r"openleo\[parquet\]"):
        read_parquet(tmp_path / "x.parquet")


def test_start_time_is_read_from_the_normalised_scenario(events: Table, tmp_path: Path) -> None:
    results = Results(
        {"scenario": {"time": {"start_utc": "2026-09-10T12:00:00Z"}}}, {"events": events}
    )
    _write(results, tmp_path / "run")
    lines = (tmp_path / "run/tables/events.csv").read_text(encoding="utf-8").split("\n")
    assert lines[1].split(",")[3] == "2026-09-10T12:01:30.250000Z"
    # scenario.time wins over a top-level time
    both = Results(
        {
            "scenario": {"time": {"start_utc": "2026-09-10T12:00:00Z"}},
            "time": {"start_utc": "2000-01-01T00:00:00Z"},
        },
        {"events": events},
    )
    _write(both, tmp_path / "both")
    assert (tmp_path / "both/tables/events.csv").read_bytes() == (
        tmp_path / "run/tables/events.csv"
    ).read_bytes()


def test_huge_csv_field_round_trips(tmp_path: Path) -> None:
    import csv

    text = ("x" * 200_000, "y")
    table = Table("big", (Column("v", "str", "", ""),), {"v": text})
    _write(Results({}, {"big": table}), tmp_path / "run", formats=("csv",))
    assert load_results(tmp_path / "run").tables["big"].data["v"] == text
    assert csv.field_size_limit() == 131072  # the global limit is restored


def test_csv_parse_errors_are_value_errors(
    sample_results: Results, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import csv

    from openleo.results import io as results_io

    _write(sample_results, tmp_path / "run", formats=("csv",))

    def broken(*args: object, **kwargs: object) -> None:
        raise csv.Error("field larger than field limit (131072)")

    monkeypatch.setattr(results_io.csv, "reader", broken)
    with pytest.raises(ValueError, match="cannot be parsed"):
        load_results(tmp_path / "run")
