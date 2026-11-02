"""Re-export a results directory in another table format (Hypatia export is S6).

``export_results(results_dir, to, output_dir)`` loads the results (verifying the manifest and
every table hash) and publishes a new results directory with the same ``results.json``
metadata, the tables in CSV (``to="csv"``) or CSV plus Parquet (``to="parquet"``; CSV is always
written), the archived ``inputs/`` copied unchanged, and a fresh manifest.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from openleo.results import load_results, write_results
from openleo.results.manifest import read_manifest

EXPORT_FORMATS = ("csv", "parquet")


def export_results(results_dir: Path, to: Literal["csv", "parquet"], output_dir: Path) -> Path:
    """Publish ``results_dir`` as a new results directory at ``output_dir``; return it."""
    if to not in EXPORT_FORMATS:
        raise ValueError(f"export format {to!r} must be csv or parquet")
    results_dir = Path(results_dir)
    results = load_results(results_dir)
    inputs = [
        (results_dir / name, name.removeprefix("inputs/"))
        for name in sorted(read_manifest(results_dir))
        if name.startswith("inputs/")
    ]
    return write_results(results, Path(output_dir), (to,), inputs)


__all__ = ["EXPORT_FORMATS", "export_results"]
