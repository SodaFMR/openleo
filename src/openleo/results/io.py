"""Writing and loading results directories: CSV, Parquet, OLC, inputs and atomic publication.

Layout (spec 7.1): ``results.json``, ``tables/<name>.{csv,olc,parquet}``, ``inputs/...`` and
``manifest.json``. CSV is always written; OLC and Parquet are selected by ``formats``.

CSV conventions
---------------
* UTF-8, LF line endings, header row first, standard CSV quoting.
* ``f64`` cells are ``repr(float)`` (shortest round trip); ``f32`` cells are the shortest text
  that round-trips the float32; NaN is an empty cell; infinities are ``inf``/``-inf``.
* ``u8`` columns whose unit is ``bool`` are written ``true``/``false`` (values must be 0 or 1).
* After every ``*_offset_s`` column an ISO 8601 ``*_utc`` text column is inserted (the name
  replaces the ``_offset_s`` suffix with ``_utc``): ``metadata["time"]["start_utc"]`` plus
  the offset, rounded to the microsecond, ending in ``Z`` (``utc_text`` of the engine). It is
  empty when the offset is NaN or infinite. A table that already has a column of that name
  keeps its own and gets no derived one. OLC and Parquet store only the offsets.
* Row order: every format stores the table's rows ordered by its key columns, which are the
  columns whose names end in ``_index`` (in column order) followed by the first column whose
  name ends in ``_offset_s``; the sort is stable and NaN sorts last. A table without such
  columns keeps the order it was given.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import secrets
import shutil
import threading
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from math import isfinite, isnan
from pathlib import Path
from typing import Any

import numpy as np

from openleo.results.manifest import (
    MANIFEST_NAME,
    read_manifest,
    sha256_file,
    verify_manifest,
    write_manifest,
)
from openleo.results.model import BOOL_UNIT, NUMPY_DTYPES, Column, Results, Table
from openleo.results.olc import decode_table, encode_table

FORMATS = ("csv", "olc", "parquet")
_PARQUET_HINT = "Parquet needs the optional pyarrow dependency: install it with 'openleo[parquet]'"
_TABLE_NAME = re.compile(r"[a-z][a-z0-9_]*\Z")
_OFFSET_SUFFIX = "_offset_s"


# --------------------------------------------------------------------- atomic publication


def publish_atomically(target: Path, build: Callable[[Path], None]) -> None:
    """Build into ``<target>.tmp-<random>`` next to the target, then rename it into place.

    Raises ``FileExistsError`` if ``target`` exists (before building and again before the
    rename). If ``build`` or the rename fails, the temporary directory is removed and the
    target is left absent.
    """
    target = Path(target)
    if os.path.lexists(target):
        raise FileExistsError(f"refusing to overwrite existing results at {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f"{target.name}.tmp-{secrets.token_hex(8)}")
    temporary.mkdir()
    try:
        build(temporary)
        if os.path.lexists(target):
            raise FileExistsError(f"refusing to overwrite existing results at {target}")
        try:
            os.rename(temporary, target)
        except OSError as exc:
            if os.path.lexists(target):
                raise FileExistsError(
                    f"refusing to overwrite existing results at {target}"
                ) from exc
            raise
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


# --------------------------------------------------------------------------- row ordering


def key_columns(columns: Sequence[Column]) -> list[str]:
    """Names of the numeric key columns that order a table's rows (see module docstring)."""
    numeric = [c.name for c in columns if c.dtype != "str"]
    keys = [n for n in numeric if n.endswith("_index")]
    offsets = [n for n in numeric if n.endswith(_OFFSET_SUFFIX)]
    return keys + offsets[:1]


def sort_by_keys(table: Table) -> Table:
    """The table with rows stably ordered by its key columns."""
    keys = key_columns(table.columns)
    if not keys or table.rows < 2:
        return table
    order = np.lexsort(tuple(table.data[name] for name in reversed(keys)))
    data: dict[str, Any] = {}
    for column in table.columns:
        values = table.data[column.name]
        if column.dtype == "str":
            data[column.name] = tuple(values[i] for i in order.tolist())
        else:
            data[column.name] = values[order]
    return Table(table.name, table.columns, data)


