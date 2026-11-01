"""OLC v1: the binary columnar table format (spec section 7.4).

Byte layout, in order:

1. magic ``OLC1``;
2. uint32 little-endian header length ``H``;
3. ``H`` bytes of UTF-8 JSON (sorted keys, compact):
   ``{"table", "rows", "columns": [{"name", "dtype", "unit", "description", "offset",
   "length", "sha256"}]}``; ``offset`` is absolute from the first byte of the file,
   ``length`` counts payload bytes (no padding), ``sha256`` is the hex digest of the payload;
   ``description`` is optional on read (it is the only key beyond the spec's list);
4. zero padding up to the next multiple of 8 bytes;
5. one payload per column, in header order, each starting on an 8-byte boundary (zero padding
   between payloads, none after the last). Numeric payloads are little-endian arrays of
   ``rows`` values. A ``str`` payload is ``rows + 1`` little-endian uint32 byte offsets
   (starting at 0) followed by the concatenated UTF-8 bytes.

The file ends exactly at the end of the last payload. The reader checks the magic, every
bound, the alignment, the padding, the length and the SHA-256 of every column.
"""

from __future__ import annotations

import hashlib
import json
import struct
from typing import Any

import numpy as np

from openleo.results.model import DTYPES, NUMPY_DTYPES, Column, Table

MAGIC = b"OLC1"
_ALIGN = 8
_STRING_OFFSET = np.dtype("<u4")


