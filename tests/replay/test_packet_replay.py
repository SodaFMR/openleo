import csv
import json
import os
import subprocess
from dataclasses import asdict
from hashlib import sha256
from importlib.metadata import version
from pathlib import Path

import pytest
from replay_fakes import packet_source

import openleo.replay.packet_replay as packet_replay_module
from openleo.replay import (
    ReplayConfig,
    parse_packet_results,
    prepare_packet_replay,
    run_packet_replay,
)
from openleo.results import manifest_digest


def _document():
    return packet_source()


def test_prepare_clips_and_quantizes_a_selected_link_without_mutating_inputs():
    document = _document()
    config = ReplayConfig(
        station_name="Cartagena", norad_id=25544, start_offset_s=5.0, duration_s=15.0
    )
    original_document = document.copy()

    prepared = prepare_packet_replay(document, config)

    assert prepared["trace_rows"] == [
        {"time_ns": 0, "rate_bps": 10, "delay_ns": 1_000_000, "available": 1},
        {"time_ns": 5_000_000_000, "rate_bps": 0, "delay_ns": 0, "available": 0},
        {"time_ns": 15_000_000_000, "rate_bps": 0, "delay_ns": 2_000_000, "available": 0},
    ]
    assert prepared["metadata"]["window"] == {
        "start_utc": "2026-08-30T06:17:05Z",
        "stop_utc": "2026-08-30T06:17:20Z",
        "start_offset_s": 5.0,
        "duration_s": 15.0,
        "duration_ns": 15_000_000_000,
    }
    assert prepared["metadata"]["quantization"] == {
        "max_rate_floor_error_bps": 0.75,
        "max_delay_round_error_ns": pytest.approx(0.4),
    }
    assert prepared["metadata"]["traffic"] == {
        "interval_ns": 40_960_000,
        "effective_offered_load_bps": 100_000.0,
        "offered_packets_per_direction": 367,
        "total_offered_packets": 734,
    }
    assert document == original_document
    assert config == ReplayConfig(
        station_name="Cartagena", norad_id=25544, start_offset_s=5.0, duration_s=15.0
    )


@pytest.mark.parametrize(
    ("changes", "match"),
    [
        ({"station_name": "Missing"}, "station"),
        ({"norad_id": 99999}, "NORAD"),
        ({"start_offset_s": True}, "start_offset_s"),
        ({"start_offset_s": 10**400}, "start_offset_s"),
        ({"duration_s": 0.0000001}, "microsecond"),
        ({"duration_s": 3601.0}, "duration_s"),
        ({"offered_load_bps": float("nan")}, "offered_load_bps"),
        ({"offered_load_bps": 1e-320}, "offered_load_bps"),
        ({"offered_load_bps": 1e-12}, "offered_load_bps"),
        ({"offered_load_bps": 10**400}, "offered_load_bps"),
        ({"packet_size_bytes": 63}, "packet_size_bytes"),
        ({"queue_packets": 10001}, "queue_packets"),
        ({"seed": 2**31}, "seed"),
    ],
)
def test_prepare_rejects_invalid_config_and_selection(changes, match):
    values = {"station_name": "Cartagena", "norad_id": 25544, "duration_s": 1, **changes}
    with pytest.raises(ValueError, match=match):
        prepare_packet_replay(_document(), ReplayConfig(**values))


def test_prepare_rejects_window_outside_experiment():
    with pytest.raises(ValueError, match="results window"):
        prepare_packet_replay(
            _document(),
            ReplayConfig(
                station_name="Cartagena", norad_id=25544, start_offset_s=10, duration_s=11
            ),
        )
    with pytest.raises(ValueError):
        prepare_packet_replay(
            _document(),
            ReplayConfig(
                station_name="Cartagena", norad_id=25544, start_offset_s=1e308, duration_s=1
            ),
        )


def test_prepare_rejects_rate_delay_serialization_and_packet_budget_bounds():
    cases = []
    for field, value in (("rate_bps", 0.5), ("rate_bps", 1_000_000_000_001.0), ("delay_s", 1.1)):
        document = _document()
        rate, delay = document.links[0][(0, 0)]
        document.links[0][(0, 0)] = (value, delay) if field == "rate_bps" else (rate, value)
        cases.append((document, 512))
    serialization = _document()
    serialization.links[0][(0, 0)] = (1_000_000_000_000.0, 0.001)
    cases.append((serialization, 64))
    for document, packet_size in cases:
        with pytest.raises(ValueError):
            prepare_packet_replay(
                document,
                ReplayConfig(
                    station_name="Cartagena",
                    norad_id=25544,
                    duration_s=1,
                    packet_size_bytes=packet_size,
                ),
            )
    with pytest.raises(ValueError, match="200000"):
        prepare_packet_replay(
            _document(),
            ReplayConfig(
                station_name="Cartagena",
                norad_id=25544,
                duration_s=20,
                offered_load_bps=1_000_000_000,
                packet_size_bytes=64,
            ),
        )