# ------------------------------------------------------------------------------------ CSV


def _csv_layout(columns: Sequence[Column]) -> list[tuple[Column, str | None]]:
    """CSV columns in order: (source column, derived UTC name or None) per emitted cell group."""
    names = {c.name for c in columns}
    layout: list[tuple[Column, str | None]] = []
    for column in columns:
        layout.append((column, None))
        if column.dtype != "str" and column.name.endswith(_OFFSET_SUFFIX):
            derived = column.name[: -len(_OFFSET_SUFFIX)] + "_utc"
            if derived not in names:
                layout.append((column, derived))
    return layout


def _start_utc(metadata: dict, table: str) -> datetime:
    """``scenario.time.start_utc`` (results.json), else ``time.start_utc``, as a UTC datetime."""
    start = None
    for path in (("scenario", "time"), ("time",)):
        node: Any = metadata
        for key in path:
            node = node.get(key) if isinstance(node, dict) else None
        if isinstance(node, dict) and "start_utc" in node:
            start = node["start_utc"]
            break
    message = (
        f"metadata scenario.time.start_utc (or time.start_utc) must be an ISO 8601 UTC "
        f"timestamp (needed for the *_utc columns of table {table!r})"
    )
    if not isinstance(start, str):
        raise ValueError(message)  # noqa: TRY004
    try:
        parsed = datetime.fromisoformat(start)
    except ValueError as exc:
        raise ValueError(message) from exc
    if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        raise ValueError(message)
    return parsed.astimezone(UTC)


def _cells(column: Column, values: Any) -> list[str]:
    if column.dtype == "f64":
        return ["" if isnan(v) else repr(v) for v in values.tolist()]
    if column.dtype == "f32":
        return ["" if isnan(v) else str(v) for v in values]
    if column.dtype == "u8" and column.unit == BOOL_UNIT:
        if values.size and values.max() > 1:
            raise ValueError(f"column {column.name!r} is a boolean flag but holds values above 1")
        return ["true" if v else "false" for v in values.tolist()]
    if column.dtype == "str":
        return list(values)
    return [str(v) for v in values.tolist()]


def _utc_cells(start: datetime, offsets: Any, column: str) -> list[str]:
    from openleo.engine.timegrid import utc_text

    cells = []
    for offset in offsets.astype(np.float64).tolist():
        if not isfinite(offset):
            cells.append("")
            continue
        try:
            cells.append(utc_text(start, offset))
        except OverflowError as exc:
            raise ValueError(
                f"column {column!r}: offset {offset} is outside the UTC range"
            ) from exc
    return cells


def csv_bytes(table: Table, metadata: dict) -> bytes:
    """The table as CSV bytes under the conventions in the module docstring."""
    layout = _csv_layout(table.columns)
    start = _start_utc(metadata, table.name) if any(d for _, d in layout) else None
    header: list[str] = []
    cells: list[list[str]] = []
    for column, derived in layout:
        values = table.data[column.name]
        if derived is None:
            header.append(column.name)
            cells.append(_cells(column, values))
        else:
            header.append(derived)
            cells.append(_utc_cells(start, values, column.name))  # type: ignore[arg-type]
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(zip(*cells, strict=True))
    return buffer.getvalue().encode("utf-8")


def _parse_ints(name: str, dtype: str, texts: list[str]) -> np.ndarray:
    target = NUMPY_DTYPES[dtype]
    info = np.iinfo(target)
    try:
        numbers = [int(t) for t in texts]
    except ValueError as exc:
        raise ValueError(f"CSV column {name!r}: invalid {dtype} value ({exc})") from exc
    if numbers and not (info.min <= min(numbers) and max(numbers) <= info.max):
        raise ValueError(f"CSV column {name!r}: {dtype} value out of range")
    return np.array(numbers, dtype=target)


