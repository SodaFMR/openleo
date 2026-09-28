# Packet replay with ns-3

`openleo packet-replay` consumes one verified ground-link experiment and runs
synthetic UDP traffic through an optional, separately built ns-3.48 backend.
The experiment uses real ns-3 IPv4/UDP sockets, point-to-point devices, event
scheduling and finite DropTail device queues. It does not simulate TCP, multi-hop
constellation routing, a satellite MAC, RF decoding or real operator traffic.

## Start

Build and check the pinned backend using the
[adapter instructions](https://github.com/SodaFMR/openleo/blob/main/adapters/ns3/README.md).
Its source is in the Git repository, not in the MIT Python distributions; it is not
a Python dependency.
Then generate a normal workbench bundle and select a station and satellite:

```bash
uv run --no-dev openleo constellation \
  examples/constellations/iridium_global_reference.json --output runs/packet-source
uv run --no-dev openleo packet-replay runs/packet-source \
  --station Madrid --norad 42960 --start-offset-s 0 --duration-s 300 \
  --offered-load-bps 100000 --packet-size-bytes 512 --queue-packets 32 \
  --backend ../ns-3.48/build/scratch/ns3.48-openleo-replay --output runs/packets
```

The backend argument is a **trusted local executable** that the command runs.
Only use a backend built from reviewed sources. Its protocol/library identity and
binary hash are recorded, but a version string or checksum does not authenticate
an arbitrary executable. The command does not expose backend execution through
the local web application.

The input bundle's hashes and structure are checked before replay. A missing
selected link is unavailable, not a link carried forward from an earlier pass.
The requested window must lie wholly within the source experiment. Inputs and
the original source bundle remain unchanged; completed outputs are published
atomically and existing nonempty output directories are refused.

## Time and service model

Source states are held constant on `[t_i, t_(i+1))`. A non-sample start uses the
state at the most recent preceding source sample. The final trace row defines the
exclusive stop and has no duration. No intermediate orbit or RF values are invented.

The backend uses nanosecond time and integer bit/s service rates. Positive source
rates are rounded down to whole bit/s; propagation delays are rounded to the nearest
nanosecond. Rate and delay quantization is recorded. Rates below 1 bit/s and settings
that would serialize a whole packet in less than one nanosecond are rejected.
Source intervals round upward to whole nanoseconds so the effective offered load
does not exceed the requested value; both loads are reported.

Each direction is an independent full-duplex link with the same declared rate and
delay trace. This reciprocal model is not an independently modeled physical uplink.
Every source opportunity before the stop gets exactly one packet record. There is
no stochastic traffic or extra random loss model; the seed is recorded for the
ns-3 execution context.

- State changes take effect before packet events at the same timestamp.
- A packet captures serialization rate and propagation delay at actual PHY
  transmission start. Later rate/delay changes do not reschedule that packet.
- New UDP admission is suppressed during an outage. On outage onset, packets
  still waiting in the device queue are dropped and counted separately.
- A transmitted packet is corrupted if its TX serialization interval or RX bit
  interval intersects a declared outage. Intervals are half-open. A fade confined
  to the propagation-only gap does not erase a packet.
- The device FIFO is the only modeled queue: native `DisableFlowControl()` avoids
  both an additional queue discipline and hidden pre-device flow-control drops.
- At the stop, source traffic stops and waiting packets are discarded as
  `end_of_window_drop`. These are censored by the experiment window, not reported
  as ordinary network losses. Already-started packets may finish during draining.

Unknown channel conditions after the stop are not extrapolated into new outages.
After-window deliveries and unresolved records are reported separately, and neither
extends the measurement duration or contributes to within-window goodput.

## Payload, framing and metrics

`packet_size_bytes` is the UDP payload size, including a 12-byte ns-3 sequence/time
measurement header. Each packet additionally serializes 8 UDP, 20 IPv4 and 2 PPP
header bytes. IPv6 and fragmentation are outside this experiment.

For a constant 1 Mbit/s link, 970 payload bytes mean 1,000 wire bytes. With a
2 ms propagation delay and no queue, one-way UDP send-to-receive delay is therefore
10 ms. The executable analytical checks test this literal reference independently
of the Python wrapper.

Per direction, within-window UDP payload goodput is

$$
G=\frac{8\,B_{\mathrm{received\ within\ window}}}{T_{\mathrm{window}}}.
$$

It is **simulated payload goodput**, not measured commercial throughput. Delay
statistics refer only to successfully delivered packets inside the window and
include queue waiting time. They are one-way delays, not RTT. They can be biased
by delivery/window selection; after-window and censored counts must be considered
alongside them. No generic loss percentage hides censored outcomes.

## Artifacts

| File | Meaning |
| --- | --- |
| `link-trace.csv` | Left-held integer ns/bit/s inputs, including unavailable states |
| `packets.csv` | One record for every offered packet in both directions |
| `packet-summary.json` | Configuration, source/backend fingerprints, quantization, counts, goodput, delay and limitations |
| `manifest.json` | Hashes of the other three artifacts |

Packet fields are `direction`, `sequence`, `offered_time_ns`, `udp_tx_time_ns`,
`phy_tx_time_ns`, `rx_time_ns`, `status`, and `payload_bytes`. Absent timestamps
are empty CSV fields. `rx_time_ns` also records physical completion of an
`rx_outage_drop`; successful delivery is determined by status, not time alone.

Status distinguishes `received`, `received_after_window`, `outage_suppressed`,
`queue_drop`, `outage_queue_drop`, `end_of_window_drop`, `rx_outage_drop`,
`send_error`, and `unresolved`. The Python reader checks identity, ordering,
causality, status/state relationships and packet accounting before summarizing.

Supported limits are a window of at most one hour, no more than 200,000 offered
packets total, 64–1,400 payload bytes, 1–10,000 waiting packets per queue,
1–1,000,000,000 requested payload bit/s, at most 4,096 trace rows, service rates
up to 10^12 bit/s, and one-way delays up to one second. These are implementation
bounds, not physical accuracy claims.

## Evidence and limits

The backend checks constant underload, overload, exact queue capacity, complete
outages, TX/RX fade intersections, recovery, changes during transmission,
same-time event ordering, short windows, draining, deterministic repetition and
invalid inputs against the actual compiled simulator. The regression traces are
deliberately analytical/synthetic; the example's orbit inputs remain archived GP
records and its RF/traffic values remain declared assumptions.

The integration pins [ns-3.48](https://gitlab.com/nsnam/ns-3-dev/-/tree/ns-3.48),
commit `d2add90b452d600cfb4859baed8e9ea633519447`. Its timing contract follows the
[point-to-point device](https://gitlab.com/nsnam/ns-3-dev/-/blob/ns-3.48/src/point-to-point/model/point-to-point-net-device.cc)
and [channel](https://gitlab.com/nsnam/ns-3-dev/-/blob/ns-3.48/src/point-to-point/model/point-to-point-channel.cc)
implementations. The separately built adapter is GPL-2.0-only; the Python core is MIT.
