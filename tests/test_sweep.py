"""Sweeps: patches, expansion, windows, comparisons, publication and the replay seam."""

import copy
import csv
import gzip
import json
import math
import os
from pathlib import Path

import pytest

from openleo import sweep
from openleo.results import load_results, verify_manifest
from openleo.scenario import load_scenario, scenario_document

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples" / "v2"
V1_FIXTURES = ROOT / "tests" / "fixtures" / "v1"
ISS = EXAMPLES / "iss_cartagena.json"
GLOBAL = EXAMPLES / "iridium_global.json"
HYDROMETEORS = EXAMPLES / "iridium_global_hydrometeors.json"
COMPARISON_LABEL = "deterministic comparison; not an uncertainty interval"


def _document(path: Path = ISS) -> dict:
    return scenario_document(load_scenario(path))


def _write(directory: Path, spec: dict, name: str = "sweep.json", base: Path = ISS) -> Path:
    document = {
        "schema": "openleo.sweep/2",
        "name": "test sweep",
        "base_scenario": os.path.relpath(base, directory),
        **spec,
    }
    path = directory / name
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def _eirp(directory: Path, **extra) -> Path:
    spec = {
        "mode": "oat",
        "parameters": [{"path": "/radio/eirp_dbw", "values": [7.0, 10.0, 13.0], "nominal": 10.0}],
        **extra,
    }
    return _write(directory, spec)


def _expand(directory: Path, spec: dict, base: Path = ISS):
    path = _write(directory, spec, base=base)
    return sweep.expand_sweep(sweep.load_sweep(path), _document(base))


