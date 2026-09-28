"""Strict CSV observations and accounting for the native network replay protocol."""

from __future__ import annotations

import csv
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from io import StringIO
from itertools import pairwise
from pathlib import Path

from openleo.packet_replay import (
    MAX_RATE_BPS,
    _csv_integer,
    _nearest_serialization_ns,
    _read_result,
    _trace_state,
)

EDGE_FIELDS = ("time_ns", "edge_id", "node_a", "node_b", "rate_bps", "delay_ns", "available")
ROUTE_FIELDS = ("time_ns", "state", "path")
PACKET_FIELDS = (
    "direction",
    "flow",
    "sequence",
    "offered_time_ns",
    "udp_tx_time_ns",
    "rx_time_ns",
    "status",
    "payload_bytes",
    "hop_count",
)
HOP_FIELDS = (
    "direction",
    "flow",
    "sequence",
    "hop_index",
    "edge_id",
    "tx_node",
    "rx_node",
    "phy_tx_time_ns",
    "phy_rx_time_ns",
    "rate_bps",
    "delay_ns",
    "status",
)
STATUSES = frozenset(
    (
        "received",
        "received_after_window",
        "no_route",
        "acquisition_suppressed",
        "queue_drop",
        "outage_queue_drop",
        "rx_outage_drop",
        "route_drop",
        "end_of_window_drop",
        "send_error",
        "unresolved",
    )
)
MAX_HOPS = 500_000


def _outage_overlap(start, stop, intervals):
    index = bisect_right(intervals, start, key=lambda interval: interval[1])
    return start < stop and index < len(intervals) and intervals[index][0] < stop


def _csv_records(path, fields, maximum_rows):
    reader = csv.reader(StringIO(_read_result(path), newline=""))
    try:
        if next(reader, None) != list(fields):
            raise ValueError(f"{path.name} header does not match the network protocol")
        rows = []
        for raw in reader:
            if len(rows) >= maximum_rows or len(raw) != len(fields):
                raise ValueError(f"{path.name} exceeds its row budget or has malformed fields")
            rows.append(dict(zip(fields, raw, strict=True)))
    except csv.Error as exc:
        raise ValueError(f"{path.name} contains malformed or oversized CSV fields") from exc
    return rows


def _metrics(records, payload, duration):
    counts = Counter(record["status"] for record in records)
    received = [r for r in records if r["status"] == "received"]
    delays = [(r["rx_time_ns"] - r["udp_tx_time_ns"]) / 1e9 for r in received]
    return {
        "offered_packets": len(records),
        "admitted_packets": len(records)
        - sum(counts[s] for s in ("no_route", "acquisition_suppressed", "send_error")),
        "received_packets": counts["received"] + counts["received_after_window"],
        "received_in_window_packets": counts["received"],
        **{f"{status}_packets": counts[status] for status in sorted(STATUSES - {"received"})},
        "censored_packets": sum(
            counts[s] for s in ("received_after_window", "end_of_window_drop", "unresolved")
        ),
        "goodput_bps": len(received) * payload * 8e9 / duration,
        "mean_delay_s": sum(delays) / len(delays) if delays else None,
        "max_delay_s": max(delays) if delays else None,
    }