PACKET_FIELDS = (
    "direction",
    "sequence",
    "offered_time_ns",
    "udp_tx_time_ns",
    "phy_tx_time_ns",
    "rx_time_ns",
    "status",
    "payload_bytes",
)


def _prepared_packets():
    config = ReplayConfig(
        station_name="Cartagena",
        norad_id=25544,
        duration_s=1,
        offered_load_bps=3200,
        packet_size_bytes=100,
    )
    return {
        "trace_rows": [
            {"time_ns": 0, "rate_bps": 1_000_000, "delay_ns": 100_000_000, "available": 1},
            {"time_ns": 500_000_000, "rate_bps": 0, "delay_ns": 0, "available": 0},
            {
                "time_ns": 750_000_000,
                "rate_bps": 1_000_000,
                "delay_ns": 100_000_000,
                "available": 1,
            },
            {
                "time_ns": 1_000_000_000,
                "rate_bps": 1_000_000,
                "delay_ns": 100_000_000,
                "available": 1,
            },
        ],
        "metadata": {
            "configuration": asdict(config),
            "window": {"duration_ns": 1_000_000_000},
            "traffic": {
                "interval_ns": 250_000_000,
                "offered_packets_per_direction": 4,
                "total_offered_packets": 8,
            },
        },
    }


def _packet_rows():
    return [
        [0, 0, 0, 0, 0, 101_040_000, "received", 100],
        [1, 0, 0, 0, 0, 101_040_000, "received", 100],
        [0, 1, 250_000_000, 250_000_000, "", "", "queue_drop", 100],
        [
            1,
            1,
            250_000_000,
            250_000_000,
            450_000_000,
            551_040_000,
            "rx_outage_drop",
            100,
        ],
        [0, 2, 500_000_000, "", "", "", "outage_suppressed", 100],
        [1, 2, 500_000_000, "", "", "", "outage_suppressed", 100],
        [
            0,
            3,
            750_000_000,
            750_000_000,
            950_000_000,
            1_051_040_000,
            "received_after_window",
            100,
        ],
        [
            1,
            3,
            750_000_000,
            750_000_000,
            "",
            "",
            "end_of_window_drop",
            100,
        ],
    ]


def _write_packets(path, rows=None, fields=PACKET_FIELDS):
    with path.open("w", encoding="ascii", newline="") as file:
        writer = csv.writer(file, lineterminator="\n")
        writer.writerow(fields)
        writer.writerows(_packet_rows() if rows is None else rows)


def test_parse_packet_results_computes_hand_checked_direction_summaries(tmp_path):
    path = tmp_path / "packets.csv"
    _write_packets(path)

    records, summaries = parse_packet_results(path, _prepared_packets())

    assert len(records) == 8
    assert records[0] == {
        "direction": 0,
        "sequence": 0,
        "offered_time_ns": 0,
        "udp_tx_time_ns": 0,
        "phy_tx_time_ns": 0,
        "rx_time_ns": 101_040_000,
        "status": "received",
        "payload_bytes": 100,
    }
    assert summaries == [
        {
            "direction": 0,
            "offered_packets": 4,
            "admitted_packets": 3,
            "received_packets": 2,
            "received_in_window_packets": 1,
            "received_after_window_packets": 1,
            "outage_suppressed_packets": 1,
            "queue_drop_packets": 1,
            "outage_queue_drop_packets": 0,
            "rx_outage_drop_packets": 0,
            "end_of_window_drop_packets": 0,
            "send_error_packets": 0,
            "unresolved_packets": 0,
            "goodput_bps": 800.0,
            "mean_delay_s": pytest.approx(0.10104),
            "max_delay_s": pytest.approx(0.10104),
        },
        {
            "direction": 1,
            "offered_packets": 4,
            "admitted_packets": 3,
            "received_packets": 1,
            "received_in_window_packets": 1,
            "received_after_window_packets": 0,
            "outage_suppressed_packets": 1,
            "queue_drop_packets": 0,
            "outage_queue_drop_packets": 0,
            "rx_outage_drop_packets": 1,
            "end_of_window_drop_packets": 1,
            "send_error_packets": 0,
            "unresolved_packets": 0,
            "goodput_bps": 800.0,
            "mean_delay_s": pytest.approx(0.10104),
            "max_delay_s": pytest.approx(0.10104),
        },
    ]


