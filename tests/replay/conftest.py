"""Saved v2 runs shared by the replay tests (built once per session)."""

import gzip
import json
from pathlib import Path

import pytest

from openleo.engine.run import run, save_run
from openleo.scenario import load_scenario, parse_scenario, scenario_document

EXAMPLES = Path("examples/constellations")
HYDROMETEORS = EXAMPLES / "iridium_global_hydrometeors.json"
GLOBAL = EXAMPLES / "iridium_global.json"
V1_HYDROMETEORS = Path("tests/fixtures/v1/iridium_global_hydrometeors.json.gz")


def save(path: Path, scenario_path: Path, directory: Path, **time) -> Path:
    """Run ``scenario_path`` with its ``time`` block updated by ``time`` and save it."""
    document = scenario_document(load_scenario(scenario_path))
    events = time.pop("events", None)
    document["time"].update(time)
    if events is not None:
        document["time"]["events"]["enabled"] = events
    document["time"]["convergence"]["mode"] = "off"
    scenario = parse_scenario(document, scenario_path, "0" * 64)
    return save_run(scenario, run(scenario), directory / path)


@pytest.fixture(scope="session")
def hydrometeors_off(tmp_path_factory) -> Path:
    """The v1 hydrometeor example with events off: v1's sampled semantics, whole window."""
    return save(Path("run"), HYDROMETEORS, tmp_path_factory.mktemp("off"), events=False)


@pytest.fixture(scope="session")
def global_on(tmp_path_factory) -> Path:
    """The free-space global example with events on, over its first ten minutes."""
    return save(
        Path("run"),
        GLOBAL,
        tmp_path_factory.mktemp("on"),
        events=True,
        stop_utc="2026-09-10T12:10:00Z",
    )


@pytest.fixture(scope="session")
def v1_hydrometeors() -> dict:
    with gzip.open(V1_HYDROMETEORS, "rt", encoding="utf-8") as handle:
        return json.load(handle)
