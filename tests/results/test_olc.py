"""The OLC v1 binary columnar codec and its committed golden files."""

import hashlib
import importlib.util
import json
import struct
from pathlib import Path

import numpy as np
import pytest

from openleo.results.model import Column, Table
from openleo.results.olc import decode_table, encode_table

GOLDEN_DIR = Path(__file__).parent.parent / "fixtures" / "olc"


def _header(data: bytes) -> tuple[dict, int, int]:
    (length,) = struct.unpack_from("<I", data, 4)
    return json.loads(data[8 : 8 + length]), length, 8 + length


def _replace_in_header(data: bytes, old: str, new: str) -> bytes:
    """Replace same-length text inside the JSON header, keeping every payload in place."""
    assert len(old) == len(new)
    _, _, end = _header(data)
    header = data[8:end]
    assert old.encode() in header
    return data[:8] + header.replace(old.encode(), new.encode(), 1) + data[end:]


def _with_nonzero_padding() -> bytes:
    for suffix in "abcdefgh":
        table = Table("t" + suffix, (Column("x", "f64", "", ""),), {"x": np.zeros(1)})
        data = encode_table(table)
        _, _, header_end = _header(data)
        if header_end % 8:
            return data[:header_end] + b"\x01" + data[header_end + 1 :]
    raise AssertionError("no unaligned header found")


def _empty_of_every_dtype() -> Table:
    names = ("f64", "f32", "i64", "i32", "u8", "str")
    columns = tuple(Column(f"c_{n}", n, "u", "empty") for n in names)  # type: ignore[arg-type]
    data: dict = {
        "c_f64": np.zeros(0),
        "c_f32": np.zeros(0, dtype=np.float32),
        "c_i64": np.zeros(0, dtype=np.int64),
        "c_i32": np.zeros(0, dtype=np.int32),
        "c_u8": np.zeros(0, dtype=np.uint8),
        "c_str": (),
    }
    return Table("empty", columns, data)


def _special_floats() -> Table:
    columns = (Column("v", "f64", "x", ""), Column("w", "f32", "x", ""))
    values = np.array([np.nan, np.inf, -np.inf, -0.0, 5e-324, 1.7976931348623157e308])
    narrow = np.array([np.nan, np.inf, -np.inf, -0.0, 1e-45, 3.4e38], dtype=np.float32)
    return Table("special", columns, {"v": values, "w": narrow})


def test_olc_round_trip_all_dtypes(all_dtypes: Table) -> None:
    for table in (all_dtypes, _empty_of_every_dtype(), _special_floats()):
        decoded = decode_table(encode_table(table))
        assert decoded == table
        assert decoded.rows == table.rows
        assert decoded.columns == table.columns


def test_olc_round_trip_preserves_unicode_and_nan_bits(all_dtypes: Table) -> None:
    decoded = decode_table(encode_table(all_dtypes))
    assert decoded.data["f_str"][2] == "café 日本語 \U0001f6f0"
    assert np.isnan(decoded.data["a_f64"][3])
    special = decode_table(encode_table(_special_floats()))
    assert np.signbit(special.data["v"][3])
    assert special.data["v"][2] == -np.inf


