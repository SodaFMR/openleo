"""OpenLEO link budget tools.

The package exports resolve lazily (PEP 562), so ``import openleo`` does not import numpy or
skyfield: the console script sets its BLAS thread limits before they load
(``openleo._launcher``).
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

__version__ = "2.0.0.dev0"

_EXPORTS = {
    "AdaptationConfig": "openleo.physics.adaptation",
    "GasesBenchmark": "openleo.calc.gases",
    "GasesCase": "openleo.calc.gases",
    "GasesResult": "openleo.calc.gases",
    "LoadedOrbit": "openleo.engine.catalogs",
    "Modcod": "openleo.physics.adaptation",
    "OrbitSource": "openleo.engine.catalogs",
    "Provenance": "openleo.engine.catalogs",
    "Results": "openleo.results",
    "Scenario": "openleo.scenario",
    "SpecificGaseousAttenuation": "openleo.physics.gases",
    "load_gases_benchmark": "openleo.calc.gases",
    "load_results": "openleo.results",
    "load_scenario": "openleo.scenario",
    "main": "openleo.cli",
    "run": "openleo.engine.run",
    "run_gases_benchmark": "openleo.calc.gases",
    "run_sweep": "openleo.sweep",
    "save_run": "openleo.engine.run",
    "select_modcod": "openleo.physics.adaptation",
    "specific_gaseous_attenuation": "openleo.physics.gases",
    "write_gases_result": "openleo.calc.gases",
}

__all__ = [
    "AdaptationConfig",
    "GasesBenchmark",
    "GasesCase",
    "GasesResult",
    "LoadedOrbit",
    "Modcod",
    "OrbitSource",
    "Provenance",
    "Results",
    "Scenario",
    "SpecificGaseousAttenuation",
    "__version__",
    "load_gases_benchmark",
    "load_results",
    "load_scenario",
    "main",
    "run",
    "run_gases_benchmark",
    "run_sweep",
    "save_run",
    "select_modcod",
    "specific_gaseous_attenuation",
    "write_gases_result",
]


def __getattr__(name: str) -> Any:
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_EXPORTS))