@pytest.mark.parametrize(
    "change",
    [
        "duplicate",
        "missing",
        "reordered",
        "offered_time",
        "payload",
        "required_times",
        "received_label",
        "phy_after_end",
        "source_state",
        "outage_overlap",
        "physical_timing",
    ],
)
def test_parse_packet_results_rejects_inconsistent_records(tmp_path, change):
    rows = _packet_rows()
    if change == "duplicate":
        rows[1][0] = 0
    elif change == "missing":
        rows.pop()
    elif change == "reordered":
        rows[0], rows[1] = rows[1], rows[0]
    elif change == "offered_time":
        rows[0][2] = 1
    elif change == "payload":
        rows[0][7] = 99
    elif change == "required_times":
        rows[4][3] = 500_000_000
    elif change == "received_label":
        rows[6][6] = "received"
    elif change == "phy_after_end":
        rows[6][4] = 1_000_000_000
        rows[6][5] = 1_101_040_000
    elif change == "source_state":
        rows[4][6] = "send_error"
        rows[4][3] = 500_000_000
    elif change == "outage_overlap":
        rows[3][6] = "received"
    elif change == "physical_timing":
        rows[0][5] += 2
    path = tmp_path / "packets.csv"
    _write_packets(path, rows)
    with pytest.raises(ValueError):
        parse_packet_results(path, _prepared_packets())


def test_parse_packet_results_rejects_bad_header_and_numeric_junk(tmp_path):
    path = tmp_path / "packets.csv"
    _write_packets(path, fields=PACKET_FIELDS[:-1])
    with pytest.raises(ValueError, match="header"):
        parse_packet_results(path, _prepared_packets())
    rows = _packet_rows()
    rows[0][2] = "0junk"
    _write_packets(path, rows)
    with pytest.raises(ValueError, match="integer"):
        parse_packet_results(path, _prepared_packets())


def test_parse_packet_results_revalidates_prepared_trace_types(tmp_path):
    path = tmp_path / "packets.csv"
    _write_packets(path)
    prepared = _prepared_packets()
    prepared["trace_rows"][0]["available"] = True
    with pytest.raises(ValueError, match="prepared"):
        parse_packet_results(path, prepared)


def test_parse_packet_results_requires_future_outage_for_outage_queue_drop(tmp_path):
    path = tmp_path / "packets.csv"
    rows = _packet_rows()
    rows[2][6] = "outage_queue_drop"
    rows[3][4:7] = ["", "", "queue_drop"]
    rows[4][3:7] = [500_000_000, "", "", "queue_drop"]
    rows[5][3:7] = [500_000_000, "", "", "queue_drop"]
    _write_packets(path, rows)
    prepared = _prepared_packets()
    prepared["trace_rows"][1].update(rate_bps=1_000_000, delay_ns=100_000_000, available=1)
    with pytest.raises(ValueError, match="outage onset"):
        parse_packet_results(path, prepared)


def test_parse_packet_results_accepts_queue_drop_at_strictly_future_outage_onset(tmp_path):
    path = tmp_path / "packets.csv"
    rows = _packet_rows()
    rows[2][6] = "outage_queue_drop"
    _write_packets(path, rows)
    _, summaries = parse_packet_results(path, _prepared_packets())
    assert summaries[0]["queue_drop_packets"] == 0
    assert summaries[0]["outage_queue_drop_packets"] == 1


def _run_config(**changes):
    values = {
        "station_name": "Madrid",
        "norad_id": 42960,
        "duration_s": 60,
        **changes,
    }
    return ReplayConfig(**values)


@pytest.fixture
def results(global_on):
    return global_on


def test_run_config_selects_a_visible_switching_link(results):
    from openleo.replay import ReplaySource

    source = ReplaySource.from_results(results)
    assert source.norad_ids[25] == 42960
    rows = prepare_packet_replay(source, _run_config())["trace_rows"]
    assert {row["rate_bps"] for row in rows} == {988858, 490243}


def test_run_rejects_missing_backend_without_creating_output(tmp_path, results):
    output = tmp_path / "output"
    with pytest.raises(ValueError, match="backend executable"):
        run_packet_replay(results, _run_config(), tmp_path / "missing-backend", output)
    assert not output.exists()


