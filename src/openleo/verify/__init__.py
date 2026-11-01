"""Verification against published references: orbit states now, ITU-R workbooks in S3."""

from openleo.verify.itu_workbook import verify_itu_workbook
from openleo.verify.orbit import verify_orbit_reference

__all__ = ["verify_itu_workbook", "verify_orbit_reference"]
