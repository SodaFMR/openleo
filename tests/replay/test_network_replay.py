import csv
import json
import os
import subprocess
from dataclasses import replace
from itertools import pairwise
from pathlib import Path

import pytest
from replay_fakes import multihop_source, network_source

from openleo.replay import (
    EDGE_FIELDS,
    HOP_FIELDS,
    NetworkReplayConfig,
    ReplaySource,
    parse_network_results,
    prepare_network_replay,
    run_network_replay,
)
from openleo.replay import NETWORK_PACKET_FIELDS as PACKET_FIELDS
from openleo.replay.network_replay import _execute_network_replay
from openleo.results import manifest_digest


def document():
    return network_source()


def config(**kwargs):
    return NetworkReplayConfig(
        duration_s=0.1, offered_load_bps=77_600, packet_size_bytes=970, **kwargs
    )


def prepared():
    return prepare_network_replay(document(), config())


def write_results(directory, packets=None, hops=None):
    if packets is None:
        packets = [[direction, 0, 0, 0, 0, 20_000_000, "received", 970, 2] for direction in (0, 1)]
    if hops is None:
        hops = [
            [0, 0, 0, 0, 0, 1, 0, 0, 10_000_000, 1_000_000, 2_000_000, "received"],
            [0, 0, 0, 1, 1, 0, 2, 10_000_000, 20_000_000, 1_000_000, 2_000_000, "received"],
            [1, 0, 0, 0, 1, 2, 0, 0, 10_000_000, 1_000_000, 2_000_000, "received"],
            [1, 0, 0, 1, 0, 0, 1, 10_000_000, 20_000_000, 1_000_000, 2_000_000, "received"],
        ]
    for name, fields, rows in (
        ("packets.csv", PACKET_FIELDS, packets),
        ("hops.csv", HOP_FIELDS, hops),
    ):
        with (directory / name).open("w", encoding="ascii", newline="") as file:
            writer = csv.writer(file)
            writer.writerow(fields)
            writer.writerows(rows)


def test_prepare_left_hold_quantization_and_selected_edges_without_mutation():
    source = document()
    original = source.copy()
    result = prepare_network_replay(source, replace(config(), start_offset_s=5, duration_s=15))
    assert source == original
    assert result["routes"] == [
        {"time_ns": 0, "state": "ready", "path": "1;0;2"},
        {"time_ns": 5_000_000_000, "state": "ready", "path": "1;0;2"},
        {"time_ns": 15_000_000_000, "state": "stop", "path": ""},
    ]
    assert len(result["edges"]) == 6
    assert tuple(result["edges"][0]) == EDGE_FIELDS
    assert result["edges"][0] == {
        "time_ns": 0,
        "edge_id": 0,
        "node_a": 0,
        "node_b": 1,
        "rate_bps": 1_000_000,
        "delay_ns": 2_000_000,
        "available": 1,
    }
    assert result["metadata"]["quantization"]["max_rate_floor_error_bps"] == 0.75


def test_acquisition_adds_boundary_and_retains_ready_time_across_frames():
    result = prepare_network_replay(
        document(), replace(config(), duration_s=20, acquisition_delay_s=12)
    )
    assert [(row["time_ns"], row["state"]) for row in result["routes"]] == [
        (0, "acquiring"),
        (10_000_000_000, "acquiring"),
        (12_000_000_000, "ready"),
        (20_000_000_000, "stop"),
    ]
    assert all(row["available"] == 0 for row in result["edges"][:4])
    assert all(row["available"] == 1 for row in result["edges"][4:6])


def test_fixed_capacity_uses_declared_baseline():
    source = document()
    source.links[0] = {key: (0.0, delay) for key, (_, delay) in source.links[0].items()}
    source.routes[0]["minimum_delay"] = None
    source.routes[0]["maximum_rate"] = None
    assert prepare_network_replay(source, config())["routes"][0]["state"] == "disconnected"
    fixed = prepare_network_replay(source, replace(config(), routing_model="fixed_capacity"))
    assert fixed["routes"][0]["state"] == "ready"
    assert fixed["edges"][0]["rate_bps"] == 1_000_000