def _parse_column(column: Column, texts: list[str]) -> Any:
    name = column.name
    if column.dtype == "str":
        return tuple(texts)
    if column.dtype in ("f64", "f32"):
        try:
            numbers = [float("nan") if t == "" else float(t) for t in texts]
        except ValueError as exc:
            raise ValueError(f"CSV column {name!r}: invalid {column.dtype} value ({exc})") from exc
        return np.array(numbers, dtype=NUMPY_DTYPES[column.dtype])
    if column.dtype == "u8" and column.unit == BOOL_UNIT:
        words = {"true": 1, "false": 0}
        if any(t not in words for t in texts):
            raise ValueError(f"CSV column {name!r}: boolean cells must be true or false")
        return np.array([words[t] for t in texts], dtype=np.uint8)
    return _parse_ints(name, column.dtype, texts)


_CSV_FIELD_LIMIT = 1 << 28  # characters per cell; Python's default of 131072 is too small
# ``csv.field_size_limit`` is process-global: one thread restoring it while another parses would
# make that parse fail (the app server loads results from worker and request threads). Raising,
# parsing and restoring therefore happen under one lock.
_CSV_LIMIT_LOCK = threading.Lock()


def _csv_rows(text: str, name: str) -> list[list[str]]:
    with _CSV_LIMIT_LOCK:
        previous = csv.field_size_limit(_CSV_FIELD_LIMIT)
        try:
            return list(csv.reader(io.StringIO(text, newline="")))
        except csv.Error as exc:
            raise ValueError(
                f"CSV table {name!r} cannot be parsed (a field may exceed {_CSV_FIELD_LIMIT} "
                f"characters): {exc}"
            ) from exc
        finally:
            csv.field_size_limit(previous)


def read_csv(data: bytes, name: str, columns: Sequence[Column]) -> Table:
    """Parse CSV written by ``csv_bytes`` back into a table with the given schema."""
    layout = _csv_layout(columns)
    expected = [d or c.name for c, d in layout]
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"CSV table {name!r} is not valid UTF-8: {exc}") from exc
    rows = _csv_rows(text, name)
    if not rows or rows[0] != expected:
        raise ValueError(f"CSV table {name!r}: header does not match the columns {expected}")
    body = rows[1:]
    for number, row in enumerate(body, start=2):
        if len(row) != len(expected):
            raise ValueError(
                f"CSV table {name!r} row {number}: {len(row)} fields, expected {len(expected)}"
            )
    values: dict[str, Any] = {}
    for index, (column, derived) in enumerate(layout):
        if derived is None:
            values[column.name] = _parse_column(column, [row[index] for row in body])
    return Table(name, tuple(columns), values)


# -------------------------------------------------------------------------------- Parquet


def _pyarrow():
    try:
        import pyarrow
        import pyarrow.parquet
    except ImportError as exc:
        raise ValueError(_PARQUET_HINT) from exc
    return pyarrow, pyarrow.parquet


_ARROW_TYPES = {
    "double": "f64",
    "float": "f32",
    "int64": "i64",
    "int32": "i32",
    "uint8": "u8",
    "string": "str",
}


def write_parquet(table: Table, path: Path) -> None:
    """Write one table as Parquet (zstd), keeping unit and description as field metadata."""
    pa, pq = _pyarrow()
    arrow_types = {
        "f64": pa.float64(),
        "f32": pa.float32(),
        "i64": pa.int64(),
        "i32": pa.int32(),
        "u8": pa.uint8(),
        "str": pa.string(),
    }
    fields, arrays = [], []
    for column in table.columns:
        arrow_type = arrow_types[column.dtype]
        meta = {"unit": column.unit, "description": column.description}
        fields.append(pa.field(column.name, arrow_type, nullable=False, metadata=meta))
        values = table.data[column.name]
        arrays.append(pa.array(list(values) if column.dtype == "str" else values, type=arrow_type))
    schema = pa.schema(fields, metadata={"openleo.table": table.name})
    pq.write_table(pa.Table.from_arrays(arrays, schema=schema), str(path), compression="zstd")