def _align(value: int) -> int:
    return -(-value // _ALIGN) * _ALIGN


def _payload(column: Column, values: object) -> bytes:
    if column.dtype == "str":
        try:
            encoded = [text.encode("utf-8") for text in values]  # type: ignore[attr-defined]
        except UnicodeEncodeError as exc:
            raise ValueError(f"column {column.name!r}: string is not valid UTF-8: {exc}") from exc
        sizes = np.fromiter((len(e) for e in encoded), dtype=np.uint64, count=len(encoded))
        offsets = np.zeros(len(encoded) + 1, dtype=np.uint64)
        np.cumsum(sizes, out=offsets[1:])
        if offsets[-1] > 0xFFFFFFFF:
            raise ValueError(f"column {column.name!r}: string data exceeds the 4 GiB offset limit")
        return offsets.astype(_STRING_OFFSET).tobytes() + b"".join(encoded)
    return values.astype(NUMPY_DTYPES[column.dtype].newbyteorder("<"), copy=False).tobytes()  # type: ignore[attr-defined]


def _layout(table: Table, payloads: list[bytes]) -> tuple[bytes, list[int]]:
    """Header JSON and absolute column offsets; offsets and header length depend on each other."""
    digests = [hashlib.sha256(p).hexdigest() for p in payloads]
    header_length = 0
    while True:
        cursor = _align(8 + header_length)
        offsets = []
        for payload in payloads:
            cursor = _align(cursor)
            offsets.append(cursor)
            cursor += len(payload)
        header = {
            "table": table.name,
            "rows": table.rows,
            "columns": [
                {
                    "name": c.name,
                    "dtype": c.dtype,
                    "unit": c.unit,
                    "description": c.description,
                    "offset": offset,
                    "length": len(payload),
                    "sha256": digest,
                }
                for c, offset, payload, digest in zip(
                    table.columns, offsets, payloads, digests, strict=True
                )
            ],
        }
        text = json.dumps(header, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        encoded = text.encode("utf-8")
        if len(encoded) == header_length:
            return encoded, offsets
        header_length = len(encoded)


def encode_table(table: Table) -> bytes:
    """Serialise a table to OLC v1 bytes (deterministic)."""
    payloads = [_payload(column, table.data[column.name]) for column in table.columns]
    header, offsets = _layout(table, payloads)
    out = bytearray(MAGIC)
    out += struct.pack("<I", len(header))
    out += header
    for offset, payload in zip(offsets, payloads, strict=True):
        out += b"\x00" * (offset - len(out))
        out += payload
    return bytes(out)


def decode_table(data: bytes) -> Table:
    """Parse and fully validate OLC v1 bytes; raises ValueError on any inconsistency."""
    if len(data) < 8:
        raise ValueError("OLC data is truncated: shorter than the 8-byte preamble")
    if data[:4] != MAGIC:
        raise ValueError(f"OLC bad magic {bytes(data[:4])!r}, expected {MAGIC!r}")
    (header_length,) = struct.unpack_from("<I", data, 4)
    header_end = 8 + header_length
    if header_end > len(data):
        raise ValueError("OLC header length exceeds the data (truncated or corrupt header)")
    try:
        header = json.loads(data[8:header_end].decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError(f"OLC header is not valid UTF-8 JSON: {exc}") from exc
    if not isinstance(header, dict) or not isinstance(header.get("columns"), list):
        raise ValueError("OLC header must be an object with a columns list")  # noqa: TRY004
    name, rows = header.get("table"), header.get("rows")
    if not isinstance(name, str) or not _is_int(rows) or rows < 0:
        raise ValueError("OLC header needs a string table name and a non-negative integer rows")

    data_start = _align(header_end)
    if len(data) < data_start:
        raise ValueError("OLC data is truncated inside the header padding")
    if any(data[header_end:data_start]):
        raise ValueError("OLC header padding must be zero")

    columns: list[Column] = []
    values: dict[str, Any] = {}
    cursor = data_start
    for entry in header["columns"]:
        column, payload_start, payload_end = _column_entry(entry, len(data), cursor)
        if any(data[cursor:payload_start]):
            raise ValueError(f"OLC padding before column {column.name!r} must be zero")
        payload = memoryview(data)[payload_start:payload_end]
        if hashlib.sha256(payload).hexdigest() != entry["sha256"]:
            raise ValueError(f"OLC column {column.name!r}: sha256 mismatch")
        values[column.name] = _read_values(column, bytes(payload), rows)
        columns.append(column)
        cursor = payload_end
    if cursor != len(data):
        raise ValueError(f"OLC has {len(data) - cursor} trailing bytes after the last column")
    return Table(name, tuple(columns), values)


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _column_entry(entry: object, size: int, cursor: int) -> tuple[Column, int, int]:
    if not isinstance(entry, dict):
        raise ValueError("OLC column entries must be objects")  # noqa: TRY004
    name, dtype, unit = entry.get("name"), entry.get("dtype"), entry.get("unit")
    offset, length, digest = entry.get("offset"), entry.get("length"), entry.get("sha256")
    description = entry.get("description", "")
    if not isinstance(name, str) or not name:
        raise ValueError("OLC column needs a non-empty string name")
    if dtype not in DTYPES:
        raise ValueError(f"OLC column {name!r}: unknown dtype {dtype!r}")
    if not isinstance(unit, str) or not isinstance(description, str):
        raise ValueError(f"OLC column {name!r}: unit and description must be strings")  # noqa: TRY004
    if not _is_int(offset) or not _is_int(length) or offset < 0 or length < 0:
        raise ValueError(f"OLC column {name!r}: offset and length must be non-negative integers")
    if not isinstance(digest, str):
        raise ValueError(f"OLC column {name!r}: sha256 must be a hex string")  # noqa: TRY004
    if offset % _ALIGN:
        raise ValueError(f"OLC column {name!r}: offset {offset} is not 8-byte aligned")
    if offset < cursor:
        raise ValueError(f"OLC column {name!r}: payload overlaps the preceding data")
    if offset + length > size:
        raise ValueError(f"OLC column {name!r}: payload exceeds the data (truncated)")
    return Column(name, dtype, unit, description), offset, offset + length


def _read_values(column: Column, payload: bytes, rows: int) -> Any:
    if column.dtype != "str":
        dtype = NUMPY_DTYPES[column.dtype]
        if len(payload) != rows * dtype.itemsize:
            raise ValueError(
                f"OLC column {column.name!r}: length {len(payload)} != "
                f"{rows} rows x {dtype.itemsize} bytes"
            )
        return np.frombuffer(payload, dtype=dtype.newbyteorder("<")).astype(dtype)
    head = 4 * (rows + 1)
    if len(payload) < head:
        raise ValueError(f"OLC column {column.name!r}: length too small for {rows + 1} offsets")
    offsets = np.frombuffer(payload, dtype=_STRING_OFFSET, count=rows + 1).tolist()
    body = payload[head:]
    if offsets[0] != 0 or offsets[-1] != len(body) or offsets != sorted(offsets):
        raise ValueError(f"OLC column {column.name!r}: invalid string offsets")
    try:
        return tuple(
            body[offsets[i] : offsets[i + 1]].decode("utf-8", errors="strict") for i in range(rows)
        )
    except UnicodeDecodeError as exc:
        raise ValueError(f"OLC column {column.name!r}: string is not valid UTF-8: {exc}") from exc