@pytest.mark.parametrize(
    "kwargs",
    [
        {"routing_model": "unknown"},
        {"pair_index": 1},
        {"pair_index": True},
        {"flows_per_direction": True},
        {"flows_per_direction": 5},
        {"acquisition_delay_s": 61},
        {"duration_s": 0},
        {"start_offset_s": 0.0000001},
        {"offered_load_bps": 1_000_000_001},
        {"queue_packets": 0},
        {"seed": 0},
        {"packet_size_bytes": 63},
    ],
)
def test_invalid_config_is_rejected(kwargs):
    with pytest.raises(ValueError):
        prepare_network_replay(document(), replace(config(), **kwargs))


def test_hand_calculated_two_hop_latency_and_accounting(tmp_path):
    write_results(tmp_path)
    result = parse_network_results(tmp_path, prepared())
    assert result["aggregate"]["offered_packets"] == 2
    assert result["aggregate"]["received_packets"] == 2
    assert result["aggregate"]["goodput_bps"] == 155_200
    assert result["aggregate"]["mean_delay_s"] == 0.02
    assert len(result["flow_metrics"]) == 2


@pytest.mark.parametrize(
    "field,value",
    [
        ("edge_id", 99),
        ("phy_rx_time_ns", 9_000_000),
        ("rate_bps", 999_999),
        ("tx_node", 2),
        ("hop_index", 1),
        ("status", "invented"),
    ],
)
def test_inconsistent_hop_csv_is_rejected(tmp_path, field, value):
    write_results(tmp_path)
    with (tmp_path / "hops.csv").open(newline="") as file:
        rows = list(csv.reader(file))
    rows[1][HOP_FIELDS.index(field)] = value
    with (tmp_path / "hops.csv").open("w", newline="") as file:
        csv.writer(file).writerows(rows)
    with pytest.raises(ValueError):
        parse_network_results(tmp_path, prepared())


def test_missing_packets_and_invented_receive_time_are_rejected(tmp_path):
    write_results(tmp_path, [[0, 0, 0, 0, 0, 20_000_000, "received", 970, 2]])
    with pytest.raises(ValueError):
        parse_network_results(tmp_path, prepared())
    write_results(tmp_path, [[d, 0, 0, 0, 0, 21_000_000, "received", 970, 2] for d in (0, 1)])
    with pytest.raises(ValueError):
        parse_network_results(tmp_path, prepared())


def execute(source, settings, backend, output):
    """Replay a hand-built source through the same backend execution as ``run_network_replay``."""
    return _execute_network_replay(
        prepare_network_replay(source, settings),
        {"synthetic": "hand-built test topology"},
        backend,
        output,
        ["test topology"],
    )


def test_output_preserved_before_backend_execution(tmp_path, global_on):
    output = tmp_path / "output"
    output.mkdir()
    (output / "sentinel").write_text("keep")
    with pytest.raises(ValueError, match="non-empty"):
        run_network_replay(global_on, config(), tmp_path / "missing", output)
    assert (output / "sentinel").read_text() == "keep"


def test_run_rejects_a_model_or_pair_the_results_lack(tmp_path, global_on):
    from openleo.replay import ReplaySource

    source = ReplaySource.from_results(global_on)
    with pytest.raises(ValueError, match="pair_index"):
        prepare_network_replay(source, replace(config(), pair_index=1))
    trimmed = network_source()
    trimmed.route_models = ("minimum_delay",)
    with pytest.raises(ValueError, match="route_models"):
        prepare_network_replay(trimmed, replace(config(), routing_model="maximum_rate"))


def test_actual_backend(tmp_path):
    backend = os.environ.get("OPENLEO_NS3_NETWORK")
    if not backend:
        pytest.skip("set OPENLEO_NS3_NETWORK to the actual pinned ns-3 network backend")
    result = execute(document(), config(), backend, tmp_path / "output")
    assert result["aggregate"]["mean_delay_s"] == pytest.approx(0.02)
    assert result["aggregate"]["received_packets"] == 2


