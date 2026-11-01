"""v1 import path for GP catalog loading, which moved to ``openleo.engine.catalogs``.

This module only re-exports the moved code, with the v1 limits, until the v1 modules are
removed.
"""

from openleo.engine.catalogs import V1_MAX_SATELLITES as MAX_SATELLITES
from openleo.engine.catalogs import _epoch, load_catalog

__all__ = ["MAX_SATELLITES", "_epoch", "load_catalog"]