def test_run_preserves_existing_nonempty_output_before_backend_execution(tmp_path, results):
    output = tmp_path / "output"
    output.mkdir()
    sentinel = output / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")
    with pytest.raises(ValueError, match="non-empty"):
        run_packet_replay(results, _run_config(), tmp_path / "missing-backend", output)
    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_run_rejects_wrong_backend_handshake_without_creating_output(
    tmp_path, monkeypatch, results
):
    backend = tmp_path / "backend"
    backend.write_bytes(b"non-executable protocol fixture")

    def wrong_version(command, **kwargs):
        assert command == [str(backend.resolve()), "--PrintVersion"]
        return subprocess.CompletedProcess(command, 0, b"wrong-version\n", b"")

    monkeypatch.setattr("openleo.replay.packet_replay.subprocess.run", wrong_version)
    output = tmp_path / "output"
    with pytest.raises(ValueError, match="version handshake"):
        run_packet_replay(results, _run_config(), backend, output)
    assert not output.exists()


def test_run_reports_nonzero_backend_failure_without_publishing_output(
    tmp_path, monkeypatch, results
):
    backend = tmp_path / "backend"
    backend.write_bytes(b"non-executable protocol fixture")

    def failing_backend(command, **kwargs):
        assert command[0] == str(backend.resolve())
        if command[1:] == ["--PrintVersion"]:
            return subprocess.CompletedProcess(command, 0, b"openleo-ns3-replay/1 ns-3.48\n", b"")
        assert [argument.split("=", 1)[0] for argument in command[1:]] == [
            "--trace",
            "--output",
            "--intervalNs",
            "--packetSize",
            "--queuePackets",
            "--seed",
        ]
        return subprocess.CompletedProcess(command, 7, b"", b"backend fixture failed\n")

    monkeypatch.setattr("openleo.replay.packet_replay.subprocess.run", failing_backend)
    output = tmp_path / "output"
    with pytest.raises(ValueError, match="status 7: backend fixture failed"):
        run_packet_replay(results, _run_config(), backend, output)
    assert not output.exists()


def _fake_backend(tmp_path, monkeypatch):
    """A protocol-exact stand-in: every packet is suppressed or dropped at the window end."""
    backend = tmp_path / "backend"
    backend.write_bytes(b"protocol fixture")

    def native(command, **kwargs):
        if command[1:] == ["--PrintVersion"]:
            return subprocess.CompletedProcess(command, 0, b"openleo-ns3-replay/1 ns-3.48\n", b"")
        arguments = dict(argument[2:].split("=", 1) for argument in command[1:])
        trace = list(csv.DictReader(Path(arguments["trace"]).read_text("ascii").splitlines()))
        interval = int(arguments["intervalNs"])
        duration = int(trace[-1]["time_ns"])
        count = (duration + interval - 1) // interval
        rows = []
        for sequence in range(count):
            offered = sequence * interval
            state = [row for row in trace if int(row["time_ns"]) <= offered][-1]
            for direction in (0, 1):
                if state["available"] == "1":
                    rows.append(
                        [direction, sequence, offered, offered, "", "", "end_of_window_drop"]
                    )
                else:
                    rows.append([direction, sequence, offered, "", "", "", "outage_suppressed"])
        with Path(arguments["output"]).open("w", encoding="ascii", newline="") as file:
            writer = csv.writer(file, lineterminator="\n")
            writer.writerow(PACKET_FIELDS)
            writer.writerows([*row, arguments["packetSize"]] for row in rows)
        return subprocess.CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr("openleo.replay.packet_replay.subprocess.run", native)
    return backend