def test_actual_backend_on_v2_results(tmp_path, global_on):
    backend = os.environ.get("OPENLEO_NS3_NETWORK")
    if not backend:
        pytest.skip("set OPENLEO_NS3_NETWORK to the actual pinned ns-3 network backend")
    settings = NetworkReplayConfig(duration_s=60, offered_load_bps=20_000, flows_per_direction=2)
    output = tmp_path / "output"
    summary = run_network_replay(global_on, settings, backend, output)
    aggregate = summary["aggregate"]
    assert aggregate["offered_packets"] == summary["traffic"]["total_offered_packets"]
    # The route 80-25-26-81 holds until 52.53 s, then the pair is disconnected.
    assert aggregate["received_in_window_packets"] > 0.8 * aggregate["offered_packets"]
    assert aggregate["no_route_packets"] > 0
    assert aggregate["unresolved_packets"] == aggregate["send_error_packets"] == 0
    # Route 80-25-26-81 has a ~20.9 ms path delay; three hops add serialization.
    assert 0.0209 < aggregate["mean_delay_s"] < 0.04
    assert summary["source"]["results"]["manifest_sha256"] == manifest_digest(global_on)
    assert summary["selection"]["pair_index"] == 0
    assert sorted(path.name for path in output.iterdir()) == [
        "edges.csv",
        "hops.csv",
        "manifest.json",
        "network-summary.json",
        "packets.csv",
        "routes.csv",
    ]


def test_supported_high_rate_is_not_bounded_by_measurement_time(tmp_path):
    source = document()
    source.links = [
        {key: (1_000_000_000.0, delay) for key, (_, delay) in frame.items()}
        for frame in source.links
    ]
    state = prepare_network_replay(source, config())
    packets = [[d, 0, 0, 0, 0, 4_016_000, "received", 970, 2] for d in (0, 1)]
    hops = [
        [
            d,
            0,
            0,
            i,
            edge,
            a,
            b,
            i * 2_008_000,
            (i + 1) * 2_008_000,
            1_000_000_000,
            2_000_000,
            "received",
        ]
        for d, path, edge_ids in ((0, (1, 0, 2), (0, 1)), (1, (2, 0, 1), (1, 0)))
        for i, ((a, b), edge) in enumerate(zip(pairwise(path), edge_ids))
    ]
    write_results(tmp_path, packets, hops)
    assert parse_network_results(tmp_path, state)["aggregate"]["mean_delay_s"] == 0.004016


def test_failed_route_drop_requires_actual_intermediary_route_loss(tmp_path):
    write_results(tmp_path, [[d, 0, 0, 0, 0, "", "route_drop", 970, 0] for d in (0, 1)], [])
    with pytest.raises(ValueError, match="route"):
        parse_network_results(tmp_path, prepared())


def test_stop_frame_cannot_claim_active_edges(tmp_path):
    state = prepared()
    state["edges"][-1].update(rate_bps=1_000_000, available=1)
    write_results(tmp_path)
    with pytest.raises(ValueError, match="stop"):
        parse_network_results(tmp_path, state)


def test_prepared_ready_route_rejects_intermediate_ground_nodes(tmp_path):
    state = prepared()
    state["metadata"]["selection"]["node_count"] = 4
    state["routes"][0]["path"] = "1;0;3;2"
    state["edges"] = [
        dict(zip(EDGE_FIELDS, (t, i, a, b, 1_000_000 if t == 0 else 0, 2_000_000, int(t == 0))))
        for t in (0, 100_000_000)
        for i, (a, b) in enumerate(((0, 1), (0, 3), (2, 3)))
    ]
    write_results(tmp_path)
    with pytest.raises(ValueError, match="ground"):
        parse_network_results(tmp_path, state)


