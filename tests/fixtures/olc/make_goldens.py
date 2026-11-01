"""Regenerate the OLC v1 golden files and goldens.json next to this script.

Usage: ``python tests/fixtures/olc/make_goldens.py [output-dir]``

The files are consumed by the Python codec tests and, later, by the TypeScript reader (S8).
``goldens.json`` lists the expected decoded content of every file. Value encoding in the JSON:

* ``f64``/``f32``: JSON numbers; ``f32`` values are the exact float32 widened to a double.
  Non-finite values are the strings ``"NaN"``, ``"Infinity"`` and ``"-Infinity"``; negative
  zero is the number ``-0.0``.
* ``i64``: decimal strings (they exceed 2**53); ``i32`` and ``u8``: JSON numbers;
  ``str``: JSON strings.

This directory is excluded from pytest collection (tests/conftest.py ignores ``fixtures``).
"""

from __future__ import annotations

import hashlib
import json
import math
import sys
from pathlib import Path

import numpy as np

from openleo.results.model import Column, Table
from openleo.results.olc import encode_table

SPECIAL = np.array([np.nan, np.inf, -np.inf, -0.0, 5e-324, 1.7976931348623157e308])
SPECIAL_F32 = np.array([np.nan, np.inf, -np.inf, -0.0, 1e-45, 3.4028234663852886e38], np.float32)


def build_goldens() -> dict[str, Table]:
    """The golden tables, keyed by file name."""
    all_dtypes = Table(
        "all_dtypes",
        (
            Column("f64", "f64", "m", "float64 column"),
            Column("f32", "f32", "dB", "float32 column"),
            Column("i64", "i64", "1", "int64 column beyond 2**53"),
            Column("i32", "i32", "1", "int32 column"),
            Column("u8", "u8", "bool", "uint8 flag column"),
            Column("str", "str", "", "text column with control characters"),
        ),
        {
            "f64": np.array([0.1, -2.5, 1e300]),
            "f32": np.array([0.1, -2.5, 3e38], dtype=np.float32),
            "i64": np.array([-(2**63), 2**53 + 1, 2**63 - 1], dtype=np.int64),
            "i32": np.array([-(2**31), 0, 2**31 - 1], dtype=np.int32),
            "u8": np.array([0, 1, 255], dtype=np.uint8),
            "str": ("", "ascii", "tab\tnew\nline"),
        },
    )
    empty = Table(
        "empty",
        (
            Column("a", "f64", "s", "empty float64"),
            Column("b", "f32", "s", "empty float32"),
            Column("c", "i64", "1", "empty int64"),
            Column("d", "i32", "1", "empty int32"),
            Column("e", "u8", "1", "empty uint8"),
            Column("f", "str", "", "empty text"),
        ),
        {
            "a": np.zeros(0),
            "b": np.zeros(0, dtype=np.float32),
            "c": np.zeros(0, dtype=np.int64),
            "d": np.zeros(0, dtype=np.int32),
            "e": np.zeros(0, dtype=np.uint8),
            "f": (),
        },
    )
    unicode_table = Table(
        "unicode",
        (
            Column("index", "i32", "1", "row number"),
            Column("text", "str", "", "UTF-8 text: BMP, CJK, astral, NUL, empty"),
        ),
        {
            "index": np.arange(5, dtype=np.int32),
            "text": ("café", "日本語", "\U0001f6f0️ orbit", "a\u0000b", ""),
        },
    )
    special = Table(
        "special_floats",
        (
            Column("f64", "f64", "x", "NaN, +/-inf, -0, subnormal, max"),
            Column("f32", "f32", "x", "NaN, +/-inf, -0, subnormal, max"),
        ),
        {"f64": SPECIAL, "f32": SPECIAL_F32},
    )
    return {
        "all_dtypes.olc": all_dtypes,
        "empty.olc": empty,
        "unicode.olc": unicode_table,
        "special_floats.olc": special,
    }


def _json_value(dtype: str, value: object) -> object:
    if dtype in ("f64", "f32"):
        number = float(value)  # type: ignore[arg-type]
        if math.isnan(number):
            return "NaN"
        if math.isinf(number):
            return "Infinity" if number > 0 else "-Infinity"
        return number
    if dtype == "i64":
        return str(int(value))  # type: ignore[call-overload]
    if dtype in ("i32", "u8"):
        return int(value)  # type: ignore[call-overload]
    return value


def expected_json(files: dict[str, bytes], tables: dict[str, Table]) -> dict:
    entries = {}
    for name, table in tables.items():
        entries[name] = {
            "sha256": hashlib.sha256(files[name]).hexdigest(),
            "table": table.name,
            "rows": table.rows,
            "columns": [
                {"name": c.name, "dtype": c.dtype, "unit": c.unit, "description": c.description}
                for c in table.columns
            ],
            "values": {
                c.name: [_json_value(c.dtype, v) for v in table.data[c.name]] for c in table.columns
            },
        }
    return {"format": "OLC1", "files": entries}


def main(output: Path | None = None) -> None:
    output = Path(output) if output is not None else Path(__file__).parent
    output.mkdir(parents=True, exist_ok=True)
    tables = build_goldens()
    files = {name: encode_table(table) for name, table in tables.items()}
    for name, payload in files.items():
        (output / name).write_bytes(payload)
    text = json.dumps(expected_json(files, tables), indent=2, sort_keys=True, ensure_ascii=False)
    (output / "goldens.json").write_bytes((text + "\n").encode("utf-8"))


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else None)
