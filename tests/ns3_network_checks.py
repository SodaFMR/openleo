"""Real ns-3.48 network replay checks; pass the compiled backend as argv[1]."""

import csv
import subprocess
import sys
import tempfile
from itertools import combinations, islice
from pathlib import Path

PACKETS = "direction,flow,sequence,offered_time_ns,udp_tx_time_ns,rx_time_ns,status,payload_bytes,hop_count"
HOPS = "direction,flow,sequence,hop_index,edge_id,tx_node,rx_node,phy_tx_time_ns,phy_rx_time_ns,rate_bps,delay_ns,status"


def check_backend(executable):
    executable = str(Path(executable).resolve())
    version = subprocess.run(
        [executable, "--PrintVersion"], check=True, capture_output=True, text=True
    )
    assert version.stdout == "openleo-ns3-network/1 ns-3.48\n"
    with tempfile.TemporaryDirectory(prefix="openleo-network-check-") as temporary:
        root = Path(temporary)

        def run(
            frames,
            edges=((0, 0, 1), (1, 1, 2)),
            interval=30_000_000,
            flows=1,
            queue=1,
            nodes=3,
            target=2,
            reject=False,
            extra=(),
            edge_override=None,
            route_override=None,
        ):
            # Frames: time, route state, path, rate, delay; optional down edge IDs.
            edge_text = "time_ns,edge_id,node_a,node_b,rate_bps,delay_ns,available\n"
            route_text = "time_ns,state,path\n"
            for time, state, path, rate, delay, *down in frames:
                route_text += f"{time},{state},{';'.join(map(str, path))}\n"
                for edge, a, b in edges:
                    up = edge not in (down[0] if down else ())
                    edge_text += f"{time},{edge},{a},{b},{rate if up else 0},{delay},{int(up)}\n"
            (root / "edges.csv").write_text(
                edge_text if edge_override is None else edge_override, encoding="ascii"
            )
            (root / "routes.csv").write_text(
                route_text if route_override is None else route_override, encoding="ascii"
            )
            output = root / "output"
            for name in ("packets.csv", "hops.csv"):
                (output / name).unlink(missing_ok=True)
            args = [
                f"--edges={root / 'edges.csv'}",
                f"--routes={root / 'routes.csv'}",
                f"--output={output}",
                "--source=0",
                f"--target={target}",
                f"--nodeCount={nodes}",
                f"--intervalNs={interval}",
                "--packetSize=970",
                f"--queuePackets={queue}",
                f"--flows={flows}",
                "--seed=1",
                *extra,
            ]
            result = subprocess.run(
                [executable, *args], capture_output=True, text=True, timeout=60, check=False
            )
            if reject:
                assert result.returncode != 0, result
                assert not (output / "packets.csv").exists()
                return
            assert result.returncode == 0, result.stderr
            tables = []
            for name, header in (("packets", PACKETS), ("hops", HOPS)):
                with (output / f"{name}.csv").open() as stream:
                    reader = csv.DictReader(stream)
                    assert reader.fieldnames == header.split(",")
                    tables.append(list(reader))
            packets, hops = tables
            end = frames[-1][0]
            count = (
                sum(
                    (end - 1 - flow * interval // flows) // interval + 1
                    for flow in range(flows)
                    if flow * interval // flows < end
                )
                * 2
            )
            assert len(packets) == count
            assert packets == sorted(
                packets,
                key=lambda r: tuple(
                    int(r[k]) for k in ("offered_time_ns", "direction", "flow", "sequence")
                ),
            )
            assert hops == sorted(
                hops,
                key=lambda r: tuple(
                    int(r[k]) for k in ("direction", "flow", "sequence", "hop_index")
                ),
            )
            assert all(r["status"] not in {"unresolved", "send_error"} for r in packets)
            grouped = {}
            for hop in hops:
                key = tuple(hop[k] for k in ("direction", "flow", "sequence"))
                grouped.setdefault(key, []).append(hop)
                assert int(hop["phy_tx_time_ns"]) < end
                assert int(hop["phy_rx_time_ns"]) == (
                    int(hop["phy_tx_time_ns"])
                    + 8_000_000_000_000 // int(hop["rate_bps"])
                    + int(hop["delay_ns"])
                )
            for packet in packets:
                key = tuple(packet[k] for k in ("direction", "flow", "sequence"))
                actual = grouped.get(key, [])
                assert int(packet["hop_count"]) == len(actual)
                assert [int(h["hop_index"]) for h in actual] == list(range(len(actual)))
                if packet["status"].startswith("received"):
                    assert packet["rx_time_ns"] == actual[-1]["phy_rx_time_ns"]
                else:
                    assert not packet["rx_time_ns"]
            return packets, hops

        constant = [
            (0, "ready", [0, 1, 2], 1_000_000, 2_000_000),
            (45_000_000, "stop", [], 1_000_000, 2_000_000),
        ]
        packets, hops = run(constant)
        assert all(int(r["rx_time_ns"]) == int(r["offered_time_ns"]) + 20_000_000 for r in packets)
        assert [r["status"] for r in packets] == ["received"] * 2 + ["received_after_window"] * 2
        assert run(constant) == (packets, hops)
        packets, hops = run(constant, flows=2, interval=2_000_000, queue=2)
        assert {"queue_drop", "end_of_window_drop"} <= {r["status"] for r in packets}
        assert {r["flow"] for r in hops} == {"0", "1"}
        assert [
            int(r["phy_tx_time_ns"])
            for r in hops
            if r["direction"] == "0" and r["hop_index"] == "0"
        ][:3] == [0, 16_000_000, 8_000_000]
        # In-flight packets use the new route at their next forwarding decision.
        switched = [
            (0, "ready", [0, 1, 2], 1_000_000, 2_000_000),
            (5_000_000, "ready", [0, 1, 3, 2], 1_000_000, 2_000_000),
            (45_000_000, "stop", [], 1_000_000, 2_000_000),
        ]
        packets, hops = run(switched, edges=((0, 0, 1), (1, 1, 2), (2, 1, 3), (3, 3, 2)), nodes=4)
        assert [h["edge_id"] for h in hops if h["direction"] == "0" and h["sequence"] == "0"] == [
            "0",
            "2",
            "3",
        ]
        # Removing routes while packets travel must cause actual IP forwarding drops.
        packets, _ = run(
            [
                (0, "ready", [0, 1, 2], 1_000_000, 2_000_000),
                (5_000_000, "acquiring", [], 1_000_000, 2_000_000),
                (45_000_000, "stop", [], 1_000_000, 2_000_000),
            ],
            interval=10_000_000,
        )
        assert [p["status"] for p in packets[:2]] == ["route_drop"] * 2
        assert all(p["status"] == "acquisition_suppressed" for p in packets[2:])
        fade = [
            (0, "ready", [0, 1, 2], 1_000_000, 2_000_000),
            (4_000_000, "disconnected", [], 1_000_000, 2_000_000, (0, 1)),
            (6_000_000, "ready", [0, 1, 2], 1_000_000, 2_000_000),
            (45_000_000, "stop", [], 1_000_000, 2_000_000),
        ]
        packets, hops = run(fade, interval=1_000_000, queue=2)
        assert {"outage_queue_drop", "rx_outage_drop", "no_route", "received"} <= {
            p["status"] for p in packets
        }
        assert hops[0]["status"] == "rx_outage_drop"
        # Fades apply per physical edge and include RX-bit intervals after TX ended.
        for onset, recovery in ((4_000_000, 6_000_000), (9_000_000, 9_500_000)):
            packets, _ = run(
                [
                    constant[0],
                    (onset, "disconnected", [], 10**6, 2_000_000, (0,)),
                    (recovery, "ready", [0, 1, 2], 10**6, 2_000_000),
                    constant[-1],
                ]
            )
            assert [p["status"] for p in packets[:2]] == ["rx_outage_drop", "received"]
        packets, _ = run(
            [
                (0, "ready", [0, 1, 2], 10**6, 20_000_000),
                (10_000_000, "disconnected", [], 10**6, 20_000_000, (0, 1)),
                (15_000_000, "ready", [0, 1, 2], 10**6, 20_000_000),
                (80_000_000, "stop", [], 10**6, 20_000_000),
            ],
            interval=100_000_000,
        )
        assert all(p["status"] == "received" and p["rx_time_ns"] == "56000000" for p in packets)
        packets, _ = run(
            [
                constant[0],
                (20_000_000, "disconnected", [], 10**6, 2_000_000, (0, 1)),
                (21_000_000, "ready", [0, 1, 2], 10**6, 2_000_000),
                constant[-1],
            ]
        )
        assert all(p["status"] == "received" for p in packets[:2]), "outages are half-open"
        packets, hops = run([constant[0], (5_000_000, "stop", [], 1_000_000, 2_000_000)])
        assert all(p["status"] == "end_of_window_drop" and p["hop_count"] == "1" for p in packets)
        # Stop exactly at intermediate RX precedes forwarding.
        packets, _ = run([constant[0], (10_000_000, "stop", [], 1_000_000, 2_000_000)])
        assert all(p["status"] == "end_of_window_drop" for p in packets)
        packets, hops = run(
            [(0, "ready", [0, 2], 1, 10**9), (1, "stop", [], 0, 0, (0,))], edges=((0, 0, 2),)
        )
        assert all(
            p["status"] == "received_after_window" and p["rx_time_ns"] == "8001000000000"
            for p in packets
        ), "drain must include the slowest admitted final hop"
        # Empty disconnected topology is useful for a fully obstructed window.
        packets, hops = run(
            [(0, "disconnected", [], 1_000_000, 0), (45_000_000, "stop", [], 1_000_000, 0)],
            edges=(),
        )
        assert not hops and all(p["status"] == "no_route" for p in packets)
        run(constant, edges=(), reject=True)
        packets, _ = run(constant, interval=2**63 - 1, flows=4)
        assert len(packets) == 2, "large interval phase arithmetic must not wrap"
        # Full permitted route length must work, beyond the default IPv4 TTL of 64.
        packets, _ = run(
            [(0, "ready", list(range(144)), 10**12, 0), (2000, "stop", [], 10**12, 0)],
            edges=tuple((i, i, i + 1) for i in range(143)),
            nodes=144,
            target=143,
        )
        assert all(p["hop_count"] == "143" and p["rx_time_ns"] == "1144" for p in packets)
        # Already-started hops retain rate/delay; subsequent hops use the new frame.
        packets, hops = run(
            [
                constant[0],
                (4_000_000, "ready", [0, 1, 2], 2_000_000, 3_000_000),
                (25_000_000, "stop", [], 2_000_000, 3_000_000),
            ]
        )
        assert all(p["rx_time_ns"] == "17000000" for p in packets)
        assert [(h["rate_bps"], h["delay_ns"]) for h in hops[:2]] == [
            ("1000000", "2000000"),
            ("2000000", "3000000"),
        ]
        # Native TTL bounds loops caused by alternating selected routes. TTL ICMP
        # must not create unmeasured transmissions or enter the device queues.
        looping = (
            [(0, "ready", [0, 1, 2, 3], 10**9, 2000)]
            + [
                (
                    15_000 + i * 10_000,
                    "ready",
                    [0, 2, 1, 3] if i % 2 == 0 else [0, 1, 2, 3],
                    10**9,
                    2000,
                )
                for i in range(258)
            ]
            + [(2_600_000, "stop", [], 10**9, 2000)]
        )
        packets, hops = run(
            looping,
            edges=((0, 0, 1), (1, 1, 2), (2, 2, 3), (3, 0, 2), (4, 1, 3)),
            nodes=4,
            target=3,
        )
        assert len(hops) == 510
        assert all(p["status"] == "route_drop" and p["hop_count"] == "255" for p in packets)
        # Route changes do not rewrite packets already admitted into a device FIFO.
        packets, hops = run(
            [
                constant[0],
                (4_000_000, "acquiring", [], 1_000_000, 2_000_000),
                (45_000_000, "stop", [], 1_000_000, 2_000_000),
            ],
            interval=1_000_000,
            queue=2,
        )
        assert any(int(h["phy_tx_time_ns"]) > 4_000_000 for h in hops)
        # Exact offered and packet-hop budgets are accepted, then one more is rejected.
        fast = [(0, "ready", [0, 1, 2], 10**12, 0), (100_000, "stop", [], 10**12, 0)]
        packets, _ = run(fast, interval=1)
        assert len(packets) == 200_000
        run([fast[0], (100_001, "stop", [], 10**12, 0)], interval=1, reject=True)
        run(
            [(0, "ready", [0, 1, 2, 3], 10**12, 0), (100_000, "stop", [], 10**12, 0)],
            edges=((0, 0, 1), (1, 1, 2), (2, 2, 3)),
            nodes=4,
            target=3,
            interval=1,
            reject=True,
        )
        for options in (
            {"interval": 0},
            {"interval": 1},
            {"flows": 0},
            {"flows": 5},
            {"queue": 0},
            {"queue": 10001},
            {"nodes": 145},
            {"target": 3},
            {"extra": ("--seed=2",)},
            {"extra": ("--unknown=1",)},
        ):
            run(constant, reject=True, **options)
        for frames in (
            [constant[0]],
            [constant[0], constant[0], constant[-1]],
            [(0, "ready", [0, 1, 0, 2], 1_000_000, 0), constant[-1]],
            [(0, "ready", [0, 2], 1_000_000, 0), constant[-1]],
            [(0, "ready", [0, 1, 2], 0, 0), constant[-1]],
            [(0, "stop", [], 1_000_000, 0), constant[-1]],
        ):
            run(frames, reject=True)
        run(constant, edges=((0, 0, 1), (0, 1, 2)), reject=True)
        run(constant, edges=((0, 0, 1), (1, 1, 0)), reject=True)
        run(constant, edge_override="wrong,header\n", reject=True)
        run(constant, route_override="wrong,header\n", reject=True)
        for value in ("-1", "NaN", "100junk", "18446744073709551616"):
            run(constant, route_override=f"time_ns,state,path\n{value},ready,0;1;2\n", reject=True)
        for rows in (
            "0,0,0,1,1000000,0,1,extra\n",
            "0,0,0,1,1000000,0\n",
            "0,0,0,1,1000000,1000000001,1\n",
            "0,0,0,1,1000000,0,2\n",
            "0,0,0,1,1000000000001,0,1\n",
            "0,0,0,1,NaN,0,1\n",
            "0,0,0,1,1000000,0,1\n45000000,0,0,2,1000000,0,1\n",
            "0,0,0,1,1000000,0,1\n45000000,1,0,1,1000000,0,1\n",
        ):
            run(
                constant,
                edge_override="time_ns,edge_id,node_a,node_b,rate_bps,delay_ns,available\n" + rows,
                reject=True,
            )
        run(
            [(i, "disconnected", [], 10**6, 0) for i in range(4096)]
            + [(4096, "stop", [], 10**6, 0)],
            edges=(),
            reject=True,
        )
        many_edges = tuple(
            (i, *pair) for i, pair in enumerate(islice(combinations(range(144), 2), 1025))
        )
        inactive = [(0, "disconnected", [], 10**6, 0), (1000, "stop", [], 10**6, 0)]
        run(inactive, edges=many_edges, nodes=144, reject=True)
        run(
            [(i, "disconnected", [], 10**6, 0) for i in range(196)] + [(196, "stop", [], 10**6, 0)],
            edges=many_edges[:1024],
            nodes=144,
            reject=True,
        )
    print("ns-3.48 analytical network checks passed")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: python tests/ns3_network_checks.py /path/to/ns3-network-replay")
    check_backend(sys.argv[1])
