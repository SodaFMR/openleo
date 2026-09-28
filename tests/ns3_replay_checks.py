"""Analytical checks against a compiled ns-3.48 replay (no Python simulator).

Run: python tests/ns3_replay_checks.py /absolute/path/to/ns3-replay-binary
"""

import csv
import io
import subprocess
import sys
import tempfile
from pathlib import Path

HEADER = "time_ns,rate_bps,delay_ns,available\n"
FIELDS = [
    "direction",
    "sequence",
    "offered_time_ns",
    "udp_tx_time_ns",
    "phy_tx_time_ns",
    "rx_time_ns",
    "status",
    "payload_bytes",
]


def check_backend(executable):
    """Catch framing, queue, boundary, outage, state capture and parser regressions."""
    executable = Path(executable).resolve()
    assert executable.is_file(), f"compiled ns-3 replay is missing: {executable}"
    version = subprocess.run(
        [str(executable), "--PrintVersion"], capture_output=True, text=True, check=True
    )
    assert version.stdout.strip() == "openleo-ns3-replay/1 ns-3.48"

    with tempfile.TemporaryDirectory(prefix="openleo-ns3-check-") as temporary:
        trace = Path(temporary) / "trace.csv"
        output = Path(temporary) / "packets.csv"

        def run(samples, interval=10_000_000, queue=1, packet=970, seed=1, reject=False, extra=()):
            trace.write_text(
                samples
                if isinstance(samples, str)
                else HEADER + "".join(",".join(map(str, sample)) + "\n" for sample in samples),
                encoding="ascii",
            )
            output.unlink(missing_ok=True)
            result = subprocess.run(
                [
                    str(executable),
                    f"--trace={trace}",
                    f"--output={output}",
                    f"--intervalNs={interval}",
                    f"--packetSize={packet}",
                    f"--queuePackets={queue}",
                    f"--seed={seed}",
                    *extra,
                ],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            if reject:
                assert result.returncode != 0, (samples, result)
                assert not output.exists(), "invalid input created an output file"
                return
            assert result.returncode == 0, (result.returncode, result.stderr)
            reader = csv.DictReader(io.StringIO(output.read_text(encoding="ascii")))
            assert reader.fieldnames == FIELDS
            rows = list(reader)
            end = samples[-1][0]
            count = (end - 1) // interval + 1
            assert len(rows) == count * 2
            assert [(int(r["offered_time_ns"]), int(r["direction"])) for r in rows] == [
                (seq * interval, direction) for seq in range(count) for direction in (0, 1)
            ]
            assert all(int(r["sequence"]) == i // 2 for i, r in enumerate(rows))
            assert all(r["payload_bytes"] == str(packet) for r in rows)
            unexpected = [r for r in rows if r["status"] in {"unresolved", "send_error"}]
            assert not unexpected, unexpected[:3]
            return rows

        # 970 payload + UDP 8 + IPv4 20 + PPP 2 = 1000 bytes, 8 ms at 1 Mbps.
        constant = [(0, 1_000_000, 2_000_000, 1), (45_000_000, 1_000_000, 2_000_000, 1)]
        rows = run(constant)
        for row in rows:
            offered = int(row["offered_time_ns"])
            assert int(row["udp_tx_time_ns"]) == offered
            assert int(row["phy_tx_time_ns"]) == offered
            assert int(row["rx_time_ns"]) == offered + 10_000_000
            assert row["status"] == (
                "received" if offered < 40_000_000 else "received_after_window"
            )
        assert rows == run(constant), "same seed and trace must reproduce identical packet rows"

        rows = run(constant, interval=1_000_000)
        statuses = {r["status"] for r in rows}
        assert {"queue_drop", "end_of_window_drop", "received_after_window"} <= statuses
        assert rows[2]["phy_tx_time_ns"] == "8000000", "one waiting packet is retained"
        assert rows[4]["status"] == "queue_drop", "a second waiting packet exceeds queue=1"
        assert all(int(r["phy_tx_time_ns"]) < 45_000_000 for r in rows if r["phy_tx_time_ns"])
        for direction in ("0", "1"):
            received = [
                r for r in rows if r["direction"] == direction and r["status"] == "received"
            ]
            assert len(received) * 1000 * 8 <= 1_000_000 * 0.045

        rows = run([(0, 0, 0, 0), (30_000_000, 0, 0, 0)])
        assert all(r["status"] == "outage_suppressed" and not r["udp_tx_time_ns"] for r in rows)

        # A fade during serialization must corrupt a packet even after recovery.
        tx_fade = [
            (0, 1_000_000, 2_000_000, 1),
            (4_000_000, 0, 2_000_000, 0),
            (6_000_000, 1_000_000, 2_000_000, 1),
            (30_000_000, 1_000_000, 2_000_000, 1),
        ]
        rows = run(tx_fade)
        assert all(r["status"] == "rx_outage_drop" for r in rows[:2])
        assert all(r["rx_time_ns"] == "10000000" for r in rows[:2])
        assert all(r["status"] == "received" for r in rows[2:4])
        rows = run(tx_fade, interval=1_000_000, queue=2)
        assert "outage_queue_drop" in {r["status"] for r in rows}
        assert all(
            r["status"] == "outage_suppressed" for r in rows if r["offered_time_ns"] == "4000000"
        )

        # RX bits span [2, 10) ms, while TX bits span [0, 8) ms.
        rows = run(
            [
                (0, 1_000_000, 2_000_000, 1),
                (9_000_000, 0, 2_000_000, 0),
                (9_500_000, 1_000_000, 2_000_000, 1),
                (30_000_000, 1_000_000, 2_000_000, 1),
            ]
        )
        assert all(r["status"] == "rx_outage_drop" for r in rows[:2])
        # An outage only in the propagation gap does not intersect either bit interval.
        rows = run(
            [
                (0, 1_000_000, 20_000_000, 1),
                (10_000_000, 0, 20_000_000, 0),
                (15_000_000, 1_000_000, 20_000_000, 1),
                (40_000_000, 1_000_000, 20_000_000, 1),
            ]
        )
        assert all(r["status"] == "received" and r["rx_time_ns"] == "28000000" for r in rows[:2])

        # Already-started packets retain BOTH old rate and old delay.
        rows = run(
            [
                (0, 1_000_000, 2_000_000, 1),
                (4_000_000, 2_000_000, 3_000_000, 1),
                (30_000_000, 2_000_000, 3_000_000, 1),
            ]
        )
        assert [int(r["rx_time_ns"]) for r in rows[::2]] == [10_000_000, 17_000_000, 27_000_000]
        rows = run(
            [
                (0, 1_000_000, 2_000_000, 1),
                (10_000_000, 2_000_000, 3_000_000, 1),
                (30_000_000, 2_000_000, 3_000_000, 1),
            ]
        )
        assert rows[2]["rx_time_ns"] == "17000000", "state updates precede boundary sends"
        rows = run(
            [
                (0, 1_000_000, 2_000_000, 1),
                (8_000_000, 2_000_000, 3_000_000, 1),
                (30_000_000, 2_000_000, 3_000_000, 1),
            ],
            interval=1_000_000,
        )
        assert rows[2]["phy_tx_time_ns"] == "8000000"
        assert rows[2]["rx_time_ns"] == "15000000", (
            "state updates precede queued transmission starts"
        )

        rows = run(
            [
                (0, 1_000_000, 2_000_000, 1),
                (10_000_000, 0, 2_000_000, 0),
                (11_000_000, 1_000_000, 2_000_000, 1),
                (30_000_000, 1_000_000, 2_000_000, 1),
            ]
        )
        assert all(r["status"] == "received" for r in rows[:2]), "outage intervals are half-open"

        rows = run([(0, 1, 1_000_000_000, 1), (1, 0, 0, 0)], packet=1400)
        assert all(
            r["status"] == "received_after_window" and r["rx_time_ns"] == "11441000000000"
            for r in rows
        ), "drain must cover the slowest admitted transmission"

        rows = run([(0, 1_000_000, 0, 1), (8_000_000, 0, 0, 0)], interval=1_000_000)
        assert all(r["status"] == "received_after_window" for r in rows[:2])
        assert all(r["status"] == "end_of_window_drop" for r in rows[2:4])
        assert all(not r["phy_tx_time_ns"] for r in rows[2:])

        invalid = [
            "wrong,header\n0,1000,0,1\n100,1000,0,1\n",
            HEADER + "0,1000,0,1,5\n100,1000,0,1\n",
            HEADER + "0,1000,0\n100,1000,0,1\n",
            HEADER + "0,1000junk,0,1\n100,1000,0,1\n",
            HEADER + "0,NaN,0,1\n100,1000,0,1\n",
            [(1, 1000, 0, 1), (100, 1000, 0, 1)],
            [(0, 1000, 0, 1), (0, 1000, 0, 1)],
            [(0, 1000, 0, 1)],
            [(0, 0, 0, 1), (100, 0, 0, 1)],
            [(0, 1, 0, 0), (100, 0, 0, 0)],
            [(0, 1000, -1, 1), (100, 1000, 0, 1)],
            [(0, 1000, 1_000_000_001, 1), (100, 1000, 0, 1)],
            [(0, 1000, 0, 2), (100, 1000, 0, 1)],
            [(0, 1_000_000_000_001, 0, 1), (100, 1000, 0, 1)],
            [(0, 1000, 0, 1), (3_600_000_000_001, 1000, 0, 1)],
            [(i, 1000, 0, 1) for i in range(4097)],
        ]
        for samples in invalid:
            run(samples, reject=True)
        for options in (
            {"interval": 0},
            {"interval": -1},
            {"interval": 1},
            {"interval": 2**63},
            {"packet": 63},
            {"packet": 1401},
            {"queue": 0},
            {"queue": 10001},
            {"seed": 0},
            {"seed": 2**31},
            {"packet": "970junk"},
            {"packet": "NaN"},
            {"extra": ("--seed=2",)},
            {"extra": ("--unknown=1",)},
        ):
            run(constant, reject=True, **options)
        run(constant, seed=2**31 - 1)
        rows = run([(0, 10**12, 0, 1), (100_000, 10**12, 0, 1)], interval=1, packet=1400)
        assert len(rows) == 200_000, "the exact packet budget is accepted and reconciled"
        run([(0, 1_000_000_000_000, 0, 1), (1000, 1_000_000_000_000, 0, 1)], packet=64, reject=True)
    print("ns-3.48 analytical replay checks passed")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: python tests/ns3_replay_checks.py /path/to/ns3-replay")
    check_backend(sys.argv[1])
