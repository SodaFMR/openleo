"""Shared builders for the results v2 tests."""

import numpy as np
import pytest

from openleo.results.model import Column, Results, Table

START_UTC = "2026-09-10T12:00:00Z"


def make_all_dtypes() -> Table:
    columns = (
        Column("a_f64", "f64", "m", "float64 column"),
        Column("b_f32", "f32", "dB", "float32 column"),
        Column("c_i64", "i64", "1", "int64 column"),
        Column("d_i32", "i32", "1", "int32 column"),
        Column("e_u8", "u8", "bool", "flag column"),
        Column("f_str", "str", "", "text column"),
    )
    data = {
        "a_f64": np.array([0.1, -2.5, 1e300, np.nan]),
        "b_f32": np.array([0.1, -2.5, 3.0e38, np.inf], dtype=np.float32),
        "c_i64": np.array([-(2**63), 0, 2**53 + 1, 2**63 - 1], dtype=np.int64),
        "d_i32": np.array([-(2**31), -1, 0, 2**31 - 1], dtype=np.int32),
        "e_u8": np.array([0, 1, 1, 0], dtype=np.uint8),
        "f_str": ("", "ascii", "café 日本語 \U0001f6f0", 'line\nbreak,"quote"'),
    }
    return Table("all_dtypes", columns, data)


def make_events() -> Table:
    columns = (
        Column("station_index", "i32", "1", "station"),
        Column("satellite_index", "i32", "1", "satellite"),
        Column("t_offset_s", "f64", "s", "event time"),
        Column("kind", "str", "", "event type"),
        Column("rate_bps", "f64", "bit/s", "rate"),
        Column("truncated", "u8", "bool", "truncated flag"),
    )
    data = {
        "station_index": np.array([1, 0, 0, 0], dtype=np.int32),
        "satellite_index": np.array([0, 2, 1, 1], dtype=np.int32),
        "t_offset_s": np.array([5.5, 120.0, 90.25, np.nan]),
        "kind": ("aos", "los", "culmination", "aos"),
        "rate_bps": np.array([1.0e6, np.nan, 2.5e6, 0.0]),
        "truncated": np.array([0, 1, 0, 1], dtype=np.uint8),
    }
    return Table("events", columns, data)


@pytest.fixture
def all_dtypes() -> Table:
    return make_all_dtypes()


@pytest.fixture
def events() -> Table:
    return make_events()


@pytest.fixture
def sample_results(all_dtypes: Table, events: Table) -> Results:
    metadata = {
        "kind": "openleo.results",
        "schema_version": "2",
        "time": {"start_utc": START_UTC},
        "note": "café",
    }
    return Results(metadata, {"all_dtypes": all_dtypes, "events": events})
