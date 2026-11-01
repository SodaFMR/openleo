"""Verification against the ITU-R validation workbooks: owned by stage S3, not available yet."""

from __future__ import annotations

from pathlib import Path
from typing import NoReturn

MESSAGE = "openleo verify itu-workbook is not available until S3"


def verify_itu_workbook(path: str | Path) -> NoReturn:
    """Always raises ``NotImplementedError`` with ``MESSAGE``: workbook verification ships in S3.

    ``path`` is accepted for the S3 interface and not read."""
    raise NotImplementedError(MESSAGE)
