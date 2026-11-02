"""Fixtures for the v2 app server tests (helpers live in ``app_client``)."""

import json

import pytest
from app_client import CATALOG, Client, scenario_raw, start, stop


@pytest.fixture
def scenario_path(tmp_path):
    directory = tmp_path / "scenario"
    directory.mkdir()
    (directory / "orbit.csv").write_bytes(CATALOG.read_bytes())
    path = directory / "scenario.json"
    path.write_text(json.dumps(scenario_raw()), encoding="utf-8")
    return path


@pytest.fixture
def scenario(scenario_path):
    from openleo.scenario import load_scenario

    return load_scenario(scenario_path)


@pytest.fixture
def running_app(scenario):
    from openleo.app import create_server

    server = create_server(scenario)
    thread = start(server)
    try:
        yield server
    finally:
        stop(server, thread)


@pytest.fixture
def client(running_app):
    return Client(running_app)