def test_atomic_wrapper_generates_hashed_evidence_without_private_paths(tmp_path, monkeypatch):
    backend = tmp_path / "trusted-backend"
    backend.write_bytes(b"portable backend test fixture")

    def native(command, **kwargs):
        if command[1:] == ["--PrintVersion"]:
            return subprocess.CompletedProcess(command, 0, b"openleo-ns3-network/1 ns-3.48\n", b"")
        destination = Path(
            next(arg.split("=", 1)[1] for arg in command if arg.startswith("--output="))
        )
        write_results(destination)
        return subprocess.CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr("openleo.replay.network_replay.subprocess.run", native)
    output = tmp_path / "output"
    output.mkdir()
    summary = execute(document(), config(), backend, output)
    assert sorted(path.name for path in output.iterdir()) == [
        "edges.csv",
        "hops.csv",
        "manifest.json",
        "network-summary.json",
        "packets.csv",
        "routes.csv",
    ]
    assert summary["kind"] == "openleo.network-replay"
    assert str(tmp_path) not in json.dumps(summary)
    assert json.loads((output / "manifest.json").read_text())["files"].keys() == {
        "edges.csv",
        "hops.csv",
        "network-summary.json",
        "packets.csv",
        "routes.csv",
    }
    for name in ("manifest.json", "network-summary.json"):  # LF on every OS, as hashed
        payload = (output / name).read_bytes()
        assert payload.endswith(b"}\n") and b"\r" not in payload, name