def read_parquet(path: Path) -> Table:
    """Read a table written by ``write_parquet``."""
    _, pq = _pyarrow()
    arrow = pq.read_table(str(path))
    schema_meta = arrow.schema.metadata or {}
    if b"openleo.table" not in schema_meta:
        raise ValueError(f"{path}: not an OpenLEO Parquet table (no openleo.table metadata)")
    columns, values = [], {}
    for index, field in enumerate(arrow.schema):
        dtype = _ARROW_TYPES.get(str(field.type))
        if dtype is None:
            raise ValueError(f"{path}: column {field.name!r} has unsupported type {field.type}")
        meta = field.metadata or {}
        columns.append(
            Column(
                field.name,
                dtype,  # type: ignore[arg-type]
                meta.get(b"unit", b"").decode("utf-8"),
                meta.get(b"description", b"").decode("utf-8"),
            )
        )
        array = arrow.column(index).combine_chunks()
        if array.null_count:
            raise ValueError(f"{path}: column {field.name!r} contains nulls")
        values[field.name] = (
            tuple(array.to_pylist())
            if dtype == "str"
            else np.asarray(array.to_numpy(zero_copy_only=False))
        )
    return Table(schema_meta[b"openleo.table"].decode("utf-8"), tuple(columns), values)


# ------------------------------------------------------------------------------- writing


def _formats(formats: Sequence[str]) -> tuple[str, ...]:
    unknown = [f for f in formats if f not in FORMATS]
    if unknown:
        raise ValueError(f"unknown output format {unknown}; choose from {list(FORMATS)}")
    return tuple(f for f in FORMATS if f == "csv" or f in formats)


def _archive_names(input_files: Sequence[tuple[Path, str]]) -> list[tuple[Path, str]]:
    checked: list[tuple[Path, str]] = []
    seen: set[str] = set()
    directories: set[str] = set()
    for source, name in input_files:
        parts = name.split("/") if isinstance(name, str) else []
        if (
            not parts
            or len(parts) > 2
            or any(p in ("", ".", "..") for p in parts)
            or any(c in name for c in "\\:\0")
        ):
            raise ValueError(
                f"invalid archive name {name!r}: use a relative name with at most one directory"
            )
        if name in seen:
            raise ValueError(f"duplicate archive name {name!r}")
        seen.add(name)
        if len(parts) == 2:
            directories.add(parts[0])
        source = Path(source)
        if not source.is_file():
            raise ValueError(f"input file {source} is not a file")
        checked.append((source, name))
    clash = directories & seen
    if clash:
        raise ValueError(f"archive name {min(clash)!r} is both a file and a directory")
    return checked


def _json_text(document: dict) -> bytes:
    try:
        text = json.dumps(document, sort_keys=True, ensure_ascii=False, indent=2, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"results metadata is not valid JSON content: {exc}") from exc
    return (text + "\n").encode("utf-8")


def _column_json(column: Column) -> dict[str, str]:
    return {
        "name": column.name,
        "dtype": column.dtype,
        "unit": column.unit,
        "description": column.description,
    }


