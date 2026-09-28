import json
from hashlib import sha256
from pathlib import Path

import pytest

FIXTURE = Path("examples/validation/vallado_06251.json")


def _verify(path=FIXTURE):
    from openleo.orbit_verification import verify_orbit_reference

    return verify_orbit_reference(path)


def _changed(tmp_path, **changes):
    data = {**json.loads(FIXTURE.read_text()), **changes}
    path = tmp_path / "reference.json"
    path.write_text(json.dumps(data))
    return path


def test_published_teme_states_match_existing_catalog_adapter():
    result = _verify()
    assert result["schema_version"] == "1"
    assert result["kind"] == "openleo.orbit-verification"
    assert result["passed"] is True
    assert result["frame"] == "TEME"
    assert result["units"] == {"position": "km", "velocity": "km/s", "elapsed_time": "s"}
    assert result["fixture_sha256"] == sha256(FIXTURE.read_bytes()).hexdigest()
    assert result["source"]["archive_sha256"] == (
        "3642043b706c76be87cf012db3f22e04da6b80498d00f515e51879e0ffadc115"
    )
    assert result["source"]["tle_sha256"] == (
        "d246d1d9d768ace445a38a965713fa9ba52d80fd8a41a0502ff83d7acffe2881"
    )
    assert result["source"]["ephemeris_sha256"] == (
        "d806df44648b1009dfd0ba7d8ff5348638c35255df94945299ce7b29cb1718aa"
    )
    assert result["time_convention"]["origin"] == "loaded SGP4 epoch"
    assert abs(result["time_convention"]["omm_epoch_shift_s"]) < 2e-7
    assert result["adapter"] == "openleo.catalog.load_catalog -> Skyfield TEME"
    assert set(result["software"]) == {"openleo-link", "skyfield", "sgp4", "numpy"}
    assert len(result["epochs"]) == 5
    assert [row["elapsed_s"] for row in result["epochs"]] == [0, 7200, 14400, 21600, 28800]
    assert all(row["position_error_km"] < 1e-6 for row in result["epochs"])
    assert all(row["velocity_error_km_s"] < 1e-9 for row in result["epochs"])
    assert result["epochs"][0]["position_km"] == pytest.approx(
        [3988.31022699, 5498.96657235, 0.90055879], abs=1e-7
    )
    assert result["limitations"]
    assert _verify() == result
    json.dumps(result, allow_nan=False)


def test_changed_published_reference_fails_numerical_comparison(tmp_path):
    data = json.loads(FIXTURE.read_text())
    states = [{**row} for row in data["states"]]
    states[0] = {**states[0], "position_km": [3989.31022699, 5498.96657235, 0.90055879]}
    result = _verify(_changed(tmp_path, states=states))
    assert result["passed"] is False
    assert result["epochs"][0]["passed"] is False
    assert result["epochs"][0]["position_error_km"] == pytest.approx(1.0, abs=1e-7)


@pytest.mark.parametrize(
    "changes, message",
    [
        ({"frame": "GCRS"}, "TEME"),
        ({"schema_version": "2"}, "schema_version"),
        ({"tolerances": {"position_km": float("nan"), "velocity_km_s": 1e-9}}, "finite"),
        ({"tolerances": {"position_km": 1e-6, "velocity_km_s": -1}}, "positive"),
        ({"states": []}, "states"),
        (
            {
                "states": [
                    {
                        "elapsed_s": float("inf"),
                        "position_km": [0, 0, 0],
                        "velocity_km_s": [0, 0, 0],
                    }
                ]
            },
            "finite",
        ),
        (
            {
                "states": [
                    {
                        "elapsed_s": 0,
                        "position_km": [float("nan"), 0, 0],
                        "velocity_km_s": [0, 0, 0],
                    }
                ]
            },
            "finite",
        ),
        (
            {"states": [{"elapsed_s": 0, "position_km": [0, 0], "velocity_km_s": [0, 0, 0]}]},
            "three",
        ),
        ({"tle": ["not a TLE", "not a TLE"]}, "TLE"),
        ({"units": {"position": "m", "velocity": "km/s", "elapsed_time": "s"}}, "units"),
    ],
)
def test_invalid_reference_inputs_rejected(tmp_path, changes, message):
    with pytest.raises(ValueError, match=message):
        _verify(_changed(tmp_path, **changes))


def test_nonfinite_tle_rejected(tmp_path):
    from sgp4.io import fix_checksum

    tle = json.loads(FIXTURE.read_text())["tle"]
    bad = fix_checksum(tle[0][:33] + "       nan" + tle[0][43:])
    with pytest.raises(ValueError, match="TLE|finite"):
        _verify(_changed(tmp_path, tle=[bad, tle[1]]))


def test_reference_residual_overflow_rejected(tmp_path):
    states = [{"elapsed_s": 0, "position_km": [1.79e308] * 3, "velocity_km_s": [0, 0, 0]}]
    with pytest.raises(ValueError, match="finite"):
        _verify(_changed(tmp_path, states=states))


def test_source_hash_must_be_valid_sha256(tmp_path):
    source = {**json.loads(FIXTURE.read_text())["source"], "archive_sha256": "not a hash"}
    with pytest.raises(ValueError, match="SHA-256"):
        _verify(_changed(tmp_path, source=source))


def test_tle_requires_numeric_checksum(tmp_path):
    tle = json.loads(FIXTURE.read_text())["tle"]
    with pytest.raises(ValueError, match="TLE"):
        _verify(_changed(tmp_path, tle=[tle[0][:-1] + "x", tle[1]]))
