"""Results v2: the columnar output every consumer reads (design spec section 7)."""

from openleo.results.io import (
    load_results,
    publish_atomically,
    read_parquet,
    write_results,
)
from openleo.results.manifest import (
    build_manifest,
    manifest_digest,
    verify_manifest,
    write_manifest,
)
from openleo.results.model import Column, Results, Table
from openleo.results.olc import decode_table, encode_table

__all__ = [
    "Column",
    "Results",
    "Table",
    "build_manifest",
    "decode_table",
    "encode_table",
    "load_results",
    "manifest_digest",
    "publish_atomically",
    "read_parquet",
    "verify_manifest",
    "write_manifest",
    "write_results",
]
