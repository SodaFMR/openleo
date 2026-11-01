"""The columnar results model: columns, tables and the results container (spec section 7)."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal

import numpy as np

if TYPE_CHECKING:
    from numpy.typing import NDArray

DType = Literal["f64", "f32", "i64", "i32", "u8", "str"]

#: Column dtypes, in the order the spec lists them.
DTYPES: tuple[str, ...] = ("f64", "f32", "i64", "i32", "u8", "str")

#: Native-endian numpy dtype of each numeric column dtype.
NUMPY_DTYPES: dict[str, np.dtype] = {
    "f64": np.dtype(np.float64),
    "f32": np.dtype(np.float32),
    "i64": np.dtype(np.int64),
    "i32": np.dtype(np.int32),
    "u8": np.dtype(np.uint8),
}

#: ``u8`` columns carrying this unit are booleans (``true``/``false`` in CSV).
BOOL_UNIT = "bool"

_TABLE_NAME = re.compile(r"[a-z][a-z0-9_]*\Z")


@dataclass(frozen=True)
class Column:
    """One column: name, dtype, unit (``""`` when dimensionless text) and description."""

    name: str
    dtype: DType
    unit: str
    description: str

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("column name must be a non-empty string")
        if self.dtype not in DTYPES:
            raise ValueError(f"column {self.name!r}: dtype must be one of {', '.join(DTYPES)}")
        if not isinstance(self.unit, str) or not isinstance(self.description, str):
            raise ValueError(f"column {self.name!r}: unit and description must be strings")  # noqa: TRY004


@dataclass(frozen=True, eq=False)
class Table:
    """A named table: equal-length columns, numeric ones as numpy arrays, text as tuples.

    The constructor validates names, lengths and dtypes, then stores read-only little-endian
    copies. A float64 array declared ``i64`` is an error, never a silent cast.
    """

    name: str
    columns: tuple[Column, ...]
    data: Mapping[str, NDArray | tuple[str, ...]] = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not _TABLE_NAME.match(self.name):
            raise ValueError(f"table name {self.name!r} must match [a-z][a-z0-9_]*")
        columns = tuple(self.columns)
        if not columns:
            raise ValueError(f"table {self.name!r} needs at least one column")
        names = [column.name for column in columns]
        if len(set(names)) != len(names):
            raise ValueError(f"table {self.name!r} has duplicate column names")
        missing = [n for n in names if n not in self.data]
        if missing:
            raise ValueError(f"table {self.name!r}: data missing for columns {missing}")
        unexpected = [n for n in self.data if n not in names]
        if unexpected:
            raise ValueError(f"table {self.name!r}: unexpected data columns {unexpected}")
        stored = {c.name: _normalise(self.name, c, self.data[c.name]) for c in columns}
        lengths = {len(values) for values in stored.values()}
        if len(lengths) != 1:
            detail = {n: len(v) for n, v in stored.items()}
            raise ValueError(f"table {self.name!r}: columns have unequal length {detail}")
        object.__setattr__(self, "columns", columns)
        object.__setattr__(self, "data", MappingProxyType(stored))

    @property
    def rows(self) -> int:
        return len(self.data[self.columns[0].name])

    def __getitem__(self, name: str) -> NDArray | tuple[str, ...]:
        return self.data[name]

    def __eq__(self, other: object) -> bool:
        """Equal names, columns and values; floats compare bitwise, so NaN equals NaN."""
        if not isinstance(other, Table):
            return NotImplemented
        if self.name != other.name or self.columns != other.columns:
            return False
        for column in self.columns:
            mine, theirs = self.data[column.name], other.data[column.name]
            if column.dtype == "str":
                if mine != theirs:
                    return False
            elif mine.tobytes() != theirs.tobytes():
                return False
        return True

    __hash__ = None  # type: ignore[assignment]


def _normalise(table: str, column: Column, values: object) -> NDArray | tuple[str, ...]:
    where = f"table {table!r} column {column.name!r}"
    if column.dtype == "str":
        if isinstance(values, np.ndarray):
            if values.ndim != 1 or values.dtype.kind != "U":
                raise ValueError(f"{where}: expected a one-dimensional sequence of strings")
            return tuple(str(v) for v in values)
        if isinstance(values, str) or not hasattr(values, "__iter__"):
            raise ValueError(f"{where}: expected a sequence of strings")
        text = tuple(values)  # type: ignore[call-overload]
        if not all(isinstance(v, str) for v in text):
            raise ValueError(f"{where}: expected a sequence of strings")
        return text
    if not isinstance(values, np.ndarray):
        raise ValueError(f"{where}: expected a numpy array")  # noqa: TRY004
    if values.ndim != 1:
        raise ValueError(f"{where}: expected a one-dimensional array")
    expected = NUMPY_DTYPES[column.dtype]
    if values.dtype.kind != expected.kind or values.dtype.itemsize != expected.itemsize:
        raise ValueError(
            f"{where}: array dtype {values.dtype} does not match declared {column.dtype}"
        )
    stored = np.array(values, dtype=expected, order="C", copy=True)
    stored.setflags(write=False)
    return stored


@dataclass(frozen=True)
class Results:
    """Run metadata (the ``results.json`` content) and the tables, keyed by table name."""

    metadata: dict
    tables: Mapping[str, Table]

    def __post_init__(self) -> None:
        tables = dict(self.tables)
        for key, table in tables.items():
            if key != table.name:
                raise ValueError(f"table key {key!r} does not match table name {table.name!r}")
        object.__setattr__(self, "tables", tables)