def test_run_publishes_v2_evidence_for_a_disconnected_window(tmp_path, monkeypatch, global_on):
    source = ReplaySource.from_results(global_on)
    assert source.route(0, "minimum_delay", 500.0) is None
    settings = replace(config(), start_offset_s=497, duration_s=10, offered_load_bps=7760)
    backend = tmp_path / "trusted-backend"
    backend.write_bytes(b"portable backend test fixture")

    def native(command, **kwargs):
        if command[1:] == ["--PrintVersion"]:
            return subprocess.CompletedProcess(command, 0, b"openleo-ns3-network/1 ns-3.48\n", b"")
        destination = Path(
            next(arg.split("=", 1)[1] for arg in command if arg.startswith("--output="))
        )
        interval = int(next(a.split("=", 1)[1] for a in command if a.startswith("--intervalNs=")))
        write_results(
            destination,
            [
                [d, 0, n, n * interval, "", "", "no_route", 970, 0]
                for n in range((10_000_000_000 + interval - 1) // interval)
                for d in (0, 1)
            ],
            [],
        )
        return subprocess.CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr("openleo.replay.network_replay.subprocess.run", native)
    summary = run_network_replay(global_on, settings, backend, tmp_path / "output")
    assert summary["aggregate"]["no_route_packets"] == summary["aggregate"]["offered_packets"]
    assert summary["source"]["results"]["manifest_sha256"] == manifest_digest(global_on)
    assert summary["window"]["start_utc"] == "2026-09-10T12:08:17Z"
    assert any("breakpoint" in text for text in summary["limitations"])


def test_queued_hop_retains_route_selected_at_previous_receive_time(tmp_path):
    state = prepare_network_replay(
        document(),
        replace(config(), duration_s=0.02, offered_load_bps=970_000, flows_per_direction=2),
    )
    state["metadata"]["selection"] = {
        "source_node": 4,
        "target_node": 5,
        "node_count": 6,
        "source_station_index": 0,
        "target_station_index": 1,
    }
    state["routes"] = [
        {"time_ns": 0, "state": "ready", "path": "4;0;1;3;5"},
        {"time_ns": 9_000_000, "state": "ready", "path": "4;0;2;3;5"},
        {"time_ns": 20_000_000, "state": "stop", "path": ""},
    ]
    state["edges"] = [
        dict(
            zip(
                EDGE_FIELDS,
                (
                    time,
                    edge,
                    a,
                    b,
                    (2_000_000 if edge in (2, 5) else 1_000_000) if time < 20_000_000 else 0,
                    0,
                    int(time < 20_000_000),
                ),
            )
        )
        for time in (0, 9_000_000, 20_000_000)
        for edge, (a, b) in enumerate(((0, 1), (0, 2), (0, 4), (1, 3), (2, 3), (3, 5)))
    ]
    packets = [
        [direction, flow, seq, time, time, "", "send_error", 970, 0]
        for time, flow, seq in (
            (0, 0, 0),
            (4_000_000, 1, 0),
            (8_000_000, 0, 1),
            (12_000_000, 1, 1),
            (16_000_000, 0, 2),
        )
        for direction in (0, 1)
    ]
    packets[0][6:9] = ["route_drop", 970, 2]
    packets[2][6:9] = ["end_of_window_drop", 970, 2]
    write_results(
        tmp_path,
        packets,
        [
            [0, 0, 0, 0, 2, 4, 0, 0, 4_000_000, 2_000_000, 0, "received"],
            [0, 0, 0, 1, 0, 0, 1, 4_000_000, 12_000_000, 1_000_000, 0, "received"],
            [0, 1, 0, 0, 2, 4, 0, 4_000_000, 8_000_000, 2_000_000, 0, "received"],
            [0, 1, 0, 1, 0, 0, 1, 12_000_000, 20_000_000, 1_000_000, 0, "received"],
        ],
    )
    result = parse_network_results(tmp_path, state)
    assert result["aggregate"]["route_drop_packets"] == 1


def multihop_document(second_path):
    return multihop_source(second_path)


def test_internal_route_switch_keeps_endpoint_acquisition_and_only_selected_union():
    result = prepare_network_replay(
        multihop_document([4, 0, 2, 3, 5]), replace(config(), duration_s=20, acquisition_delay_s=12)
    )
    assert [(r["time_ns"], r["state"]) for r in result["routes"]] == [
        (0, "acquiring"),
        (10_000_000_000, "acquiring"),
        (12_000_000_000, "ready"),
        (20_000_000_000, "stop"),
    ]
    assert result["routes"][2]["path"] == "4;0;2;3;5"
    pairs = {(r["node_a"], r["node_b"]) for r in result["edges"]}
    assert pairs == {(0, 1), (0, 2), (0, 4), (1, 3), (2, 3), (3, 5)}


def test_endpoint_change_reacquires_and_gates_only_selected_ground_edges():
    result = prepare_network_replay(
        multihop_document([4, 1, 3, 5]), replace(config(), duration_s=20, acquisition_delay_s=2)
    )
    assert [(r["time_ns"], r["state"]) for r in result["routes"]] == [
        (0, "acquiring"),
        (2_000_000_000, "ready"),
        (10_000_000_000, "acquiring"),
        (12_000_000_000, "ready"),
        (20_000_000_000, "stop"),
    ]
    at_ready = {
        (r["node_a"], r["node_b"])
        for r in result["edges"]
        if r["time_ns"] == 12_000_000_000 and r["available"] and r["node_b"] >= 4
    }
    assert at_ready == {(1, 4), (3, 5)}


def test_loss_resets_pending_acquisition():
    source = document()
    source.offsets.insert(1, 5.0)
    source.links.insert(1, dict(source.links[0]))
    source.routes.insert(1, {**source.routes[0], "minimum_delay": None})
    source.isl.insert(1, {})
    result = prepare_network_replay(source, replace(config(), duration_s=20, acquisition_delay_s=8))
    assert [(r["time_ns"], r["state"]) for r in result["routes"]] == [
        (0, "acquiring"),
        (5_000_000_000, "disconnected"),
        (10_000_000_000, "acquiring"),
        (18_000_000_000, "ready"),
        (20_000_000_000, "stop"),
    ]


def test_empty_disconnected_topology_and_phase_aware_offers(tmp_path):
    source = document()
    for frame in source.routes:
        frame["minimum_delay"] = None
    state = prepare_network_replay(
        source, replace(config(), duration_s=0.06, flows_per_direction=2)
    )
    assert state["edges"] == []
    assert state["metadata"]["traffic"]["offered_packets_per_flow"] == [1, 1]
    write_results(
        tmp_path,
        [
            [d, f, 0, time, "", "", "no_route", 970, 0]
            for f, time in ((0, 0), (1, 50_000_000))
            for d in (0, 1)
        ],
        [],
    )
    assert parse_network_results(tmp_path, state)["aggregate"]["no_route_packets"] == 4


def test_huge_offset_is_rejected_as_a_window_validation_error():
    with pytest.raises(ValueError, match="window"):
        prepare_network_replay(document(), replace(config(), start_offset_s=1e20))


def test_queued_packet_cannot_survive_an_edge_outage_and_resume_after_recovery(tmp_path):
    state = prepared()
    state["routes"] = [
        {"time_ns": t, "state": status, "path": path}
        for t, status, path in (
            (0, "ready", "1;0;2"),
            (1_000_000, "disconnected", ""),
            (3_000_000, "ready", "1;0;2"),
            (100_000_000, "stop", ""),
        )
    ]
    state["edges"] = [
        dict(
            zip(EDGE_FIELDS, (time, edge, 0, edge + 1, 1_000_000 if up else 0, 2_000_000, int(up)))
        )
        for time, up in ((0, True), (1_000_000, False), (3_000_000, True), (100_000_000, False))
        for edge in (0, 1)
    ]
    write_results(
        tmp_path,
        [
            [0, 0, 0, 0, 0, 24_000_000, "received", 970, 2],
            [1, 0, 0, 0, 0, "", "send_error", 970, 0],
        ],
        [
            [0, 0, 0, 0, 0, 1, 0, 4_000_000, 14_000_000, 1_000_000, 2_000_000, "received"],
            [0, 0, 0, 1, 1, 0, 2, 14_000_000, 24_000_000, 1_000_000, 2_000_000, "received"],
        ],
    )
    with pytest.raises(ValueError, match="queue"):
        parse_network_results(tmp_path, state)


def test_after_window_delivery_is_censored_and_does_not_invent_latency(tmp_path):
    state = prepare_network_replay(document(), replace(config(), duration_s=0.015))
    write_results(
        tmp_path, [[d, 0, 0, 0, 0, 20_000_000, "received_after_window", 970, 2] for d in (0, 1)]
    )
    result = parse_network_results(tmp_path, state)["aggregate"]
    assert result["received_after_window_packets"] == 2
    assert result["censored_packets"] == 2
    assert result["goodput_bps"] == 0
    assert result["mean_delay_s"] is None


def test_outage_hop_has_actual_phy_receive_but_no_udp_delivery_time(tmp_path):
    state = prepared()
    state["routes"].insert(1, {"time_ns": 4_000_000, "state": "disconnected", "path": ""})
    state["edges"] = [
        dict(
            zip(
                EDGE_FIELDS,
                (time, edge, 0, edge + 1, 1_000_000 if time == 0 else 0, 2_000_000, int(time == 0)),
            )
        )
        for time in (0, 4_000_000, 100_000_000)
        for edge in (0, 1)
    ]
    write_results(
        tmp_path,
        [[d, 0, 0, 0, 0, "", "rx_outage_drop", 970, 1] for d in (0, 1)],
        [
            [d, 0, 0, 0, edge, start, 0, 0, 10_000_000, 1_000_000, 2_000_000, "rx_outage_drop"]
            for d, edge, start in ((0, 0, 1), (1, 1, 2))
        ],
    )
    assert parse_network_results(tmp_path, state)["aggregate"]["rx_outage_drop_packets"] == 2


@pytest.mark.parametrize("handshake", [b"wrong\n", b"openleo-ns3-network/1 ns-3.48"])
def test_wrong_or_inexact_handshake_leaves_output_absent(
    tmp_path, monkeypatch, handshake, global_on
):
    backend = tmp_path / "backend"
    backend.write_bytes(b"fixture")
    monkeypatch.setattr(
        "openleo.replay.network_replay.subprocess.run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 0, handshake, b""),
    )
    output = tmp_path / "output"
    with pytest.raises(ValueError, match="handshake"):
        run_network_replay(global_on, config(), backend, output)
    assert not output.exists()


@pytest.mark.parametrize(
    "mode", ["overload", "acquisition", "disconnected", "internal_switch", "outage", "high_rate"]
)
def test_actual_backend_flow_queue_and_route_variants(tmp_path, mode):
    backend = os.environ.get("OPENLEO_NS3_NETWORK")
    if not backend:
        pytest.skip("set OPENLEO_NS3_NETWORK to the actual pinned ns-3 network backend")
    source, settings = document(), config()
    if mode == "overload":
        settings = replace(
            settings, offered_load_bps=2_000_000, flows_per_direction=2, queue_packets=1
        )
    elif mode == "acquisition":
        settings = replace(settings, offered_load_bps=155_200, acquisition_delay_s=0.02)
    elif mode == "disconnected":
        for frame in source.routes:
            frame["minimum_delay"] = None
    elif mode == "outage":
        settings = replace(settings, duration_s=20, offered_load_bps=7760, queue_packets=1)
        source.links = [
            {key: (1000.0, delay) for key, (_, delay) in frame.items()} for frame in source.links
        ]
        source.routes[1]["minimum_delay"] = None
    elif mode == "high_rate":
        settings = replace(
            settings,
            duration_s=0.0001,
            packet_size_bytes=64,
            offered_load_bps=1_000_000_000,
            flows_per_direction=4,
        )
        source.links = [
            {key: (1_000_000_000_000.0, delay) for key, (_, delay) in frame.items()}
            for frame in source.links
        ]
    else:
        source = multihop_document([4, 0, 2, 3, 5])
        settings = replace(settings, offered_load_bps=1552, duration_s=20, acquisition_delay_s=12)
    result = execute(source, settings, backend, tmp_path / "output")["aggregate"]
    if mode == "overload":
        assert result["queue_drop_packets"] > 0
        assert result["end_of_window_drop_packets"] > 0
    elif mode == "disconnected":
        assert result["no_route_packets"] == 2
    elif mode == "acquisition":
        assert result["acquisition_suppressed_packets"] == 2
        assert result["received_packets"] == 2
    elif mode == "outage":
        assert result["outage_queue_drop_packets"] > 0
        assert result["rx_outage_drop_packets"] > 0
        assert result["no_route_packets"] > 0
    elif mode == "high_rate":
        assert result["end_of_window_drop_packets"] == result["offered_packets"]
        assert result["queue_drop_packets"] == 0
    else:
        assert result["acquisition_suppressed_packets"] == 6
        assert result["received_packets"] == 2


def test_unbounded_csv_field_is_a_validation_error(tmp_path):
    (tmp_path / "packets.csv").write_text(
        ",".join(PACKET_FIELDS) + "\n" + "1" * 140_000, encoding="ascii"
    )
    with pytest.raises(ValueError):
        parse_network_results(tmp_path, prepared())


def test_outage_queue_drop_requires_outage_on_the_actual_next_edge(tmp_path):
    state = prepared()
    state["routes"] = [
        {"time_ns": t, "state": status, "path": path}
        for t, status, path in (
            (0, "ready", "3;0;1;2;4"),
            (12_000_000, "disconnected", ""),
            (100_000_000, "stop", ""),
        )
    ]
    state["metadata"]["selection"] = {
        "source_node": 3,
        "target_node": 4,
        "node_count": 5,
        "source_station_index": 0,
        "target_station_index": 1,
    }
    state["edges"] = [
        dict(zip(EDGE_FIELDS, (time, edge, a, b, 1_000_000 if up else 0, 2_000_000, int(up))))
        for time in (0, 12_000_000, 100_000_000)
        for edge, (a, b) in enumerate(((0, 1), (0, 3), (1, 2), (2, 4)))
        for up in (time == 0 or (edge == 0 and time == 12_000_000),)
    ]
    # Drop at intermediary 0 after a successful ground hop; its onward edge 0
    # stays available, while unrelated ISL edge 2 has an outage.
    write_results(
        tmp_path,
        [
            [0, 0, 0, 0, 0, "", "outage_queue_drop", 970, 1],
            [1, 0, 0, 0, 0, "", "send_error", 970, 0],
        ],
        [[0, 0, 0, 0, 1, 3, 0, 0, 10_000_000, 1_000_000, 2_000_000, "received"]],
    )
    with pytest.raises(ValueError, match="outage"):
        parse_network_results(tmp_path, state)


def test_prepared_offer_count_cannot_use_boolean(tmp_path):
    state = prepared()
    state["metadata"]["traffic"]["offered_packets_per_flow"] = [True]
    write_results(tmp_path)
    with pytest.raises(ValueError):
        parse_network_results(tmp_path, state)


def test_prepared_topology_cannot_include_other_ground_stations(tmp_path):
    state = prepared()
    state["metadata"]["selection"]["node_count"] = 4
    state["edges"] = sorted(
        [
            *state["edges"],
            *[dict(zip(EDGE_FIELDS, (t, 2, 0, 3, 0, 0, 0))) for t in (0, 100_000_000)],
        ],
        key=lambda row: (row["time_ns"], row["edge_id"]),
    )
    write_results(tmp_path)
    with pytest.raises(ValueError, match="ground"):
        parse_network_results(tmp_path, state)


@pytest.mark.parametrize("status", ["queue_drop", "end_of_window_drop", "unresolved"])
def test_uncontended_packet_cannot_disappear_before_its_first_hop(tmp_path, status):
    write_results(tmp_path, [[d, 0, 0, 0, 0, "", status, 970, 0] for d in (0, 1)], [])
    with pytest.raises(ValueError, match="FIFO"):
        parse_network_results(tmp_path, prepared())


def test_uncontended_phy_transmissions_cannot_invent_idle_queue_delay(tmp_path):
    write_results(tmp_path)
    for name, fields in (("packets", PACKET_FIELDS), ("hops", HOP_FIELDS)):
        with (tmp_path / f"{name}.csv").open(newline="") as file:
            rows = list(csv.DictReader(file))
        for row in rows:
            for field in fields:
                if field in ("rx_time_ns", "phy_tx_time_ns", "phy_rx_time_ns"):
                    row[field] = int(row[field]) + 50_000_000
        with (tmp_path / f"{name}.csv").open("w", newline="") as file:
            writer = csv.DictWriter(file, fields)
            writer.writeheader()
            writer.writerows(rows)
    with pytest.raises(ValueError, match="FIFO"):
        parse_network_results(tmp_path, prepared())


def fifo_fixture(directory, statuses, transmitted=(0, 1), rate=1_000_000):
    source = document()
    source.links = [
        {key: (rate, delay) for key, (_, delay) in frame.items()} for frame in source.links
    ]
    state = prepare_network_replay(
        source, replace(config(), duration_s=0.009, offered_load_bps=1_940_000, queue_packets=1)
    )
    packets = [
        [
            direction,
            0,
            sequence,
            sequence * 4_000_000,
            sequence * 4_000_000,
            "",
            status,
            970,
            int(sequence in transmitted),
        ]
        for sequence, status in enumerate(statuses)
        for direction in (0, 1)
    ]
    serialization = 8_000_000 if rate == 1_000_000 else 20_000_000
    hops = [
        [
            direction,
            0,
            sequence,
            0,
            direction,
            direction + 1,
            0,
            tx,
            tx + serialization + 2_000_000,
            rate,
            2_000_000,
            "received",
        ]
        for direction in (0, 1)
        for sequence in transmitted
        for tx in (0 if sequence == 0 else 8_000_000,)
    ]
    write_results(directory, packets, hops)
    return state


def test_equal_time_source_offer_and_phy_dequeue_allows_native_fifo_overflow(tmp_path):
    state = fifo_fixture(tmp_path, ("end_of_window_drop", "end_of_window_drop", "queue_drop"))
    aggregate = parse_network_results(tmp_path, state)["aggregate"]
    assert aggregate["queue_drop_packets"] == 2
    assert aggregate["end_of_window_drop_packets"] == 4


@pytest.mark.parametrize(
    "statuses,transmitted,rate",
    [
        (("end_of_window_drop", "queue_drop", "queue_drop"), (0,), 1_000_000),
        (("end_of_window_drop",) * 3, (0, 2), 1_000_000),
        (("end_of_window_drop",) * 3, (0,), 400_000),
    ],
)
def test_impossible_fifo_overflow_skip_and_waiting_capacity_are_rejected(
    tmp_path, statuses, transmitted, rate
):
    state = fifo_fixture(tmp_path, statuses, transmitted, rate)
    with pytest.raises(ValueError, match="FIFO"):
        parse_network_results(tmp_path, state)
