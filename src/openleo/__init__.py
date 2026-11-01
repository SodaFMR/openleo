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
    "ConstellationScenario": "openleo.constellation",
    "GasesBenchmark": "openleo.calc.gases",
    "GasesCase": "openleo.calc.gases",
    "GasesResult": "openleo.calc.gases",
    "GroundStation": "openleo.model",
    "LoadedOrbit": "openleo.orbit",
    "Modcod": "openleo.physics.adaptation",
    "NetworkConfig": "openleo.network",
    "OrbitSource": "openleo.model",
    "Provenance": "openleo.model",
    "RadioLink": "openleo.model",
    "Results": "openleo.results",
    "Scenario": "openleo.model",
    "SensitivityBaselineContext": "openleo.sensitivity",
    "SensitivityCase": "openleo.sensitivity",
    "SensitivityMetrics": "openleo.sensitivity",
    "SensitivityResult": "openleo.sensitivity",
    "SensitivityStudy": "openleo.sensitivity",
    "SensitivitySweep": "openleo.sensitivity",
    "SimulationResult": "openleo.simulation",
    "SimulationSummary": "openleo.simulation",
    "SpecificGaseousAttenuation": "openleo.physics.gases",
    "TimeWindow": "openleo.model",
    "TraceRow": "openleo.simulation",
    "add_network": "openleo.network",
    "load_constellation": "openleo.constellation",
    "load_experiment": "openleo.workbench",
    "load_gases_benchmark": "openleo.calc.gases",
    "load_orbit": "openleo.orbit",
    "load_results": "openleo.results",
    "load_scenario": "openleo.scenario",
    "load_sensitivity_study": "openleo.sensitivity",
    "main": "openleo.cli",
    "parse_constellation": "openleo.constellation",
    "render_workbench": "openleo.workbench",
    "run": "openleo.engine.run",
    "run_gases_benchmark": "openleo.calc.gases",
    "run_sensitivity": "openleo.sensitivity",
    "save_run": "openleo.engine.run",
    "scenario_document": "openleo.constellation",
    "select_modcod": "openleo.physics.adaptation",
    "simulate_constellation": "openleo.constellation",
    "simulate_scenario": "openleo.simulation",
    "specific_gaseous_attenuation": "openleo.physics.gases",
    "write_gases_result": "openleo.calc.gases",
    "write_result": "openleo.output",
    "write_sensitivity_result": "openleo.sensitivity",
    "write_workbench": "openleo.workbench",
}

__all__ = [
    "AdaptationConfig",
    "ConstellationScenario",
    "GasesBenchmark",
    "GasesCase",
    "GasesResult",
    "GroundStation",
    "LoadedOrbit",
    "Modcod",
    "NetworkConfig",
    "OrbitSource",
    "Provenance",
    "RadioLink",
    "Results",
    "Scenario",
    "SensitivityBaselineContext",
    "SensitivityCase",
    "SensitivityMetrics",
    "SensitivityResult",
    "SensitivityStudy",
    "SensitivitySweep",
    "SimulationResult",
    "SimulationSummary",
    "SpecificGaseousAttenuation",
    "TimeWindow",
    "TraceRow",
    "__version__",
    "add_network",
    "load_constellation",
    "load_experiment",
    "load_gases_benchmark",
    "load_orbit",
    "load_results",
    "load_scenario",
    "load_sensitivity_study",
    "main",
    "parse_constellation",
    "render_workbench",
    "run",
    "run_gases_benchmark",
    "run_sensitivity",
    "save_run",
    "scenario_document",
    "select_modcod",
    "simulate_constellation",
    "simulate_scenario",
    "specific_gaseous_attenuation",
    "write_gases_result",
    "write_result",
    "write_sensitivity_result",
    "write_workbench",
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