def test_run_publishes_hashed_evidence_bound_to_the_results(tmp_path, monkeypatch, results):
    backend = _fake_backend(tmp_path, monkeypatch)
    output = tmp_path / "output"
    summary = run_packet_replay(results, _run_config(duration_s=5), backend, output)
    assert sorted(path.name for path in output.iterdir()) == [
        "link-trace.csv",
        "manifest.json",
        "packet-summary.json",
        "packets.csv",
    ]
    assert summary["kind"] == "openleo.packet-replay"
    assert summary["source"]["results"]["manifest_sha256"] == manifest_digest(results)
    assert (
        summary["source"]["results"]["results_json_sha256"]
        == sha256((results / "results.json").read_bytes()).hexdigest()
    )
    recorded = json.loads((results / "results.json").read_text(encoding="utf-8"))
    assert summary["source"]["scenario"] == {
        "source_sha256": recorded["scenario"]["source_sha256"],
        "normalised_sha256": recorded["scenario"]["normalised_sha256"],
    }
    assert summary["source"]["catalogs"] == [
        {"archived_path": c["archived_path"], "sha256": c["sha256"]}
        for c in recorded["provenance"]["catalogs"]
    ]
    aggregate = summary["aggregate"]
    assert aggregate["offered_packets"] == sum(d["offered_packets"] for d in summary["directions"])
    assert aggregate["end_of_window_drop_packets"] == aggregate["offered_packets"]
    assert aggregate["unresolved_packets"] == aggregate["send_error_packets"] == 0
    assert str(tmp_path) not in json.dumps(summary)
    assert any("held" in text for text in summary["limitations"])
    stored = json.loads((output / "packet-summary.json").read_text(encoding="utf-8"))
    assert stored == json.loads(json.dumps(summary))


def test_run_refuses_a_results_directory_changed_during_preparation(
    tmp_path, monkeypatch, global_on
):
    import shutil

    from openleo.results import write_manifest

    results = tmp_path / "run"
    shutil.copytree(global_on, results)
    backend = _fake_backend(tmp_path, monkeypatch)
    real = packet_replay_module.prepare_packet_replay

    def tamper(source, config):
        prepared = real(source, config)
        (results / "tables" / "passes.csv").write_text("changed", encoding="utf-8")
        write_manifest(results)
        return prepared

    monkeypatch.setattr(packet_replay_module, "prepare_packet_replay", tamper)
    with pytest.raises(ValueError, match="changed"):
        run_packet_replay(results, _run_config(duration_s=5), backend, tmp_path / "output")
    assert not (tmp_path / "output").exists()


def test_real_backend_results_to_validated_atomic_evidence(tmp_path, monkeypatch, results):
    backend = os.environ.get("OPENLEO_NS3_REPLAY")
    if not backend:
        pytest.skip("set OPENLEO_NS3_REPLAY to the actual pinned ns-3 backend")
    output = tmp_path / "output"
    output.mkdir()
    real_replace = os.replace

    def windows_replace(source, target):
        if Path(target).exists():
            raise FileExistsError(target)
        real_replace(source, target)

    monkeypatch.setattr("openleo.replay.packet_replay.os.replace", windows_replace)

    summary = run_packet_replay(results, _run_config(), backend, output)

    assert sorted(path.name for path in output.iterdir()) == [
        "link-trace.csv",
        "manifest.json",
        "packet-summary.json",
        "packets.csv",
    ]
    for direction in summary["directions"]:
        assert direction["offered_packets"] == 1465
        assert direction["outage_suppressed_packets"] == 0
        assert direction["received_in_window_packets"] > 1400
        assert direction["unresolved_packets"] == direction["send_error_packets"] == 0
        # Slant delay ~2.7-4 ms plus 4.4-8.9 ms serialization of 542 wire bytes.
        assert 0.006 < direction["mean_delay_s"] < 0.014
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["files"] == {
        name: sha256((output / name).read_bytes()).hexdigest()
        for name in ("link-trace.csv", "packets.csv", "packet-summary.json")
    }
    assert summary["source"]["results"]["manifest_sha256"] == manifest_digest(results)
    assert summary["backend"]["binary_sha256"] == sha256(Path(backend).read_bytes()).hexdigest()
    assert summary["backend"]["executable"] == Path(backend).name
    assert summary["software"] == {"openleo-link": version("openleo-link")}
    assert summary["aggregate"]["unresolved_packets"] == 0


def test_real_backend_refuses_concurrently_populated_output(tmp_path, monkeypatch, results):
    backend = os.environ.get("OPENLEO_NS3_REPLAY")
    if not backend:
        pytest.skip("set OPENLEO_NS3_REPLAY to the actual pinned ns-3 backend")
    output = tmp_path / "output"
    output.mkdir()
    real_check = packet_replay_module._check_output_target
    calls = 0

    def populate_after_second_check(path):
        nonlocal calls
        real_check(path)
        calls += 1
        if calls == 2:
            (path / "concurrent.txt").write_text("preserve", encoding="utf-8")

    monkeypatch.setattr(
        "openleo.replay.packet_replay._check_output_target", populate_after_second_check
    )
    with pytest.raises(ValueError, match="produce packet replay output"):
        run_packet_replay(results, _run_config(duration_s=1), backend, output)
    assert (output / "concurrent.txt").read_text(encoding="utf-8") == "preserve"