def test_olc_layout_follows_the_spec(all_dtypes: Table) -> None:
    data = encode_table(all_dtypes)
    assert data[:4] == b"OLC1"
    header, _, header_end = _header(data)
    assert header["table"] == "all_dtypes"
    assert header["rows"] == 4
    names = [c["name"] for c in header["columns"]]
    assert names == [c.name for c in all_dtypes.columns]
    data_start = -(-header_end // 8) * 8
    assert data[header_end:data_start] == b"\x00" * (data_start - header_end)
    cursor = data_start
    sizes = {"f64": 8, "i64": 8, "f32": 4, "i32": 4, "u8": 1}
    for column in header["columns"]:
        assert set(column) >= {"name", "dtype", "unit", "offset", "length", "sha256"}
        assert column["offset"] % 8 == 0
        assert column["offset"] >= cursor
        assert data[cursor : column["offset"]] == b"\x00" * (column["offset"] - cursor)
        payload = data[column["offset"] : column["offset"] + column["length"]]
        assert hashlib.sha256(payload).hexdigest() == column["sha256"]
        if column["dtype"] != "str":
            assert column["length"] == 4 * sizes[column["dtype"]]
        cursor = column["offset"] + column["length"]
    assert cursor == len(data)
    # f64 payload is little-endian
    first = header["columns"][0]
    assert data[first["offset"] : first["offset"] + 8] == struct.pack("<d", 0.1)
    # strings: u32 offsets array (rows + 1 entries) then UTF-8
    text = header["columns"][5]
    offsets = struct.unpack_from("<5I", data, text["offset"])
    assert offsets[0] == 0
    assert text["length"] == 20 + offsets[-1]


def test_olc_encoding_is_deterministic(all_dtypes: Table) -> None:
    assert encode_table(all_dtypes) == encode_table(all_dtypes)


def test_olc_empty_table_has_zero_length_payloads() -> None:
    header, _, _ = _header(encode_table(_empty_of_every_dtype()))
    lengths = {c["dtype"]: c["length"] for c in header["columns"]}
    assert lengths == {"f64": 0, "f32": 0, "i64": 0, "i32": 0, "u8": 0, "str": 4}


def test_olc_rejects_bad_magic_truncation_misalignment_and_hash(all_dtypes: Table) -> None:
    data = encode_table(all_dtypes)
    with pytest.raises(ValueError, match="magic"):
        decode_table(b"XLC1" + data[4:])
    for cut in range(len(data)):
        with pytest.raises(ValueError):
            decode_table(data[:cut])
    header, _, _ = _header(data)
    offset = header["columns"][0]["offset"]
    with pytest.raises(ValueError, match="aligned"):
        decode_table(_replace_in_header(data, f'"offset":{offset},', f'"offset":{offset + 1},'))
    last = bytearray(data)
    last[header["columns"][1]["offset"]] ^= 0x01
    with pytest.raises(ValueError, match="sha256"):
        decode_table(bytes(last))


def test_olc_rejects_structural_corruption(all_dtypes: Table) -> None:
    data = encode_table(all_dtypes)
    _, length, header_end = _header(data)
    with pytest.raises(ValueError, match="trailing"):
        decode_table(data + b"\x00")
    with pytest.raises(ValueError, match="header"):
        decode_table(data[:4] + struct.pack("<I", len(data)) + data[8:])
    with pytest.raises(ValueError, match="padding"):
        decode_table(_with_nonzero_padding())
    with pytest.raises(ValueError, match="dtype"):
        decode_table(_replace_in_header(data, '"dtype":"f64"', '"dtype":"f65"'))
    with pytest.raises(ValueError, match="length|sha256"):
        # shrink a fixed-width column's declared length
        decode_table(_replace_in_header(data, '"length":32', '"length":24'))
    with pytest.raises(ValueError, match="JSON"):
        decode_table(data[:8] + b"{" * length + data[header_end:])
    with pytest.raises(ValueError, match="header"):
        body = b"[1]"
        decode_table(b"OLC1" + struct.pack("<I", 3) + body + b"\x00" * 5)


def test_olc_rejects_bad_string_offsets_and_utf8() -> None:
    table = Table("s", (Column("t", "str", "", ""),), {"t": ("ab", "cd")})
    data = encode_table(table)
    header, _, _ = _header(data)
    column = header["columns"][0]

    def with_payload(payload: bytes) -> bytes:
        assert len(payload) == column["length"]
        digest = hashlib.sha256(payload).hexdigest()
        patched = data[: column["offset"]] + payload
        return _replace_in_header(patched, column["sha256"], digest)

    good = data[column["offset"] : column["offset"] + column["length"]]
    assert good == struct.pack("<3I", 0, 2, 4) + b"abcd"
    with pytest.raises(ValueError, match="string offsets"):
        decode_table(with_payload(struct.pack("<3I", 0, 3, 2) + b"abcd"))
    with pytest.raises(ValueError, match="string offsets"):
        decode_table(with_payload(struct.pack("<3I", 1, 2, 4) + b"abcd"))
    with pytest.raises(ValueError, match="string offsets"):
        decode_table(with_payload(struct.pack("<3I", 0, 2, 3) + b"abcd"))
    with pytest.raises(ValueError, match="UTF-8"):
        decode_table(with_payload(struct.pack("<3I", 0, 2, 4) + b"a\xffcd"))


def test_olc_encode_rejects_unencodable_strings() -> None:
    table = Table("s", (Column("t", "str", "", ""),), {"t": ("\ud800",)})
    with pytest.raises(ValueError, match="UTF-8"):
        encode_table(table)


def _load_generator():
    spec = importlib.util.spec_from_file_location("make_goldens", GOLDEN_DIR / "make_goldens.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_olc_golden_files() -> None:
    expected = json.loads((GOLDEN_DIR / "goldens.json").read_text(encoding="utf-8"))
    assert sorted(expected["files"]) == [
        "all_dtypes.olc",
        "empty.olc",
        "special_floats.olc",
        "unicode.olc",
    ]

    all_dtypes = decode_table((GOLDEN_DIR / "all_dtypes.olc").read_bytes())
    assert all_dtypes.name == "all_dtypes"
    assert all_dtypes.rows == 3
    assert [(c.name, c.dtype, c.unit) for c in all_dtypes.columns] == [
        ("f64", "f64", "m"),
        ("f32", "f32", "dB"),
        ("i64", "i64", "1"),
        ("i32", "i32", "1"),
        ("u8", "u8", "bool"),
        ("str", "str", ""),
    ]
    np.testing.assert_array_equal(all_dtypes.data["f64"], [0.1, -2.5, 1e300])
    np.testing.assert_array_equal(all_dtypes.data["f32"], np.array([0.1, -2.5, 3e38], np.float32))
    assert all_dtypes.data["i64"].tolist() == [-(2**63), 9007199254740993, 2**63 - 1]
    assert all_dtypes.data["i32"].tolist() == [-(2**31), 0, 2**31 - 1]
    assert all_dtypes.data["u8"].tolist() == [0, 1, 255]
    assert all_dtypes.data["str"] == ("", "ascii", "tab\tnew\nline")

    empty = decode_table((GOLDEN_DIR / "empty.olc").read_bytes())
    assert (empty.name, empty.rows) == ("empty", 0)
    assert [c.dtype for c in empty.columns] == ["f64", "f32", "i64", "i32", "u8", "str"]
    assert empty.data["f"] == ()

    uni = decode_table((GOLDEN_DIR / "unicode.olc").read_bytes())
    assert uni.data["text"] == (
        "café",
        "日本語",
        "\U0001f6f0️ orbit",
        "a\u0000b",
        "",
    )

    special = decode_table((GOLDEN_DIR / "special_floats.olc").read_bytes())
    v = special.data["f64"]
    assert np.isnan(v[0]) and v[1] == np.inf and v[2] == -np.inf
    assert v[3] == 0.0 and np.signbit(v[3])
    assert v[4] == 5e-324 and v[5] == 1.7976931348623157e308
    w = special.data["f32"]
    assert np.isnan(w[0]) and w[1] == np.inf and w[2] == -np.inf and np.signbit(w[3])

    # goldens.json agrees with the files and with a fresh run of the generator
    for name, entry in expected["files"].items():
        raw = (GOLDEN_DIR / name).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == entry["sha256"]
        assert entry["rows"] == decode_table(raw).rows


def test_olc_goldens_are_reproducible(tmp_path: Path) -> None:
    generator = _load_generator()
    generator.main(tmp_path)
    for path in sorted(tmp_path.iterdir()):
        assert path.read_bytes() == (GOLDEN_DIR / path.name).read_bytes(), path.name
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(
        p.name for p in GOLDEN_DIR.iterdir() if p.suffix in {".olc", ".json"}
    )


def _raw(header: object) -> bytes:
    body = json.dumps(header).encode()
    padded = body + b" " * (504 - len(body))  # the data section starts at byte 512
    return b"OLC1" + struct.pack("<I", len(padded)) + padded


def _entry(**changes: object) -> dict:
    entry = {
        "name": "x",
        "dtype": "u8",
        "unit": "",
        "offset": 512,
        "length": 0,
        "sha256": hashlib.sha256(b"").hexdigest(),
    }
    return {**entry, **changes}


@pytest.mark.parametrize(
    ("header", "match"),
    [
        ({"table": "t", "rows": 0}, "columns"),
        ([], "object"),
        ({"table": 1, "rows": 0, "columns": []}, "table name"),
        ({"table": "t", "rows": -1, "columns": []}, "rows"),
        ({"table": "t", "rows": True, "columns": []}, "rows"),
        ({"table": "t", "rows": 0, "columns": [3]}, "objects"),
        ({"table": "t", "rows": 0, "columns": [_entry(name="")]}, "name"),
        ({"table": "t", "rows": 0, "columns": [_entry(unit=3)]}, "unit"),
        ({"table": "t", "rows": 0, "columns": [_entry(offset="16")]}, "offset"),
        ({"table": "t", "rows": 0, "columns": [_entry(length=-1)]}, "offset"),
        ({"table": "t", "rows": 0, "columns": [_entry(sha256=3)]}, "sha256"),
        ({"table": "t", "rows": 0, "columns": [_entry(offset=8)]}, "overlaps"),
        ({"table": "t", "rows": 0, "columns": [_entry(offset=512, length=8)]}, "exceeds"),
        ({"table": "t", "rows": 0, "columns": [_entry(), _entry()]}, "duplicate"),
        ({"table": "T", "rows": 0, "columns": [_entry()]}, "table name"),
    ],
)
def test_olc_rejects_malformed_header_fields(header: object, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        decode_table(_raw(header))


def test_olc_accepts_a_spec_exact_header_without_description() -> None:
    table = decode_table(_raw({"table": "t", "rows": 0, "columns": [_entry()]}))
    assert table.columns[0].description == ""
    assert table.rows == 0


def test_olc_deeply_nested_header_is_a_value_error() -> None:
    body = b"[" * 100_000 + b"]" * 100_000
    padded = body + b" " * (-(8 + len(body)) % 8)
    with pytest.raises(ValueError, match="JSON"):
        decode_table(b"OLC1" + struct.pack("<I", len(padded)) + padded)
