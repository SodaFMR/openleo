"""v1 import path for frozen GP orbit loading, which moved to ``openleo.engine.catalogs``.

This module only re-exports the moved code, with the v1 limits, until the v1 modules are
removed.
"""

from openleo.engine.catalogs import REQUIRED_OMM_FIELDS, LoadedOrbit, load_orbit
from openleo.engine.catalogs import V1_MAX_CATALOG_BYTES as MAX_ORBIT_BYTES

__all__ = ["MAX_ORBIT_BYTES", "REQUIRED_OMM_FIELDS", "LoadedOrbit", "load_orbit"]