def _csv(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


# --- patches -------------------------------------------------------------------------------------


def test_patch_ops_and_immutability():
    document = {
        "a": {"b": 1, "c": [10, 20, 30]},
        "d": None,
        "~k/": 5,
    }
    snapshot = copy.deepcopy(document)
    value = {"nested": [1, 2]}
    result = sweep.apply_patch(
        document,
        [
            {"op": "replace", "path": "/a/b", "value": 2},
            {"op": "add", "path": "/a/c/1", "value": 15},
            {"op": "add", "path": "/a/c/-", "value": 40},
            {"op": "remove", "path": "/a/c/0"},
            {"op": "add", "path": "/e", "value": value},
            {"op": "replace", "path": "/d", "value": 3},
            {"op": "replace", "path": "/~0k~1", "value": 6},
            {"op": "add", "path": "/a/b", "value": 9},
            {"op": "remove", "path": "/a/b"},
        ],
    )
    assert result == {"a": {"c": [15, 20, 30, 40]}, "d": 3, "~k/": 6, "e": {"nested": [1, 2]}}
    assert document == snapshot
    value["nested"].append(3)
    assert result["e"] == {"nested": [1, 2]}
    assert sweep.apply_patch(document, []) == document
    assert sweep.apply_patch(document, []) is not document


@pytest.mark.parametrize(
    ("operations", "message"),
    [
        ({"op": "replace"}, r"^/ must be an array"),
        ([[]], r"^/0 must be a JSON object"),
        ([{"op": "move", "path": "/a/b"}], r"^/0/op='move' must be one of"),
        ([{"op": "replace", "path": "/a/b"}], r"^/0/value is required for replace"),
        ([{"op": "remove", "path": "/a/b", "value": 1}], r"^/0/value is not allowed for remove"),
        ([{"op": "remove", "path": "/a/b", "from": "/x"}], r"^/0/from is not allowed"),
        ([{"op": "remove"}], r"^/0/path is required"),
        ([{"op": "remove", "path": "a/b"}], r"^/0/path='a/b' must be a JSON Pointer"),
        ([{"op": "remove", "path": ""}], r"^/0/path='' must be a JSON Pointer"),
        ([{"op": "remove", "path": "/a~2"}], r"^/0/path='/a~2' .*escape"),
        ([{"op": "replace", "path": "/missing", "value": 1}], r"^/0/path='/missing': no member"),
        ([{"op": "remove", "path": "/missing"}], r"^/0/path='/missing': no member"),
        ([{"op": "replace", "path": "/a/c/9", "value": 1}], r"^/0/path='/a/c/9': .*index"),
        ([{"op": "replace", "path": "/a/c/01", "value": 1}], r"^/0/path='/a/c/01': .*index"),
        ([{"op": "replace", "path": "/a/c/x", "value": 1}], r"^/0/path='/a/c/x': .*index"),
        ([{"op": "add", "path": "/a/c/4", "value": 1}], r"^/0/path='/a/c/4': .*index"),
        ([{"op": "add", "path": "/x/y", "value": 1}], r"^/0/path='/x/y': no member 'x'"),
        ([{"op": "add", "path": "/d/y", "value": 1}], r"^/0/path='/d/y': .*cannot hold"),
        ([{"op": "replace", "path": "/d/y/z", "value": 1}], r"^/0/path='/d/y/z': .*cannot hold"),
        (
            [{"op": "add", "path": "/a/b", "value": 1}, {"op": "remove", "path": "/zzz"}],
            r"^/1/path='/zzz'",
        ),
    ],
)
def test_patch_errors_carry_pointers(operations, message):
    document = {"a": {"b": 1, "c": [1, 2, 3]}, "d": 7}
    with pytest.raises(ValueError, match=message):
        sweep.apply_patch(document, operations)


# --- the spec ------------------------------------------------------------------------------------


def test_load_sweep_normalises_the_spec(tmp_path):
    path = _eirp(tmp_path)
    spec = sweep.load_sweep(path)
    assert spec["schema"] == "openleo.sweep/2"
    assert spec["mode"] == "oat"
    assert spec["variants"] is None
    assert spec["window"] is None
    assert spec["replay"] is None
    assert spec["max_runs"] == 512
    assert spec["base_scenario_path"] == str(ISS.resolve())
    assert spec["parameters"] == [
        {"path": "/radio/eirp_dbw", "values": [7.0, 10.0, 13.0], "nominal": 10.0}
    ]


def _spec_error(tmp_path, spec: dict, message: str):
    path = _write(tmp_path, spec)
    with pytest.raises(ValueError, match=message):
        sweep.load_sweep(path)


def test_load_sweep_rejects_bad_files(tmp_path):
    path = tmp_path / "s.json"
    with pytest.raises(ValueError, match="could not load"):
        sweep.load_sweep(path)
    path.write_bytes(b"\xff")
    with pytest.raises(ValueError, match="UTF-8"):
        sweep.load_sweep(path)
    path.write_text("{", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid JSON"):
        sweep.load_sweep(path)
    path.write_text("[1]", encoding="utf-8")
    with pytest.raises(ValueError, match="^/ must be a JSON object"):
        sweep.load_sweep(path)
    path.write_text('{"schema": "openleo.sweep/2", "schema": "x"}', encoding="utf-8")
    with pytest.raises(ValueError, match="^/schema is a duplicate JSON object key"):
        sweep.load_sweep(path)
    path.write_text('{"schema": "openleo.sweep/2", "name": NaN}', encoding="utf-8")
    with pytest.raises(ValueError, match="invalid JSON"):
        sweep.load_sweep(path)
    path.write_text('{"schema": "openleo.sweep/1"}', encoding="utf-8")
    with pytest.raises(ValueError, match=r"^/schema='openleo.sweep/1' must be exactly"):
        sweep.load_sweep(path)
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="^/schema is required"):
        sweep.load_sweep(path)
    path.write_bytes(b" " * (sweep.MAX_SPEC_BYTES + 1))
    with pytest.raises(ValueError, match="exceeds"):
        sweep.load_sweep(path)


@pytest.mark.parametrize(
    ("spec", "message"),
    [
        ({"mode": "random"}, r"^/mode='random' must be one of"),
        ({"mode": "oat"}, r"^/parameters is required for mode oat"),
        ({"mode": "variants"}, r"^/variants is required for mode variants"),
        ({"mode": "oat", "parameters": [], "x": 1}, r"^/x is not allowed"),
        ({"mode": "oat", "parameters": []}, r"^/parameters must hold 1 to 512"),
        ({"mode": "oat", "parameters": "x"}, r"^/parameters must be an array"),
        (
            {"mode": "variants", "variants": [{"id": "a", "patch": []}], "parameters": []},
            r"^/parameters is not allowed for mode variants",
        ),
        (
            {"mode": "grid", "parameters": [{"path": "/a", "values": [1]}], "variants": []},
            r"^/variants is not allowed for mode grid",
        ),
        ({"mode": "grid", "parameters": [1]}, r"^/parameters/0 must be a JSON object"),
        ({"mode": "grid", "parameters": [{"path": "/a"}]}, r"^/parameters/0/values is required"),
        (
            {"mode": "grid", "parameters": [{"path": "a", "values": [1]}]},
            r"^/parameters/0/path='a' must be a JSON Pointer",
        ),
        (
            {"mode": "grid", "parameters": [{"path": "/a", "values": []}]},
            r"^/parameters/0/values must hold 1 to 512",
        ),
        (
            {"mode": "grid", "parameters": [{"path": "/a", "values": [1, 1.0]}]},
            r"^/parameters/0/values/1 duplicates /parameters/0/values/0",
        ),
        (
            {"mode": "grid", "parameters": [{"path": "/a", "values": [1], "nominal": 1}]},
            r"^/parameters/0/nominal is not allowed for mode grid",
        ),
        (
            {"mode": "oat", "parameters": [{"path": "/a", "values": [1]}]},
            r"^/parameters/0/nominal is required for mode oat",
        ),
        (
            {"mode": "oat", "parameters": [{"path": "/a", "values": [1], "nominal": 1}]},
            r"^/parameters/0/values must hold a value other than the nominal",
        ),
        (
            {
                "mode": "grid",
                "parameters": [
                    {"path": "/a/b", "values": [1]},
                    {"path": "/a", "values": [2]},
                ],
            },
            r"^/parameters/1/path='/a' overlaps /parameters/0/path",
        ),
        (
            {
                "mode": "oat",
                "parameters": [
                    {"path": "/a", "values": [1], "nominal": 0},
                    {"path": "/a", "values": [2], "nominal": 0},
                ],
            },
            r"^/parameters/1/path='/a' overlaps /parameters/0/path",
        ),
        (
            {"mode": "variants", "variants": [{"id": "a", "patch": []}] * 2},
            r"^/variants/1/id='a' duplicates /variants/0/id",
        ),
        (
            {"mode": "variants", "variants": [{"id": "", "patch": []}]},
            r"^/variants/0/id=''",
        ),
        (
            {"mode": "variants", "variants": [{"id": "a" * 70, "patch": []}]},
            r"^/variants/0/id=.* at most 64",
        ),
        (
            {"mode": "variants", "variants": [{"id": "a"}]},
            r"^/variants/0/patch is required",
        ),
        (
            {"mode": "variants", "variants": [{"id": "a", "patch": [{"op": "zap"}]}]},
            r"^/variants/0/patch/0/op='zap' must be one of",
        ),
        (
            {"mode": "variants", "variants": [{"id": "a", "label": 3, "patch": []}]},
            r"^/variants/0/label=3 must be a string",
        ),
        (
            {"mode": "variants", "variants": [{"id": "a", "baseline": 1, "patch": []}]},
            r"^/variants/0/baseline=1 must be true or false",
        ),
        (
            {
                "mode": "variants",
                "variants": [
                    {"id": "a", "baseline": True, "patch": []},
                    {"id": "b", "baseline": True, "patch": []},
                ],
            },
            r"^/variants/1/baseline: only one variant may be the baseline",
        ),
        (
            {"mode": "variants", "variants": [{"id": "a", "patch": []}], "window": []},
            r"^/window must be a JSON object",
        ),
        (
            {"mode": "variants", "variants": [{"id": "a", "patch": []}], "max_runs": 513},
            r"^/max_runs=513 must be an integer in 1\.\.512",
        ),
        (
            {"mode": "variants", "variants": [{"id": "a", "patch": []}], "max_runs": True},
            r"^/max_runs=True must be an integer",
        ),
        (
            {"mode": "variants", "variants": [{"id": "a", "patch": []}], "name": ""},
            r"^/name=''",
        ),
        (
            {"mode": "variants", "variants": [{"id": "a", "patch": []}], "base_scenario": 3},
            r"^/base_scenario=3",
        ),
    ],
)
def test_load_sweep_errors_name_the_pointer(tmp_path, spec, message):
    path = _write(tmp_path, spec)
    if "base_scenario" in spec:
        document = json.loads(path.read_text(encoding="utf-8"))
        document["base_scenario"] = spec["base_scenario"]
        path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        sweep.load_sweep(path)


@pytest.mark.parametrize(
    ("window", "message"),
    [
        ({"duration_s": 60}, r"^/window/start_offset_s is required"),
        ({"start_offset_s": 0}, r"^/window/duration_s is required"),
        ({"start_offset_s": -1, "duration_s": 60}, r"^/window/start_offset_s=-1 must be"),
        ({"start_offset_s": 0, "duration_s": 0}, r"^/window/duration_s=0 must be"),
        ({"start_offset_s": 0, "duration_s": True}, r"^/window/duration_s=True must be"),
        ({"start_offset_s": 0, "duration_s": "x"}, r"^/window/duration_s='x' must be"),
        ({"start_offset_s": 0, "duration_s": 60, "z": 1}, r"^/window/z is not allowed"),
    ],
)
def test_window_spec_errors(tmp_path, window, message):
    spec = {"mode": "variants", "variants": [{"id": "a", "patch": []}], "window": window}
    _spec_error(tmp_path, spec, message)


def test_window_clipping_rules(tmp_path):
    base = _document()
    start = "2026-08-30T06:14:00Z"
    assert base["time"]["start_utc"] == start
    assert base["time"]["stop_utc"] == "2026-08-30T06:24:00Z"
    assert base["time"]["step_s"] == 10.0

    def clipped(window, **extra):
        spec = {"mode": "variants", "variants": [{"id": "a", "patch": []}], "window": window}
        return _expand(tmp_path, {**spec, **extra})[0].document["time"]

    time = clipped({"start_offset_s": 60, "duration_s": 120})
    assert (time["start_utc"], time["stop_utc"]) == ("2026-08-30T06:15:00Z", "2026-08-30T06:17:00Z")
    time = clipped({"start_offset_s": 0.5, "duration_s": 10})
    assert (time["start_utc"], time["stop_utc"]) == (
        "2026-08-30T06:14:00.500000Z",
        "2026-08-30T06:14:10.500000Z",
    )
    time = clipped({"start_offset_s": 0, "duration_s": 600})  # the whole base window
    assert time["stop_utc"] == "2026-08-30T06:24:00Z"
    time = clipped({"start_offset_s": 590, "duration_s": 10})  # exactly one step, at the end
    assert time["start_utc"] == "2026-08-30T06:23:50Z"
    assert base["time"]["start_utc"] == start  # the base document is untouched

    with pytest.raises(ValueError, match=r"^/window: start_offset_s \+ duration_s = 601.*base"):
        clipped({"start_offset_s": 1, "duration_s": 600})
    with pytest.raises(ValueError, match=r"^/window/duration_s=9 must be at least.*step_s 10"):
        clipped({"start_offset_s": 0, "duration_s": 9})
    with pytest.raises(ValueError, match=r"^/window/start_offset_s=1e-07 .*microsecond"):
        clipped({"start_offset_s": 1e-7, "duration_s": 60})

    network = {
        "network": {"loads_bps": [1000.0]},
    }
    long_base = GLOBAL
    spec = {
        "mode": "variants",
        "variants": [{"id": "a", "patch": []}],
        "replay": network,
        "window": {"start_offset_s": 0, "duration_s": 3601},
    }
    path = _write(tmp_path, spec, base=long_base)
    with pytest.raises(ValueError, match=r"^/window/duration_s=3601 .*at most 3600.*replay"):
        sweep.load_sweep(path)
    spec["window"]["duration_s"] = 3600
    runs = sweep.expand_sweep(
        sweep.load_sweep(_write(tmp_path, spec, base=long_base)), _document(long_base)
    )
    assert runs[0].document["time"]["stop_utc"] == "2026-09-10T13:00:00Z"
    # Without a window, a replay needs a base window of at most 3600 s.
    del spec["window"]
    with pytest.raises(ValueError, match=r"^/window: .*replay.*at most 3600.*7200"):
        sweep.expand_sweep(
            sweep.load_sweep(_write(tmp_path, spec, base=long_base)), _document(long_base)
        )


# --- expansion -----------------------------------------------------------------------------------


def test_oat_expansion_ids_labels_and_nominal(tmp_path):
    base = _document()
    path = _eirp(tmp_path)
    spec = sweep.load_sweep(path)
    runs = sweep.expand_sweep(spec, base)
    assert [r.id for r in runs] == [
        "oat%3Anominal",
        "oat%3A%2Fradio%2Feirp_dbw%3D7.0",
        "oat%3A%2Fradio%2Feirp_dbw%3D13.0",
    ]
    assert [r.label for r in runs] == ["nominal", "/radio/eirp_dbw = 7.0", "/radio/eirp_dbw = 13.0"]
    assert runs[0].document == base
    assert runs[1].document["radio"]["eirp_dbw"] == 7.0
    assert base["radio"]["eirp_dbw"] == 10.0
    assert all(isinstance(r, sweep.SweepRun) and len(r.sha256) == 64 for r in runs)
    assert len({r.sha256 for r in runs}) == 3
    assert runs[0].sha256 == sweep.document_sha256(base)
    assert sweep.baseline_run_id(spec, runs) == runs[0].id
    for run_id in (r.id for r in runs):
        assert "/" not in run_id and ":" not in run_id and "=" not in run_id


def test_oat_nominal_must_reproduce_the_base(tmp_path):
    spec = {
        "mode": "oat",
        "parameters": [{"path": "/radio/eirp_dbw", "values": [7.0], "nominal": 11.0}],
    }
    with pytest.raises(
        ValueError, match=r"^/parameters/0/nominal=11\.0 must equal the base .*10\.0"
    ):
        _expand(tmp_path, spec)
    spec["parameters"][0] = {"path": "/radio/nope", "values": [7.0], "nominal": 1}
    with pytest.raises(ValueError, match=r"^/parameters/0/path='/radio/nope': no member 'nope'"):
        _expand(tmp_path, spec)
    # Integer and float spellings of the same number are the same value; booleans are not numbers.
    spec["parameters"][0] = {"path": "/radio/eirp_dbw", "values": [7.0], "nominal": 10}
    assert len(_expand(tmp_path, spec)) == 2
    spec["parameters"][0] = {
        "path": "/space_segment/synthetic_rf",
        "values": [True],
        "nominal": 0,
    }
    with pytest.raises(ValueError, match=r"nominal=0 must equal the base"):
        _expand(tmp_path, spec)
    spec["parameters"][0] = {
        "path": "/ground_segment/stations",
        "values": [[]],
        "nominal": _document()["ground_segment"]["stations"],
    }
    with pytest.raises(
        ValueError, match=r"^/parameters/0/values/0: generated scenario .*is invalid"
    ):
        _expand(tmp_path, spec)


def test_oat_invalid_value_names_the_parameter(tmp_path):
    spec = {
        "mode": "oat",
        "parameters": [{"path": "/radio/eirp_dbw", "values": ["loud"], "nominal": 10.0}],
    }
    with pytest.raises(ValueError, match=r"^/parameters/0/values/0: generated scenario .*eirp_dbw"):
        _expand(tmp_path, spec)


def test_grid_limit_512(tmp_path):
    def parameters(counts):
        paths = ["/radio/eirp_dbw", "/radio/carrier_frequency_hz", "/adaptation/hysteresis_db"]
        bases = [1.0, 2.2e9, 0.1]
        return [
            {"path": path, "values": [base + i * 0.01 for i in range(count)]}
            for path, base, count in zip(paths, bases, counts, strict=True)
        ]

    runs = _expand(tmp_path, {"mode": "grid", "parameters": parameters((8, 8, 8))})
    assert len(runs) == 512
    assert [r.id for r in runs[:3]] == ["grid%3A0", "grid%3A1", "grid%3A2"]
    assert runs[0].document["radio"]["eirp_dbw"] == 1.0
    assert runs[1].document["adaptation"]["hysteresis_db"] == pytest.approx(0.11)
    assert runs[8].document["radio"]["carrier_frequency_hz"] == pytest.approx(2.2e9 + 0.01)
    assert runs[-1].document["radio"]["eirp_dbw"] == pytest.approx(1.07)
    assert len({r.sha256 for r in runs}) == 512
    spec = sweep.load_sweep(_write(tmp_path, {"mode": "grid", "parameters": parameters((8, 8, 8))}))
    assert sweep.baseline_run_id(spec, runs) == "grid%3A0"
    with pytest.raises(ValueError, match=r"^/parameters: the grid has 576 runs.*at most 512"):
        _expand(tmp_path, {"mode": "grid", "parameters": parameters((8, 8, 9))})
    with pytest.raises(ValueError, match=r"^/parameters: the grid has 8 runs.*at most 4"):
        _expand(tmp_path, {"mode": "grid", "parameters": parameters((2, 2, 2)), "max_runs": 4})
    with pytest.raises(ValueError, match=r"^/parameters: the oat sweep has 17 runs.*at most 16"):
        _expand(
            tmp_path,
            {
                "mode": "oat",
                "parameters": [
                    {
                        "path": "/radio/eirp_dbw",
                        "values": [i + 20.0 for i in range(16)],
                        "nominal": 10.0,
                    }
                ],
                "max_runs": 16,
            },
        )
    with pytest.raises(ValueError, match=r"^/variants: .*3 runs.*at most 2"):
        _expand(
            tmp_path,
            {
                "mode": "variants",
                "variants": [{"id": str(i), "patch": []} for i in range(3)],
                "max_runs": 2,
            },
        )


def test_variants_expansion_baseline_and_ids(tmp_path):
    variants = [
        {"id": "first", "patch": []},
        {
            "id": "weird id/x",
            "label": "Weird",
            "patch": [{"op": "replace", "path": "/radio/eirp_dbw", "value": 12.0}],
            "baseline": True,
        },
    ]
    path = _write(tmp_path, {"mode": "variants", "variants": variants})
    spec = sweep.load_sweep(path)
    runs = sweep.expand_sweep(spec, _document())
    assert [r.id for r in runs] == ["variant%3Afirst", "variant%3Aweird%20id%2Fx"]
    assert [r.label for r in runs] == ["first", "Weird"]
    assert sweep.baseline_run_id(spec, runs) == "variant%3Aweird%20id%2Fx"
    variants[1]["baseline"] = False
    spec = sweep.load_sweep(_write(tmp_path, {"mode": "variants", "variants": variants}))
    assert sweep.baseline_run_id(spec, sweep.expand_sweep(spec, _document())) == "variant%3Afirst"


def test_patch_errors_in_a_variant_name_its_location(tmp_path):
    variants = [{"id": "a", "patch": [{"op": "replace", "path": "/radio/nope", "value": 1}]}]
    with pytest.raises(ValueError, match=r"^/variants/0/patch/0/path='/radio/nope': no member"):
        _expand(tmp_path, {"mode": "variants", "variants": variants})
    variants = [{"id": "a", "patch": [{"op": "replace", "path": "/radio/eirp_dbw", "value": "x"}]}]
    with pytest.raises(ValueError, match=r"^/variants/0: generated scenario .*is invalid: /radio"):
        _expand(tmp_path, {"mode": "variants", "variants": variants})


def test_long_ids_are_refused(tmp_path):
    spec = {
        "mode": "oat",
        "parameters": [
            {
                "path": "/space_segment/select_norad_ids",
                "values": [list(range(1, 80))],
                "nominal": None,
            }
        ],
    }
    with pytest.raises(ValueError, match=r"^/parameters/0/values/0: run id .* exceeds 200"):
        _expand(tmp_path, spec)


def test_hydrometeors_need_itu_reference_in_a_variant(tmp_path):
    variants = [
        {
            "id": "free_space",
            "patch": [{"op": "replace", "path": "/propagation/model", "value": "free_space"}],
        }
    ]
    with pytest.raises(
        ValueError, match=r"^/variants/0: .*requires propagation.model itu_reference"
    ):
        _expand(tmp_path, {"mode": "variants", "variants": variants}, base=HYDROMETEORS)


# --- replay validation and the seam --------------------------------------------------------------

NETWORK_REPLAY = {
    "network": {
        "routing_models": ["minimum_delay"],
        "loads_bps": [10000, 300000],
        "packet_size_bytes": 1200,
        "queue_packets": 8,
        "flows_per_direction": 2,
        "acquisition_delay_s": 0.1,
        "seed": 1,
    }
}


def _replay_spec(replay, **extra):
    return {
        "mode": "variants",
        "variants": [{"id": "a", "patch": []}],
        "window": {"start_offset_s": 0, "duration_s": 300},
        "replay": replay,
        **extra,
    }


def test_replay_spec_is_normalised(tmp_path):
    path = _write(tmp_path, _replay_spec(NETWORK_REPLAY), base=GLOBAL)
    replay = sweep.load_sweep(path)["replay"]
    assert replay["link"] is None
    assert replay["network"] == {**NETWORK_REPLAY["network"], "loads_bps": [10000.0, 300000.0]}
    link = {"link": {"station": "Madrid", "norad_id": 41920, "loads_bps": [5000]}}
    replay = sweep.load_sweep(_write(tmp_path, _replay_spec(link), base=GLOBAL))["replay"]
    assert replay["network"] is None
    assert replay["link"] == {
        "station": "Madrid",
        "norad_id": 41920,
        "loads_bps": [5000.0],
        "packet_size_bytes": 512,
        "queue_packets": 32,
        "seed": 1,
    }
    network = sweep.load_sweep(
        _write(tmp_path, _replay_spec({"network": {"loads_bps": [1000]}}), base=GLOBAL)
    )["replay"]["network"]
    assert network["routing_models"] is None
    assert network["flows_per_direction"] == 1
    assert network["acquisition_delay_s"] == 0.0


@pytest.mark.parametrize(
    ("replay", "message"),
    [
        ([], r"^/replay must be a JSON object"),
        ({}, r"^/replay must name a link or a network replay"),
        ({"link": None, "network": None}, r"^/replay must name a link or a network replay"),
        ({"x": 1}, r"^/replay/x is not allowed"),
        ({"network": 3}, r"^/replay/network must be a JSON object"),
        ({"network": {}}, r"^/replay/network/loads_bps is required"),
        ({"network": {"loads_bps": []}}, r"^/replay/network/loads_bps must hold 1 to 8"),
        ({"network": {"loads_bps": [0]}}, r"^/replay/network/loads_bps/0=0 must be"),
        ({"network": {"loads_bps": [1e10]}}, r"^/replay/network/loads_bps/0=10000000000.0 must be"),
        ({"network": {"loads_bps": [1, 1]}}, r"^/replay/network/loads_bps/1 duplicates"),
        (
            {"network": {"loads_bps": [1], "packet_size_bytes": 10}},
            r"^/replay/network/packet_size_bytes=10 must be an integer in 64\.\.1400",
        ),
        (
            {"network": {"loads_bps": [1], "queue_packets": 0}},
            r"^/replay/network/queue_packets=0 must be an integer in 1\.\.10000",
        ),
        (
            {"network": {"loads_bps": [1], "flows_per_direction": 5}},
            r"^/replay/network/flows_per_direction=5 must be an integer in 1\.\.4",
        ),
        (
            {"network": {"loads_bps": [1], "seed": 0}},
            r"^/replay/network/seed=0 must be an integer in 1\.\.2147483647",
        ),
        (
            {"network": {"loads_bps": [1], "acquisition_delay_s": 61}},
            r"^/replay/network/acquisition_delay_s=61 must be",
        ),
        (
            {"network": {"loads_bps": [1], "routing_models": ["fastest"]}},
            r"^/replay/network/routing_models/0='fastest' must be one of",
        ),
        (
            {"network": {"loads_bps": [1], "routing_models": []}},
            r"^/replay/network/routing_models must hold 1 to 3",
        ),
        (
            {"network": {"loads_bps": [1], "routing_models": ["minimum_delay"] * 2}},
            r"^/replay/network/routing_models/1 duplicates",
        ),
        ({"link": {"loads_bps": [1], "norad_id": 1}}, r"^/replay/link/station is required"),
        (
            {"link": {"loads_bps": [1], "station": "M"}},
            r"^/replay/link/norad_id is required",
        ),
        (
            {"link": {"loads_bps": [1], "station": "", "norad_id": 1}},
            r"^/replay/link/station=''",
        ),
        (
            {"link": {"loads_bps": [1], "station": "M", "norad_id": 0}},
            r"^/replay/link/norad_id=0 must be an integer in 1\.\.",
        ),
    ],
)
def test_replay_spec_errors(tmp_path, replay, message):
    _spec_error(tmp_path, _replay_spec(replay), message)


def test_replay_run_checks_against_each_scenario(tmp_path):
    # The ISS scenario has no network.
    path = _write(
        tmp_path, _replay_spec(NETWORK_REPLAY, window={"start_offset_s": 0, "duration_s": 60})
    )
    with pytest.raises(ValueError, match=r"^/replay/network: run variant%3Aa has no network"):
        sweep.expand_sweep(sweep.load_sweep(path), _document())
    models = {"network": {**NETWORK_REPLAY["network"], "routing_models": ["maximum_rate"]}}
    base = _document(GLOBAL)
    base["network"]["route_models"] = ["minimum_delay"]
    base["network"]["fixed_capacity_bps"] = None
    path = _write(tmp_path, _replay_spec(models), base=GLOBAL)
    with pytest.raises(
        ValueError, match=r"^/replay/network/routing_models/0='maximum_rate': run .*"
    ):
        sweep.expand_sweep(sweep.load_sweep(path), base)
    link = {"link": {"station": "Atlantis", "norad_id": 5, "loads_bps": [1000]}}
    path = _write(tmp_path, _replay_spec(link), base=GLOBAL)
    with pytest.raises(
        ValueError, match=r"^/replay/link/station='Atlantis': run .* no such ground station"
    ):
        sweep.expand_sweep(sweep.load_sweep(path), _document(GLOBAL))
    # A valid replay expands.
    path = _write(tmp_path, _replay_spec(NETWORK_REPLAY), base=GLOBAL)
    assert len(sweep.expand_sweep(sweep.load_sweep(path), _document(GLOBAL))) == 1


def test_replays_seam_is_a_stub():
    with pytest.raises(NotImplementedError, match="replay bridges arrive in Task 15"):
        sweep._run_replays(Path("."), {}, None, None)


# --- running -------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def eirp_sweep(tmp_path_factory):
    directory = tmp_path_factory.mktemp("eirp")
    spec_path = _eirp(directory)
    output = directory / "out"
    summary = sweep.run_sweep(spec_path, output)
    return spec_path, output, summary


def test_oat_nominal_equals_base_tables_bytewise(eirp_sweep, tmp_path):
    from openleo import run, save_run

    _, output, summary = eirp_sweep
    scenario = load_scenario(ISS)
    base_dir = save_run(scenario, run(scenario), tmp_path / "base")
    nominal = output / "runs" / "oat%3Anominal"
    base_tables = sorted((base_dir / "tables").iterdir())
    assert base_tables
    assert [p.name for p in sorted((nominal / "tables").iterdir())] == [p.name for p in base_tables]
    for path in base_tables:
        assert (nominal / "tables" / path.name).read_bytes() == path.read_bytes(), path.name
    base_results, nominal_results = load_results(base_dir), load_results(nominal)
    assert dict(base_results.tables) == dict(nominal_results.tables)
    changed = load_results(output / "runs" / "oat%3A%2Fradio%2Feirp_dbw%3D13.0")
    assert dict(changed.tables) != dict(nominal_results.tables)
    assert summary["baseline_run_id"] == "oat%3Anominal"


def test_sweep_output_layout_and_labels(eirp_sweep):
    spec_path, output, summary = eirp_sweep
    assert sorted(p.name for p in output.iterdir()) == [
        "comparisons.csv",
        "manifest.json",
        "runs",
        "sweep.json",
    ]
    verify_manifest(output)
    document = json.loads((output / "sweep.json").read_text(encoding="utf-8"))
    assert document == json.loads(json.dumps(summary))
    assert document["kind"] == "openleo.sweep"
    assert document["comparison_label"] == COMPARISON_LABEL
    assert COMPARISON_LABEL in document["limitations"]
    assert "uncertainty" in " ".join(document["limitations"]).lower()
    assert document["mode"] == "oat"
    assert "base_scenario_path" not in document["spec"]
    assert document["spec"]["base_scenario"] == os.path.relpath(ISS, spec_path.parent)
    base = load_scenario(ISS)
    assert document["base"]["source_sha256"] == base.source_sha256
    assert document["base"]["normalised_sha256"] == sweep.document_sha256(scenario_document(base))
    assert document["spec_sha256"] == sweep.document_sha256(
        json.loads(spec_path.read_text(encoding="utf-8"))
    )
    assert [r["id"] for r in document["runs"]] == [
        "oat%3Anominal",
        "oat%3A%2Fradio%2Feirp_dbw%3D7.0",
        "oat%3A%2Fradio%2Feirp_dbw%3D13.0",
    ]
    assert sorted(p.name for p in (output / "runs").iterdir()) == sorted(
        r["id"] for r in document["runs"]
    )
    for entry in document["runs"]:
        assert entry["directory"] == f"runs/{entry['id']}"
        assert entry["base_sha256"] == document["base"]["normalised_sha256"]
        results = load_results(output / "runs" / entry["id"])
        assert results.metadata["scenario"]["normalised_sha256"] == entry["scenario_sha256"]
        assert entry["replay"] is None
    assert document["replay"] is None
    assert document["comparisons"]["file"] == "comparisons.csv"
    rows = _csv(output / "comparisons.csv")
    assert document["comparisons"]["rows"] == len(rows)
    assert list(rows[0]) == ["run_id", "metric", "value", "baseline_value", "delta", "ratio"]


def test_zero_effect_rows_preserved(eirp_sweep):
    _, output, _ = eirp_sweep
    rows = _csv(output / "comparisons.csv")
    by_run = {}
    for row in rows:
        by_run.setdefault(row["run_id"], {})[row["metric"]] = row
    nominal = by_run["oat%3Anominal"]
    assert set(by_run) == {
        "oat%3Anominal",
        "oat%3A%2Fradio%2Feirp_dbw%3D7.0",
        "oat%3A%2Fradio%2Feirp_dbw%3D13.0",
    }
    metrics = set(nominal)
    assert all(set(v) == metrics for v in by_run.values())
    assert "station/Cartagena/visible_fraction" in metrics
    assert "pair/Cartagena/25544/pass_count" in metrics
    for row in nominal.values():
        assert row["value"] == row["baseline_value"]
        assert float(row["delta"]) == 0.0
        assert row["ratio"] in ("1", "")
    higher = by_run["oat%3A%2Fradio%2Feirp_dbw%3D13.0"]
    # EIRP does not move geometry: the row is kept, with delta 0 and ratio 1.
    zero = higher["pair/Cartagena/25544/visible_duration_s"]
    assert float(zero["delta"]) == 0.0
    assert zero["ratio"] == "1"
    assert zero["value"] == zero["baseline_value"]
    # It does move the link: a non-zero delta with a ratio.
    moved = higher["pair/Cartagena/25544/integrated_shannon_upper_bound_bits"]
    assert float(moved["delta"]) > 0.0
    assert float(moved["ratio"]) == pytest.approx(
        float(moved["value"]) / float(moved["baseline_value"]), rel=1e-12
    )


def test_comparison_rows_definitions():
    base = {"a": 2.0, "zero": 0.0, "nan": math.nan, "only_base": 1.0, "count": 3.0}
    other = {"a": 3.0, "zero": 5.0, "nan": 1.0, "new": 4.0, "count": 3.0}
    rows = sweep.comparison_rows([("base", base), ("other", other)], "base")
    index = {(r["run_id"], r["metric"]): r for r in rows}
    assert index[("base", "a")] == {
        "run_id": "base",
        "metric": "a",
        "value": "2",
        "baseline_value": "2",
        "delta": "0",
        "ratio": "1",
    }
    assert index[("other", "a")]["delta"] == "1"
    assert index[("other", "a")]["ratio"] == "1.5"
    assert index[("other", "zero")]["delta"] == "5"
    assert index[("other", "zero")]["ratio"] == ""  # baseline 0: no ratio
    assert index[("base", "zero")]["ratio"] == ""
    assert index[("other", "nan")]["baseline_value"] == ""
    assert index[("other", "nan")]["delta"] == ""
    assert index[("other", "nan")]["ratio"] == ""
    assert index[("base", "nan")]["value"] == ""
    assert index[("other", "new")]["baseline_value"] == ""
    assert index[("other", "new")]["delta"] == ""
    assert ("other", "only_base") not in index
    assert index[("other", "count")]["delta"] == "0"
    assert [r["metric"] for r in rows if r["run_id"] == "base"] == list(base)
    with pytest.raises(ValueError, match="baseline"):
        sweep.comparison_rows([("base", base)], "nope")


def test_comparison_number_text():
    assert sweep.number_text(3.0) == "3"
    assert sweep.number_text(-0.0) == "0"
    assert sweep.number_text(0.1) == "0.1"
    assert sweep.number_text(1e20) == "1e+20"
    assert sweep.number_text(math.nan) == ""
    assert sweep.number_text(math.inf) == "inf"
    assert sweep.number_text(123456789012.0) == "123456789012"


def test_flatten_metrics_on_a_networked_run(tmp_path):
    from openleo import run

    scenario = load_scenario(GLOBAL)
    document = scenario_document(scenario)
    document["time"]["stop_utc"] = "2026-09-10T12:05:00Z"
    document["time"]["convergence"]["mode"] = "off"
    document["space_segment"]["select_norad_ids"] = [
        int(row.split(",")[11])
        for row in scenario.space_segment.catalogs[0].path.read_text().splitlines()[1:4]
    ]
    from openleo.scenario import parse_scenario

    results = run(parse_scenario(document, GLOBAL, "0" * 64))
    metrics = sweep.flatten_metrics(results)
    assert all(math.isfinite(v) or math.isnan(v) for v in metrics.values())
    kinds = {key.split("/")[0] for key in metrics}
    assert kinds == {"pair", "station", "route"}
    assert "route/Madrid->Tromso/minimum_delay/connected_fraction" in metrics
    assert any(key.startswith("station/Madrid/") for key in metrics)
    assert any(key.startswith("pair/Madrid/") and key.endswith("/pass_count") for key in metrics)


def test_replay_seam_is_not_called_without_replay(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("the replay seam must not be called")

    monkeypatch.setattr(sweep, "_run_replays", forbidden)
    spec_path = _write(
        tmp_path,
        {
            "mode": "variants",
            "variants": [{"id": "only", "patch": []}],
            "window": {"start_offset_s": 0, "duration_s": 120},
        },
    )
    summary = sweep.run_sweep(spec_path, tmp_path / "out", packet_backend=tmp_path / "x")
    assert summary["replay"] is None
    assert summary["runs"][0]["replay"] is None


def _scenario_with_network(directory: Path) -> Path:
    document = _document(GLOBAL)
    document["space_segment"]["select_norad_ids"] = [
        int(row.split(",")[11])
        for row in load_scenario(GLOBAL)
        .space_segment.catalogs[0]
        .path.read_text()
        .splitlines()[1:4]
    ]
    document["time"]["convergence"]["mode"] = "off"
    path = directory / "base" / "scenario.json"
    path.parent.mkdir()
    catalog = load_scenario(GLOBAL).space_segment.catalogs[0].path
    document["space_segment"]["catalogs"][0]["path"] = os.path.relpath(catalog, path.parent)
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def test_replay_seam_receives_each_run_and_the_evidence_is_recorded(tmp_path, monkeypatch):
    base = _scenario_with_network(tmp_path)
    calls = []

    def seam(run_dir, replay_spec, packet_backend, network_backend):
        calls.append((run_dir, replay_spec, packet_backend, network_backend))
        assert (run_dir / "results.json").is_file()
        return {"evidence": "actual_ns3", "replays": []}

    monkeypatch.setattr(sweep, "_run_replays", seam)
    spec = _replay_spec(NETWORK_REPLAY)
    spec["variants"] = [
        {"id": "a", "patch": []},
        {"id": "b", "patch": [{"op": "replace", "path": "/radio/eirp_dbw", "value": 18.0}]},
    ]
    spec["window"] = {"start_offset_s": 0, "duration_s": 120}
    spec_path = _write(tmp_path, spec, base=base)
    backend = tmp_path / "ns3"
    summary = sweep.run_sweep(spec_path, tmp_path / "out", network_backend=backend)
    assert [c[0].name for c in calls] == ["variant%3Aa", "variant%3Ab"]
    assert all(c[0].parent.name == "runs" for c in calls)
    assert all(c[2] is None and c[3] == backend for c in calls)
    assert all(c[1] == summary["spec"]["replay"] for c in calls)
    assert summary["replay"] == {"evidence": "actual", "missing": []}
    assert all(r["replay"] == {"evidence": "actual_ns3", "replays": []} for r in summary["runs"])
    verify_manifest(tmp_path / "out")


def test_replay_without_its_backend_is_recorded_as_missing(tmp_path, monkeypatch):
    base = _scenario_with_network(tmp_path)

    def forbidden(*args, **kwargs):
        raise AssertionError("no backend, no replay")

    monkeypatch.setattr(sweep, "_run_replays", forbidden)
    spec = _replay_spec(NETWORK_REPLAY, window={"start_offset_s": 0, "duration_s": 120})
    summary = sweep.run_sweep(_write(tmp_path, spec, base=base), tmp_path / "out")
    assert summary["replay"] == {"evidence": "missing", "missing": ["network"]}
    assert summary["runs"][0]["replay"] is None
    assert any("replay" in text and "missing" in text for text in summary["limitations"])


def test_partial_replay_only_runs_the_available_kinds(tmp_path, monkeypatch):
    base = _scenario_with_network(tmp_path)
    seen = []

    def seam(run_dir, replay_spec, packet_backend, network_backend):
        seen.append((replay_spec, packet_backend, network_backend))
        return {"evidence": "actual_ns3", "replays": []}

    monkeypatch.setattr(sweep, "_run_replays", seam)
    both = {
        **NETWORK_REPLAY,
        "link": {"station": "Madrid", "norad_id": 41920, "loads_bps": [1000]},
    }
    spec = _replay_spec(both, window={"start_offset_s": 0, "duration_s": 120})
    summary = sweep.run_sweep(
        _write(tmp_path, spec, base=base), tmp_path / "out", packet_backend=tmp_path / "p"
    )
    assert summary["replay"] == {"evidence": "partial", "missing": ["network"]}
    replay_spec = seen[0][0]
    assert replay_spec["network"] is None and replay_spec["link"]["station"] == "Madrid"
    assert seen[0][1:] == (tmp_path / "p", None)


def test_a_failing_run_publishes_nothing(tmp_path, monkeypatch):
    spec_path = _eirp(tmp_path)
    real = sweep.run_engine
    calls = []

    def flaky(scenario, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise ValueError("boom")
        return real(scenario, **kwargs)

    monkeypatch.setattr(sweep, "run_engine", flaky)
    out = tmp_path / "out"
    with pytest.raises(ValueError, match=r"^run oat%3A%2Fradio%2Feirp_dbw%3D7.0: boom"):
        sweep.run_sweep(spec_path, out)
    assert not out.exists()
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith("out")] == []


def test_existing_output_is_refused(eirp_sweep):
    spec_path, output, _ = eirp_sweep
    with pytest.raises(FileExistsError):
        sweep.run_sweep(spec_path, output)


def test_workers_argument_is_passed_through(tmp_path, monkeypatch):
    real = sweep.run_engine
    seen = []

    def spy(scenario, **kwargs):
        seen.append(kwargs)
        return real(scenario, **kwargs)

    monkeypatch.setattr(sweep, "run_engine", spy)
    spec_path = _write(tmp_path, {"mode": "variants", "variants": [{"id": "v", "patch": []}]})
    sweep.run_sweep(spec_path, tmp_path / "out", workers=2)
    assert seen == [{"workers": 2}]


# --- v1 aligned cases ----------------------------------------------------------------------------

ANGLE_DEG, RANGE_M, DB = 1e-8, 1e-4, 1e-9
ALIGNED_TOLERANCES = {
    "elevation_deg": ANGLE_DEG,
    "azimuth_deg": ANGLE_DEG,
    "range_m": RANGE_M,
    "doppler_hz": 1e-3,
    "delay_s": RANGE_M / 299_792_458.0 + 1e-13,
    "cn0_db_hz": DB,
    "esn0_db": DB,
    "snr_db": DB,
    "margin_db": DB,
    "gaseous_attenuation_db": DB,
    "hydrometeor_attenuation_db": DB,
    "total_atmospheric_attenuation_db": DB,
    "rain_attenuation_db": DB,
    "cloud_attenuation_db": DB,
}
ALIGNED = {
    "free_space": "iridium_global",
    "itu_reference": "iridium_global_reference",
    "hydrometeors": "iridium_global_hydrometeors",
}


def test_variants_match_v1_aligned_cases(tmp_path):
    """Free space, reference and hydrometeor variants reproduce v1's per-sample values.

    The window is the first ten minutes of the fixtures' grid (events off, so samples are
    grid samples as in v1) and ten satellites. The last sample is left out: its pass state
    depends on the window end, which differs from the fixtures' two hours.
    """
    catalog = load_scenario(HYDROMETEORS).space_segment.catalogs[0].path
    ids = [int(row.split(",")[11]) for row in catalog.read_text().splitlines()[1:11]]
    common = [
        {
            "op": "replace",
            "path": "/time/events",
            "value": {"enabled": False, "tolerance_s": 0.001},
        },
        {"op": "replace", "path": "/time/convergence/mode", "value": "off"},
        {"op": "replace", "path": "/space_segment/select_norad_ids", "value": ids},
    ]
    variants = [
        {
            "id": "free_space",
            "patch": [
                *common,
                {"op": "replace", "path": "/propagation", "value": {"model": "free_space"}},
            ],
        },
        {
            "id": "itu_reference",
            "patch": [
                *common,
                {"op": "replace", "path": "/propagation/hydrometeors", "value": None},
            ],
        },
        {"id": "hydrometeors", "patch": common},
    ]
    spec = {
        "mode": "variants",
        "variants": variants,
        "window": {"start_offset_s": 0, "duration_s": 600},
    }
    path = _write(tmp_path, spec, base=HYDROMETEORS)
    sweep.run_sweep(path, tmp_path / "out")
    compared = {}
    for variant, fixture_name in ALIGNED.items():
        results = load_results(tmp_path / "out" / "runs" / f"variant%3A{variant}")
        assert results.metadata["events"]["enabled"] is False
        samples = results.tables["samples"]
        fixture = json.loads(
            gzip.decompress((V1_FIXTURES / f"{fixture_name}.json.gz").read_bytes())
        )
        rows = {
            key: index
            for index, key in enumerate(
                zip(
                    samples["station_index"].tolist(),
                    samples["satellite_index"].tolist(),
                    samples["time_index"].tolist(),
                    strict=True,
                )
            )
            if key[0] <= 2 and key[2] <= 9
        }
        links = [link for link in fixture["links"] if link["time_index"] <= 9]
        assert links
        assert set(rows) == {
            (r["station_index"], r["satellite_index"], r["time_index"]) for r in links
        }
        names = {c.name for c in samples.columns}
        checked = set()
        for link in links:
            row = rows[(link["station_index"], link["satellite_index"], link["time_index"])]
            assert samples["modcod"][row] == (link["modcod"] or "")
            assert samples["rate_bps"][row] == link["rate_bps"]
            for field, tolerance in ALIGNED_TOLERANCES.items():
                if field not in link:
                    continue
                assert field in names
                error = abs(float(samples[field][row]) - link[field])
                if field == "azimuth_deg":
                    error = min(error % 360.0, 360.0 - error % 360.0)
                assert error <= tolerance, (variant, field, link["time_index"], error)
                checked.add(field)
        compared[variant] = checked
    assert {"elevation_deg", "esn0_db", "margin_db"} <= compared["free_space"]
    assert "gaseous_attenuation_db" in compared["itu_reference"]
    assert "hydrometeor_attenuation_db" in compared["hydrometeors"]
    assert "gaseous_attenuation_db" not in compared["free_space"]
    assert "hydrometeor_attenuation_db" not in compared["itu_reference"]


# --- shipped examples ----------------------------------------------------------------------------

EXAMPLE_SWEEPS = {
    "propagation_cases": ("variants", 3),
    "eirp_oat": ("oat", 3),
    "sampling_fidelity": ("grid", 6),
    "release_matrix": ("grid", 8),
}


@pytest.mark.parametrize("name", sorted(EXAMPLE_SWEEPS))
def test_example_sweeps_load_and_expand(name):
    path = EXAMPLES / "sweeps" / f"{name}.json"
    spec = sweep.load_sweep(path)
    mode, count = EXAMPLE_SWEEPS[name]
    assert spec["mode"] == mode
    base = load_scenario(Path(spec["base_scenario_path"]))
    runs = sweep.expand_sweep(spec, scenario_document(base))
    assert len(runs) == count
    assert len({r.id for r in runs}) == count
    assert sweep.baseline_run_id(spec, runs) in {r.id for r in runs}


def test_example_propagation_cases_cover_the_v1_aligned_cases():
    spec = sweep.load_sweep(EXAMPLES / "sweeps" / "propagation_cases.json")
    runs = {r.id: r for r in sweep.expand_sweep(spec, _document(HYDROMETEORS))}
    free = runs["variant%3Afree_space"].document["propagation"]
    reference = runs["variant%3Aitu_reference"].document["propagation"]
    declared = runs["variant%3Aitu_reference_hydrometeors"].document["propagation"]
    assert free["model"] == "free_space" and free["hydrometeors"] is None
    assert reference["model"] == "itu_reference" and reference["hydrometeors"] is None
    assert declared["model"] == "itu_reference" and declared["hydrometeors"] is not None


def test_example_release_matrix_reproduces_the_v1_frozen_matrix():
    v1 = json.loads((ROOT / "examples" / "studies" / "release_benchmark.json").read_text())
    spec = sweep.load_sweep(EXAMPLES / "sweeps" / "release_matrix.json")
    by_path = {p["path"]: p["values"] for p in spec["parameters"]}
    assert by_path["/radio/carrier_frequency_hz"] == v1["frequencies_hz"]
    assert by_path["/time/step_s"] == v1["sampling_steps_s"]
    assert by_path["/propagation/refinement"] == v1["refinements"]
    network = spec["replay"]["network"]
    assert network["loads_bps"] == v1["traffic_loads_bps"]
    for key in (
        "packet_size_bytes",
        "queue_packets",
        "flows_per_direction",
        "acquisition_delay_s",
        "seed",
    ):
        assert network[key] == v1[key]
    assert spec["window"] == {
        "start_offset_s": v1["start_offset_s"],
        "duration_s": v1["duration_s"],
    }
    assert spec["base_scenario_path"] == str(HYDROMETEORS.resolve())


def test_example_sampling_fidelity_mirrors_v1():
    v1 = json.loads((ROOT / "examples" / "studies" / "reference_fidelity.json").read_text())
    cases = {
        case["id"]: json.loads((ROOT / "examples" / "studies" / case["scenario"]).read_text())
        for case in v1["cases"]
    }
    spec = sweep.load_sweep(EXAMPLES / "sweeps" / "sampling_fidelity.json")
    by_path = {p["path"]: p["values"] for p in spec["parameters"]}
    assert by_path["/radio/carrier_frequency_hz"] == [
        cases[c]["radio_link"]["carrier_frequency_hz"] for c in ("12ghz", "20ghz")
    ]
    assert by_path["/time/step_s"] == v1["sampling_steps_s"]
    window = cases["12ghz"]["time_window"]
    assert spec["window"]["duration_s"] == 1200.0 == 20 * 60  # v1: 12:00 to 12:20
    assert (window["start_utc"], window["stop_utc"]) == (
        "2026-09-10T12:00:00Z",
        "2026-09-10T12:20:00Z",
    )
    runs = sweep.expand_sweep(spec, _document(Path(spec["base_scenario_path"])))
    combos = {
        (r.document["radio"]["carrier_frequency_hz"], r.document["time"]["step_s"]) for r in runs
    }
    assert combos == {(f, s) for f in (12e9, 20e9) for s in (30.0, 60.0, 120.0)}
    assert {r.document["propagation"]["model"] for r in runs} == {"itu_reference"}


def test_oat_over_an_array_element_and_deep_json(tmp_path):
    spec = {
        "mode": "oat",
        "parameters": [
            {
                "path": "/ground_segment/stations/0/latitude_deg",
                "values": [38.0],
                "nominal": 37.6057,
            }
        ],
    }
    runs = _expand(tmp_path, spec)
    assert runs[1].document["ground_segment"]["stations"][0]["latitude_deg"] == 38.0
    spec["parameters"][0]["path"] = "/ground_segment/stations/3/latitude_deg"
    with pytest.raises(ValueError, match=r"^/parameters/0/path=.*not a valid array index"):
        _expand(tmp_path, spec)
    path = tmp_path / "deep.json"
    path.write_text("[" * 100_000, encoding="utf-8")
    with pytest.raises(ValueError, match="invalid JSON"):
        sweep.load_sweep(path)


def test_expand_checks_the_replay_window_of_a_hand_built_spec(tmp_path):
    spec = sweep.load_sweep(_write(tmp_path, _replay_spec(NETWORK_REPLAY), base=GLOBAL))
    spec["window"] = {"start_offset_s": 0, "duration_s": 3601}
    with pytest.raises(ValueError, match=r"^/window/duration_s=3601 must be at most 3600"):
        sweep.expand_sweep(spec, _document(GLOBAL))


@pytest.mark.slow
def test_propagation_cases_example_runs_and_orders_the_cases(tmp_path):
    """Declared rain and cloud cost more usable time than gases, which cost more than none."""
    workers = min(8, os.cpu_count() or 1)
    summary = sweep.run_sweep(
        EXAMPLES / "sweeps" / "propagation_cases.json", tmp_path / "out", workers=workers
    )
    assert summary["baseline_run_id"] == "variant%3Afree_space"
    rows = {(r["run_id"], r["metric"]): r for r in _csv(tmp_path / "out" / "comparisons.csv")}
    metric = "station/Madrid/rf_usable_fraction"
    free, reference, declared = (
        float(rows[(f"variant%3A{name}", metric)]["value"])
        for name in ("free_space", "itu_reference", "itu_reference_hydrometeors")
    )
    assert free > reference > declared
    verify_manifest(tmp_path / "out")


def _hand_edit(spec: dict, patch: list) -> dict:
    """The loaded spec with extra patch operations, as a caller that skips load_sweep would."""
    spec = copy.deepcopy(spec)
    spec["variants"][0]["patch"] = patch
    return spec


def test_patches_cannot_move_the_window_bounds_when_a_window_is_set(tmp_path):
    window = {"start_offset_s": 0, "duration_s": 600}
    stop = {"op": "replace", "path": "/time/stop_utc", "value": "2026-08-30T08:24:00Z"}
    start = {"op": "replace", "path": "/time/start_utc", "value": "2026-08-30T05:00:00Z"}
    whole = {"op": "replace", "path": "/time", "value": _document()["time"]}
    for position, operation in enumerate((stop, start, whole)):
        spec = {
            "mode": "variants",
            "variants": [{"id": "a", "patch": [{"op": "remove", "path": "/network"}, operation]}],
            "window": window,
        }
        path = _write(tmp_path, {**spec, "replay": {"network": {"loads_bps": [1000]}}})
        with pytest.raises(
            ValueError,
            match=rf"^/variants/0/patch/1/path='{operation['path']}' cannot change "
            r"/time/start_utc or /time/stop_utc when a window is set",
        ):
            sweep.load_sweep(path)
    parameter = {"path": "/time/stop_utc", "values": ["2026-08-30T06:20:00Z"], "nominal": "x"}
    path = _write(tmp_path, {"mode": "oat", "parameters": [parameter], "window": window})
    with pytest.raises(ValueError, match=r"^/parameters/0/path='/time/stop_utc' cannot change"):
        sweep.load_sweep(path)
    parameter = {"path": "/time", "values": [{}]}
    path = _write(tmp_path, {"mode": "grid", "parameters": [parameter], "window": window})
    with pytest.raises(ValueError, match=r"^/parameters/0/path='/time' cannot change"):
        sweep.load_sweep(path)
    # Other time fields are fine, and without a window the bounds may be patched.
    ok = {"op": "replace", "path": "/time/step_s", "value": 5.0}
    spec = {"mode": "variants", "variants": [{"id": "a", "patch": [ok]}], "window": window}
    assert sweep.load_sweep(_write(tmp_path, spec))["variants"][0]["patch"] == [ok]
    spec = {"mode": "variants", "variants": [{"id": "a", "patch": [stop]}]}
    assert sweep.load_sweep(_write(tmp_path, spec))["window"] is None


def test_generated_runs_stay_inside_the_window_and_short_with_a_replay(tmp_path):
    """Defence after patching, for specs that did not come through load_sweep."""
    network = {"loads_bps": [1000]}
    spec = sweep.load_sweep(
        _write(
            tmp_path,
            {
                "mode": "variants",
                "variants": [{"id": "a", "patch": []}],
                "window": {"start_offset_s": 0, "duration_s": 600},
                "replay": {"network": network},
            },
        )
    )
    long_stop = {"op": "replace", "path": "/time/stop_utc", "value": "2026-08-30T08:24:00Z"}
    early_start = {"op": "replace", "path": "/time/start_utc", "value": "2026-08-30T06:00:00Z"}
    with pytest.raises(
        ValueError, match=r"^/variants/0: run variant%3Aa spans .*T08:24:00Z.*base window"
    ):
        sweep.expand_sweep(_hand_edit(spec, [long_stop]), _document())
    with pytest.raises(
        ValueError, match=r"^/variants/0: run variant%3Aa spans 2026-08-30T06:00:00Z.*outside"
    ):
        sweep.expand_sweep(_hand_edit(spec, [early_start]), _document())
    # A base window longer than 3600 s: only the replay rule applies when no window is set.
    base = _document()
    base["time"]["stop_utc"] = "2026-08-30T07:00:00Z"
    unwindowed = sweep.load_sweep(
        _write(
            tmp_path,
            {
                "mode": "variants",
                "variants": [{"id": "a", "patch": [long_stop]}],
                "replay": {"network": network},
            },
        )
    )
    with pytest.raises(
        ValueError, match=r"^/variants/0: run variant%3Aa spans 7800 s, more than the 3600 s"
    ):
        sweep.expand_sweep(unwindowed, base)
    # Without a replay or a window, a patched span is the caller's business.
    spec_plain = sweep.load_sweep(
        _write(tmp_path, {"mode": "variants", "variants": [{"id": "a", "patch": [long_stop]}]})
    )
    assert (
        sweep.expand_sweep(spec_plain, _document())[0].document["time"]["stop_utc"]
        == "2026-08-30T08:24:00Z"
    )


@pytest.mark.parametrize("value", [1e300, 10**400, 31 * 86400.0 + 1])
def test_huge_window_numbers_are_value_errors(tmp_path, value):
    for key, other in (("start_offset_s", "duration_s"), ("duration_s", "start_offset_s")):
        window = {key: value, other: 60}
        path = _write(tmp_path, {"mode": "variants", "variants": [{"id": "a", "patch": []}]})
        document = json.loads(path.read_text(encoding="utf-8"))
        document["window"] = window
        path.write_text(json.dumps(document), encoding="utf-8")
        with pytest.raises(ValueError, match=rf"^/window/{key}=.* must be a finite number"):
            sweep.load_sweep(path)
    # The same bound protects the other numeric fields.
    with pytest.raises(ValueError, match=r"^/replay/network/loads_bps/0=.* must be"):
        sweep._replay({"network": {"loads_bps": [10**400]}})


def test_spec_hash_and_bytes_come_from_the_single_read(tmp_path, monkeypatch):
    path = _eirp(tmp_path)
    original = path.read_bytes()
    spec = sweep.load_sweep(path)
    assert spec["source_bytes"] == original
    assert spec["spec_sha256"] == sweep.document_sha256(json.loads(original))
    assert not (
        {"source_bytes", "spec_sha256", "base_scenario_path"} & set(sweep._spec_document(spec))
    )
    # run_sweep reads the spec file once (load_sweep); changing it afterwards cannot matter.
    calls = []
    real = Path.read_text

    def spy(self, *args, **kwargs):
        calls.append(self)
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", spy)
    summary = sweep.run_sweep(path, tmp_path / "out")
    assert path not in calls
    assert summary["spec_sha256"] == spec["spec_sha256"]