def _validate_fifo(entries, capacity):
    """Reconcile actual services and inferred queue admissions, without new PHY events."""
    services = sorted((entry for entry in entries if entry[1] is not None), key=lambda e: e[1])
    previous_end, previous_admission = 0, -1
    runs = []
    for admitted, tx, end, _ in services:
        if admitted < previous_admission or abs(tx - max(admitted, previous_end)) > 1:
            raise ValueError(
                "directed FIFO transmission order or work-conserving start is inconsistent"
            )
        previous_end, previous_admission = end, admitted
        if runs and tx <= runs[-1][1] + 1:
            runs[-1] = (runs[-1][0], end)
        else:
            runs.append((tx, end))

    arrivals, departures, flushes, drops, instant = (Counter() for _ in range(5))
    for admitted, tx, end, status in entries:
        if status == "queue_drop":
            drops[admitted] += 1
            continue
        if tx == admitted:
            instant[admitted] += 1
            continue
        release = tx if tx is not None else end
        arrivals[admitted] += 1
        departures[release] += 1
        if tx is None:
            flushes[release] += 1
            run = bisect_right(runs, admitted, key=lambda r: r[0]) - 1
            if release <= admitted or run < 0 or runs[run][1] < release - 1:
                raise ValueError(
                    "terminal FIFO packet could not remain queued until its reported removal"
                )
            later = bisect_right(services, admitted, key=lambda e: e[0])
            if later < len(services) and services[later][1] < release:
                raise ValueError("directed FIFO cannot skip an earlier waiting packet")

    queued = 0
    times = sorted(arrivals.keys() | departures.keys() | drops.keys())
    for time in times:
        previous = bisect_left(services, time, key=lambda e: e[1]) - 1
        ending_busy = previous >= 0 and services[previous][2] == time
        # Frame flushes precede offers. At a TX-end tie, admission versus dequeue
        # order is not recorded: permit any equal-time ordering consistent with bounds.
        upper = queued - flushes[time] + arrivals[time] + instant[time] * ending_busy
        if drops[time] and upper < capacity:
            raise ValueError("FIFO queue_drop requires enough admitted packets to fill the queue")
        queued += arrivals[time] - departures[time]
        if not 0 <= queued <= capacity:
            raise ValueError("observed FIFO waiting occupancy exceeds its configured capacity")


