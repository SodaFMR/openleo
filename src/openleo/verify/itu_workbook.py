"""Verification against the ITU-R validation workbooks: owned by stage S3, not available yet."""

from __future__ import annotations

from pathlib import Path
from typing import NoReturn

MESSAGE = "available in S3"


def verify_itu_workbook(path: str | Path) -> NoReturn:
    """Always raises ``NotImplementedError``: workbook verification ships in S3."""
    raise NotImplementedError(f"ITU-R workbook verification of {Path(path).name} is {MESSAGE}")
