"""Column, Table and Results validation."""

import numpy as np
import pytest

from openleo.results.model import DTYPES, Column, Results, Table


def _col(name: str = "x", dtype: str = "f64", unit: str = "m") -> Column:
    return Column(name, dtype, unit, "d")  # type: ignore[arg-type]


def test_dtype_set_is_exact() -> None:
    assert set(DTYPES) == {"f64", "f32", "i64", "i32", "u8", "str"}


def test_float64_array_declared_i64_is_an_error() -> None:
    with pytest.raises(ValueError, match="x.*i64"):
        Table("t", (_col(dtype="i64"),), {"x": np.array([1.0, 2.0])})


@pytest.mark.parametrize(
    ("dtype", "array"),
    [
        ("f64", np.array([1.0], dtype=np.float32)),
        ("f32", np.array([1.0])),
        ("i32", np.array([1], dtype=np.int64)),
        ("u8", np.array([True])),
        ("i64", np.array([1], dtype=np.uint64)),
    ],
)
def test_wrong_numpy_dtype_is_rejected(dtype: str, array: np.ndarray) -> None:
    with pytest.raises(ValueError, match="dtype"):
        Table("t", (_col(dtype=dtype),), {"x": array})


def test_unknown_dtype_is_rejected() -> None:
    with pytest.raises(ValueError, match="dtype"):
        Column("x", "f16", "", "")  # type: ignore[arg-type]


def test_unequal_lengths_are_rejected() -> None:
    columns = (_col("a"), _col("b"))
    with pytest.raises(ValueError, match="length"):
        Table("t", columns, {"a": np.zeros(3), "b": np.zeros(2)})


def test_data_must_match_columns() -> None:
    with pytest.raises(ValueError, match="missing"):
        Table("t", (_col("a"), _col("b")), {"a": np.zeros(1)})
    with pytest.raises(ValueError, match="unexpected"):
        Table("t", (_col("a"),), {"a": np.zeros(1), "z": np.zeros(1)})


def test_names_and_shapes_are_validated() -> None:
    with pytest.raises(ValueError, match="table name"):
        Table("Bad Name", (_col(),), {"x": np.zeros(1)})
    with pytest.raises(ValueError, match="duplicate"):
        Table("t", (_col("a"), _col("a")), {"a": np.zeros(1)})
    with pytest.raises(ValueError, match="at least one column"):
        Table("t", (), {})
    with pytest.raises(ValueError, match="one-dimensional"):
        Table("t", (_col(),), {"x": np.zeros((2, 2))})
    with pytest.raises(ValueError, match="numpy array"):
        Table("t", (_col(),), {"x": [1.0]})  # type: ignore[dict-item]
    with pytest.raises(ValueError, match="strings"):
        Table("t", (_col(dtype="str"),), {"x": ("a", 1)})  # type: ignore[dict-item]
    with pytest.raises(ValueError, match="strings"):
        Table("t", (_col(dtype="str"),), {"x": np.zeros(2)})
    with pytest.raises(ValueError, match="column name"):
        Column("", "f64", "", "")
    with pytest.raises(ValueError, match="unit"):
        Column("x", "f64", 3, "")  # type: ignore[arg-type]


def test_table_copies_and_freezes_data() -> None:
    source = np.array([1.0, 2.0])
    table = Table("t", (_col(),), {"x": source})
    source[0] = 99.0
    assert table.data["x"][0] == 1.0
    assert not table.data["x"].flags.writeable
    assert table.rows == 2
    assert table["x"] is table.data["x"]


def test_big_endian_input_is_normalised() -> None:
    table = Table("t", (_col(),), {"x": np.array([1.5], dtype=">f8")})
    assert table.data["x"].dtype == np.dtype(np.float64)
    assert table.data["x"][0] == 1.5


def test_table_equality_is_bitwise_and_nan_safe() -> None:
    a = Table("t", (_col(),), {"x": np.array([np.nan, 1.0])})
    b = Table("t", (_col(),), {"x": np.array([np.nan, 1.0])})
    c = Table("t", (_col(),), {"x": np.array([np.nan, 2.0])})
    d = Table("t", (_col(unit="s"),), {"x": np.array([np.nan, 1.0])})
    assert a == b
    assert a != c
    assert a != d
    assert a != "not a table"


def test_results_keys_must_match_table_names() -> None:
    table = Table("t", (_col(),), {"x": np.zeros(1)})
    assert Results({}, {"t": table}).tables["t"] is table
    with pytest.raises(ValueError, match="key"):
        Results({}, {"other": table})