def _parse_network_results(directory: str | Path, context: tuple) -> dict:
    """Revalidate native observations and derive metrics without trusting summaries."""
    config, duration, interval, offers, routes, edges, source, target = context
    expected = sorted(
        (phase + seq * interval, direction, flow, seq)
        for direction in (0, 1)
        for flow, (phase, count) in enumerate(offers)
        for seq in range(count)
    )
    wire_bits = (config.packet_size_bytes + 30) * 8
    drain = max(
        (
            (wire_bits * 1_000_000_000 + row["rate_bps"] - 1) // row["rate_bps"] + row["delay_ns"]
            for trace in edges.values()
            for row in trace[:-1]
            if row["available"]
        ),
        default=0,
    )
    maximum_time = duration + drain + 1
    directory = Path(directory)
    packets = _csv_records(directory / "packets.csv", PACKET_FIELDS, len(expected))
    if len(packets) != len(expected):
        raise ValueError("packet results must contain every offered packet exactly once")
    by_packet = {}
    for index, raw in enumerate(packets):
        bounds = {
            "direction": 1,
            "flow": config.flows_per_direction - 1,
            "sequence": max(n for _, n in offers) - 1,
            "payload_bytes": 1400,
            "hop_count": 255,
        }
        record = {
            field: (
                _csv_integer(
                    value,
                    field,
                    bounds.get(field, maximum_time),
                    optional=field in ("udp_tx_time_ns", "rx_time_ns"),
                )
                if field != "status"
                else value
            )
            for field, value in raw.items()
        }
        actual = tuple(
            record[field] for field in ("offered_time_ns", "direction", "flow", "sequence")
        )
        if actual != expected[index]:
            raise ValueError("packet results must be complete, unique, and ordered by offer time")
        status, udp, rx = record["status"], record["udp_tx_time_ns"], record["rx_time_ns"]
        if status not in STATUSES or record["payload_bytes"] != config.packet_size_bytes:
            raise ValueError("invalid packet status or payload")
        route = _trace_state(routes, record["offered_time_ns"])
        suppression = {"disconnected": "no_route", "acquiring": "acquisition_suppressed"}.get(
            route["state"]
        )
        if suppression:
            if status != suppression or udp is not None or rx is not None or record["hop_count"]:
                raise ValueError("packet source suppression does not match the prepared route")
        elif status in ("no_route", "acquisition_suppressed") or udp != record["offered_time_ns"]:
            raise ValueError("packet UDP admission must match a ready source opportunity")
        successful = status in ("received", "received_after_window")
        if successful != (rx is not None):
            raise ValueError("only successful UDP deliveries may have receive times")
        if successful and (rx < udp or (status == "received") != (rx < duration)):
            raise ValueError("packet receive time does not match its window status")
        key = tuple(record[field] for field in ("direction", "flow", "sequence"))
        by_packet[key] = record
        packets[index] = record
    hops = _csv_records(directory / "hops.csv", HOP_FIELDS, MAX_HOPS)
    grouped, busy, fifo = defaultdict(list), {}, defaultdict(list)
    previous_key = (-1, -1, -1, -1)
    outages = {
        edge_id: [
            (row["time_ns"], trace[index + 1]["time_ns"])
            for index, row in enumerate(trace[:-1])
            if not row["available"]
        ]
        for edge_id, trace in edges.items()
    }
    edge_ids = {
        (trace[0]["node_a"], trace[0]["node_b"]): edge_id for edge_id, trace in edges.items()
    }
    for index, raw in enumerate(hops):
        bounds = {
            "direction": 1,
            "flow": config.flows_per_direction - 1,
            "sequence": max(n for _, n in offers) - 1,
            "hop_index": 254,
            "edge_id": 1023,
            "tx_node": 143,
            "rx_node": 143,
            "rate_bps": MAX_RATE_BPS,
            "delay_ns": 1_000_000_000,
        }
        hop = {
            field: _csv_integer(value, field, bounds.get(field, maximum_time))
            if field != "status"
            else value
            for field, value in raw.items()
        }
        key = tuple(hop[field] for field in ("direction", "flow", "sequence", "hop_index"))
        packet_key = key[:3]
        if (
            key <= previous_key
            or packet_key not in by_packet
            or hop["hop_index"] != len(grouped[packet_key])
        ):
            raise ValueError("hop IDs must be ordered, contiguous, and name an offered packet")
        previous_key = key
        edge_id = hop["edge_id"]
        if edge_id not in edges or hop["status"] not in ("received", "rx_outage_drop"):
            raise ValueError("unknown hop edge or status")
        tx, rx = hop["phy_tx_time_ns"], hop["phy_rx_time_ns"]
        packet = by_packet[packet_key]
        chain = grouped[packet_key]
        start_node, end_node = (source, target) if key[0] == 0 else (target, source)
        previous_rx = chain[-1]["phy_rx_time_ns"] if chain else packet["udp_tx_time_ns"]
        previous_node = chain[-1]["rx_node"] if chain else start_node
        if (
            previous_rx is None
            or tx < previous_rx
            or tx >= duration
            or hop["tx_node"] != previous_node
            or hop["tx_node"] == end_node
            or (chain and chain[-1]["status"] != "received")
        ):
            raise ValueError("hop chain must be causal and stop after an outage or delivery")
        route = _trace_state(routes, previous_rx)
        path = tuple(map(int, route["path"].split(";"))) if route["state"] == "ready" else ()
        if key[0] == 1:
            path = path[::-1]
        if (
            hop["tx_node"] not in path[:-1]
            or path[path.index(hop["tx_node"]) + 1] != hop["rx_node"]
        ):
            raise ValueError(
                "hop transmission does not follow the route at its forwarding decision"
            )
        state = _trace_state(edges[edge_id], tx)
        if _outage_overlap(previous_rx, tx, outages[edge_id]):
            raise ValueError("queued hop cannot survive an edge outage that flushes its queue")
        if (
            not state["available"]
            or sorted((hop["tx_node"], hop["rx_node"])) != [state["node_a"], state["node_b"]]
            or hop["rate_bps"] != state["rate_bps"]
            or hop["delay_ns"] != state["delay_ns"]
        ):
            raise ValueError("hop does not match its prepared edge state")
        expected_serialization = _nearest_serialization_ns(wire_bits, state["rate_bps"])
        serialization = rx - tx - state["delay_ns"]
        if serialization < 1 or abs(serialization - expected_serialization) > 1:
            raise ValueError("hop receive time disagrees with serialization and propagation")
        overlaps = _outage_overlap(tx, tx + serialization, outages[edge_id]) or _outage_overlap(
            rx - serialization, rx, outages[edge_id]
        )
        if (hop["status"] == "rx_outage_drop") != overlaps:
            raise ValueError("hop outage status disagrees with half-open edge availability")
        busy_key = (edge_id, hop["tx_node"])
        busy.setdefault(busy_key, []).append((tx, tx + serialization))
        fifo[busy_key].append((previous_rx, tx, tx + serialization, "transmit"))
        grouped[packet_key].append(hop)
        hops[index] = hop
    for intervals in busy.values():
        ordered = sorted(intervals)
        if any(first[1] > second[0] + 1 for first, second in pairwise(ordered)):
            raise ValueError("hop transmissions overlap on the same directed edge")
    for key, packet in by_packet.items():
        chain = grouped[key]
        if packet["hop_count"] != len(chain):
            raise ValueError("packet hop_count disagrees with actual PHY observations")
        status = packet["status"]
        destination = target if key[0] == 0 else source
        reached = bool(
            chain and chain[-1]["rx_node"] == destination and chain[-1]["status"] == "received"
        )
        if status in ("received", "received_after_window"):
            if not reached or packet["rx_time_ns"] != chain[-1]["phy_rx_time_ns"]:
                raise ValueError("UDP delivery must match the complete observed hop chain")
        elif reached or (status == "rx_outage_drop") != bool(
            chain and chain[-1]["status"] == "rx_outage_drop"
        ):
            raise ValueError("packet terminal status disagrees with its observed hop chain")
        if status in ("no_route", "acquisition_suppressed", "send_error") and chain:
            raise ValueError("suppressed or failed source send cannot have physical hops")
        if status == "route_drop":
            if not chain or chain[-1]["phy_rx_time_ns"] >= duration:
                raise ValueError("route drop requires an intermediary arrival inside the window")
            route = _trace_state(routes, chain[-1]["phy_rx_time_ns"])
            path = tuple(map(int, route["path"].split(";"))) if route["state"] == "ready" else ()
            if key[0] == 1:
                path = path[::-1]
            if chain[-1]["rx_node"] in path[:-1] and len(chain) < 255:
                raise ValueError("route drop requires missing onward route or expired TTL")
        if status in ("queue_drop", "outage_queue_drop", "end_of_window_drop", "unresolved"):
            admitted = chain[-1]["phy_rx_time_ns"] if chain else packet["udp_tx_time_ns"]
            if admitted >= duration:
                if status not in ("end_of_window_drop", "unresolved"):
                    raise ValueError("FIFO queue admission cannot occur after the window")
                continue
            node = chain[-1]["rx_node"] if chain else (source if key[0] == 0 else target)
            route = _trace_state(routes, admitted)
            path = tuple(map(int, route["path"].split(";"))) if route["state"] == "ready" else ()
            if key[0] == 1:
                path = path[::-1]
            if node not in path[:-1]:
                raise ValueError("terminal FIFO status requires an admitted onward route")
            edge_id = edge_ids[tuple(sorted((node, path[path.index(node) + 1])))]
            release = duration
            if status == "outage_queue_drop":
                spans = outages[edge_id]
                next_outage = bisect_right(spans, admitted, key=lambda span: span[0])
                if next_outage >= len(spans) or spans[next_outage][0] >= duration:
                    raise ValueError(
                        "outage queue drop requires a subsequent outage on its queued edge"
                    )
                release = spans[next_outage][0]
            elif status in ("end_of_window_drop", "unresolved") and _outage_overlap(
                admitted, duration, outages[edge_id]
            ):
                raise ValueError(
                    "terminal FIFO packet would have been flushed by an earlier outage"
                )
            fifo[(edge_id, node)].append((admitted, None, release, status))
    for entries in fifo.values():
        _validate_fifo(entries, config.queue_packets)
    metrics = [
        {
            "direction": direction,
            "flow": flow,
            **_metrics(
                [r for r in packets if r["direction"] == direction and r["flow"] == flow],
                config.packet_size_bytes,
                duration,
            ),
        }
        for direction in (0, 1)
        for flow in range(config.flows_per_direction)
    ]
    return {
        "flow_metrics": metrics,
        "aggregate": _metrics(packets, config.packet_size_bytes, duration),
    }