def write_results(
    results: Results,
    directory: Path,
    formats: tuple[str, ...],
    input_files: Sequence[tuple[Path, str]] = (),
) -> Path:
    """Publish ``results`` atomically at ``directory`` and return that directory.

    ``results.metadata`` becomes ``results.json`` with a ``tables`` section added (it must not
    already have one). ``input_files`` are (source path, archive name) pairs copied to
    ``inputs/<archive name>``. The manifest is written last; see ``manifest_digest``.
    """
    wanted = _formats(formats)
    if "parquet" in wanted:
        _pyarrow()
    inputs = _archive_names(input_files)
    if "tables" in results.metadata:
        raise ValueError("results metadata must not contain a 'tables' key; it is generated")
    tables = {name: sort_by_keys(table) for name, table in sorted(results.tables.items())}
    directory = Path(directory)

    def build(root: Path) -> None:
        (root / "tables").mkdir()
        described: dict[str, Any] = {}
        for name, table in tables.items():
            files: dict[str, dict[str, str]] = {}

            def record(kind: str, payload: bytes, name: str = name, files: dict = files) -> None:
                relative = f"tables/{name}.{kind}"
                (root / relative).write_bytes(payload)
                files[kind] = {"path": relative, "sha256": hashlib.sha256(payload).hexdigest()}

            record("csv", csv_bytes(table, results.metadata))
            if "olc" in wanted:
                record("olc", encode_table(table))
            if "parquet" in wanted:
                relative = f"tables/{name}.parquet"
                write_parquet(table, root / relative)
                files["parquet"] = {"path": relative, "sha256": sha256_file(root / relative)}
            described[name] = {
                "rows": table.rows,
                "columns": [_column_json(c) for c in table.columns],
                "files": files,
            }
        (root / "results.json").write_bytes(_json_text({**results.metadata, "tables": described}))
        for source, archive_name in inputs:
            destination = root / "inputs" / archive_name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
        write_manifest(root)

    publish_atomically(directory, build)
    return directory


# ------------------------------------------------------------------------------- loading


def _columns_from_json(name: str, raw: object) -> tuple[Column, ...]:
    try:
        if not isinstance(raw, list) or not raw:
            raise ValueError("expected a non-empty list")
        return tuple(Column(c["name"], c["dtype"], c["unit"], c["description"]) for c in raw)
    except (TypeError, KeyError, ValueError) as exc:
        raise ValueError(f"results.json table {name!r}: invalid columns ({exc})") from exc


def load_results(directory: Path) -> Results:
    """Load a results directory after verifying its manifest and the recorded file hashes.

    Each table is read from its OLC file when one was written (the OLC reader re-checks every
    column hash), otherwise from CSV. ``Results.metadata`` is ``results.json`` without the
    generated ``tables`` section.
    """
    directory = Path(directory)
    verify_manifest(directory)
    manifest = read_manifest(directory)
    try:
        metadata = json.loads((directory / "results.json").read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"results.json not found in {directory}") from exc
    except ValueError as exc:
        raise ValueError(f"results.json is not valid JSON: {exc}") from exc
    if not isinstance(metadata, dict) or not isinstance(metadata.get("tables"), dict):
        raise ValueError("results.json must be an object with a tables section")  # noqa: TRY004
    described = metadata.pop("tables")
    tables: dict[str, Table] = {}
    for name, entry in described.items():
        if not _TABLE_NAME.match(name) or not isinstance(entry, dict):
            raise ValueError(f"results.json has an invalid table entry {name!r}")
        columns = _columns_from_json(name, entry.get("columns"))
        files = entry.get("files")
        if not isinstance(files, dict) or not ({"csv", "olc"} & files.keys()):
            raise ValueError(f"results.json table {name!r}: files must list csv or olc")
        for kind, info in files.items():
            expected = f"tables/{name}.{kind}"
            if kind not in FORMATS or not isinstance(info, dict) or info.get("path") != expected:
                raise ValueError(
                    f"results.json table {name!r}: bad {kind} path, expected {expected}"
                )
            if manifest.get(expected) != info.get("sha256"):
                raise ValueError(f"results.json table {name!r}: sha256 mismatch for {expected}")
        if "olc" in files:
            table = decode_table((directory / files["olc"]["path"]).read_bytes())
            if table.name != name or table.columns != columns:
                raise ValueError(f"table {name!r}: OLC columns differ from results.json columns")
        else:
            table = read_csv((directory / files["csv"]["path"]).read_bytes(), name, columns)
        if table.rows != entry.get("rows"):
            raise ValueError(
                f"table {name!r}: rows {table.rows} differ from results.json rows {entry.get('rows')}"
            )
        tables[name] = table
    return Results(metadata, tables)


__all__ = [
    "FORMATS",
    "MANIFEST_NAME",
    "csv_bytes",
    "key_columns",
    "load_results",
    "publish_atomically",
    "read_csv",
    "read_parquet",
    "sort_by_keys",
    "write_parquet",
    "write_results",
]
