# Optional multi-hop packet replay

The Python network bridge turns a verified OpenLEO workbench bundle into a
bounded input trace for the optional native ns-3.48 backend. Packet timing,
forwarding, shared device FIFO queues and UDP delivery come from the executable.
Python independently validates the packet and physical-hop CSV observations
before publishing metrics. It does not substitute model estimates when the
backend is unavailable.

```python
from openleo.network_replay import NetworkReplayConfig, run_network_replay

summary = run_network_replay(
    "workbench-bundle",
    NetworkReplayConfig(
        routing_model="minimum_delay",
        duration_s=60,
        offered_load_bps=100_000,
        flows_per_direction=2,
        acquisition_delay_s=0.5,
    ),
    backend="/path/to/openleo-network-replay",
    output_dir="network-evidence",
)
print(summary["aggregate"]["goodput_bps"])
```

Build instructions are in [the ns-3 adapter README](https://github.com/SodaFMR/openleo/blob/main/adapters/ns3/README.md).
The executable is explicitly selected and trusted: its version handshake and
SHA-256 identify the binary but do not sandbox or attest its authorship.
Execution uses an argument list without a shell, a ten-second version handshake
timeout and a 120-second replay timeout.

## Configuration and selection

`NetworkReplayConfig` is frozen. Defaults are `routing_model="minimum_delay"`,
`start_offset_s=0`, `duration_s=60`, `offered_load_bps=100000`,
`packet_size_bytes=512`, `queue_packets=32`, `flows_per_direction=1`,
`acquisition_delay_s=0`, and `seed=1`. The route model must be `minimum_delay`,
`maximum_rate` or `fixed_capacity`; source and target are the scenario's declared
stations.

`prepare_network_replay(document, config)` validates the portable document and
returns `edges`, `routes` and `metadata`. The window must lie inside the source
experiment. Geometric states are held from the latest source sample at or before
each boundary. Only edges used by selected routes during the window belong to
the replay topology. ISL availability and delay come from network frames; ground
rates come from link frames, or from the declared fixed-capacity baseline for
`fixed_capacity`. Only the selected ground edge at each endpoint is available.

Initial acquisition, an endpoint satellite-pair change, or recovery after route
loss incurs `acquisition_delay_s`. Both selected ground edges remain unavailable
while acquiring. An internal path change with the same endpoint satellites
retains the pending completion time or existing readiness. Completion boundaries
are explicitly inserted without advancing the underlying geometric sample.
The terminal stop is exclusive and disables all edges.

## Traffic and numeric bounds

Offered load is payload bits per second **per flow**, with 1–4 flows in each of
two reciprocal endpoint directions. Each flow uses
`ceil(packet_size_bytes * 8e9 / offered_load_bps)` nanoseconds between offers.
Flow `f` starts at `floor(f * interval_ns / flows_per_direction)`; an offer at the
window end is excluded. The payload includes a 12-byte measurement header and
each physical transmission serializes 30 additional PPP, IPv4 and UDP bytes.

| Input | Bound |
|---|---|
| Duration | Positive, at most 3600 seconds |
| Start, duration, acquisition | Whole microseconds; start nonnegative |
| Acquisition delay | 0–60 seconds |
| Offered load per flow | 1–1,000,000,000 bit/s |
| Payload | 64–1400 bytes |
| Device queue | 1–10,000 packets |
| Seed | 1–2,147,483,647 |
| Nodes / edges / frames | At most 144 / 1024 / 4096 |
| Edge-state rows | At most 200,000 |
| Edge rate / delay | 1–1,000,000,000,000 bit/s when available / 0–1 second |
| Total offers / budgeted hops | At most 200,000 / 500,000 |
| Each result CSV | At most 64,000,000 bytes |

Rates are floored to integer bit/s and delays rounded to nearest nanoseconds.
Metadata reports the largest rate and delay errors, the ceiling interval rule,
effective load, phased offer counts and exact measurement duration. The hop
budget uses total offers times the largest selected route length; the native
backend also caps actual transmissions at 500,000. Native IPv4 TTL 255 bounds
loops introduced by subsequent route updates; TTL failure is a `route_drop`.

## Native CSV protocol and validation

The exact version response is `openleo-ns3-network/1 ns-3.48` followed by a
newline. Input headers are:

```text
time_ns,edge_id,node_a,node_b,rate_bps,delay_ns,available
time_ns,state,path
```

Every edge appears at each route time, with stable integer IDs and node pairs.
The first time is zero, times strictly increase, and the last route is `stop`.
Routes use `ready`, `disconnected`, `acquiring` or terminal `stop`; only `ready`
has a semicolon-separated source-to-target simple path. A disconnected window
may have a header-only edge table.

Output headers are:

```text
direction,flow,sequence,offered_time_ns,udp_tx_time_ns,rx_time_ns,status,payload_bytes,hop_count
direction,flow,sequence,hop_index,edge_id,tx_node,rx_node,phy_tx_time_ns,phy_rx_time_ns,rate_bps,delay_ns,status
```

Packet rows are ordered by `(offered_time_ns, direction, flow, sequence)`.
Hop rows are ordered by `(direction, flow, sequence, hop_index)`, with zero-based
contiguous hop indices. Optional packet timestamps are empty; `rx_time_ns` is
present only for successful UDP delivery. A failed physical reception still has
its actual `phy_rx_time_ns` in the hop table.

Packet statuses are `received`, `received_after_window`, `no_route`,
`acquisition_suppressed`, `queue_drop`, `outage_queue_drop`, `rx_outage_drop`,
`route_drop`, `end_of_window_drop`, `send_error` and `unresolved`. Hop statuses
are `received` and `rx_outage_drop`.

`parse_network_results(directory, prepared)` revalidates the prepared input and
both CSV tables: exact headers and bounds, complete offer identity and ordering,
source suppression, causal hop chains, route and edge membership, captured rates
and delays, serialization and outage timing, directed-edge transmission overlap,
hop counts and successful end-to-end delivery. A queued packet retains the route
selected at its previous hop's receive time (or initial UDP send); rate and delay
are sampled at actual PHY transmission. Physical outage intervals are half-open
and an outage flushes packets already waiting in the affected device queue.
Route updates can change forwarding at subsequent arrivals, including revisits.
The parser derives metrics from validated observations, without trusting an
external aggregate summary.

Directed FIFO reconciliation uses the observed PHY services and inferred IP
admissions. Each service must begin at the later of admission and the previous
service's end, with nanosecond rounding tolerance; services preserve admission
order. A queue overflow requires enough observed waiting admissions to fill the
configured capacity. A packet reported waiting until an outage or stop requires
continuous competing service throughout that wait and cannot be skipped by a
later arrival. An unresolved observation requires physically supported censoring,
so it cannot replace an uncontended packet's missing transmission.

CSV timestamps do not record the order of equal-time offer, arrival and dequeue
callbacks. Capacity and overflow checks therefore permit any equal-time ordering
consistent with the observed before/after queue bounds. This ambiguity does not
permit idle service time, over-capacity persistent queues or impossible losses.

## Published evidence and metrics

`run_network_replay(bundle_dir, config, backend, output_dir)` returns a
`kind="openleo.network-replay"`, `schema_version="1"` summary and publishes:

- `edges.csv` and `routes.csv`: exact prepared input.
- `packets.csv` and `hops.csv`: validated native observations.
- `network-summary.json`: configuration, software, source fingerprints, backend
  identity, selection, window, quantization, traffic, `flow_metrics`, `aggregate`
  and model limitations.
- `manifest.json`: SHA-256 hashes of those five artifacts.

Each flow entry includes `direction`, `flow`, offered/admitted/received counts,
counts for every terminal status, `censored_packets`, payload `goodput_bps`,
`mean_delay_s` and `max_delay_s`. The aggregate has the same metrics across all
flows. Goodput includes payload successfully delivered inside the measurement
window. Delay is one-way from UDP send to UDP delivery for those packets only;
absent delivery gives null delay metrics. After-window deliveries,
end-of-window drops and unresolved observations are reported separately as
censored packets rather than silently entering goodput or latency.

Only a new or empty output directory is accepted. Failed execution or validation
does not publish partial evidence, and existing nonempty results are preserved.
Input traces and the executable are checked again before atomic publication.

The topology is synthetic, reciprocal and full duplex. Geometric routing and
endpoint acquisition assumptions are declared models; they do not represent
radio scheduling, uplink hardware, TCP behavior or measured network performance.

For optional native integration tests, set `OPENLEO_NS3_NETWORK` to the compiled
backend and run `pytest tests/test_network_replay.py`.
