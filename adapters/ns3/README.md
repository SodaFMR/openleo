# Optional ns-3 packet replay backend

This adapter runs a two-node, full-duplex UDP experiment using real ns-3 devices,
queues and scheduling. It consumes one OpenLEO ground-link trace. It is not a
constellation MAC/PHY, TCP simulation or reconstruction of operator traffic.

The backend source in this directory is GPL-2.0-only; see [LICENSE](LICENSE).
OpenLEO's Python core remains MIT licensed. ns-3 is downloaded and built separately;
neither its source tree nor compiled libraries are bundled with OpenLEO.

## Build

Use a C++23-capable compiler, Python, CMake and Ninja supported by
[ns-3's installation guide](https://www.nsnam.org/docs/installation/html/index.html).
The pinned backend is ns-3.48, commit
`d2add90b452d600cfb4859baed8e9ea633519447`.

From the OpenLEO repository root, on Linux or macOS:

```bash
git clone --depth 1 --branch ns-3.48 \
  https://gitlab.com/nsnam/ns-3-dev.git ../ns-3.48
git -C ../ns-3.48 rev-parse HEAD
cp adapters/ns3/openleo-replay.cc ../ns-3.48/scratch/openleo-replay.cc
cp adapters/ns3/replay-common.h ../ns-3.48/scratch/replay-common.h
cd ../ns-3.48
./ns3 configure --build-profile=release \
  --enable-modules='core;network;internet;point-to-point;applications' \
  --enable-build-version --enable-examples --enable-tests \
  --disable-python-bindings --disable-precompiled-headers \
  --disable-gtk --disable-gsl --disable-werror
./ns3 build openleo-replay -j 4
build/scratch/ns3.48-openleo-replay --PrintVersion
```

The commit must match the value above. The last command must print
`openleo-ns3-replay/1 ns-3.48`; it checks the linked library version. Keep the
compiled binary in its ns-3 build tree so its shared libraries remain available.
Some platforms add an executable suffix. Do not substitute an unrelated program.

For Windows, build and run the optional backend and its Python caller inside WSL2.
The core OpenLEO application does not require WSL or ns-3. A native Windows ns-3
build is not covered by this integration. Do not install into the system Python.

## Check and use

Back in the OpenLEO directory:

```bash
uv run --no-dev python tests/ns3_replay_checks.py \
  ../ns-3.48/build/scratch/ns3.48-openleo-replay
uv run --no-dev openleo constellation \
  examples/constellations/iridium_global_reference.json --output runs/packet-source
uv run --no-dev openleo packet-replay runs/packet-source \
  --station Madrid --norad 42960 --duration-s 300 \
  --backend ../ns-3.48/build/scratch/ns3.48-openleo-replay \
  --output runs/packets
```

Outputs and the precise event, queue, outage and measurement-window semantics are
documented in [Packet replay](../../docs/PACKET_REPLAY.md). There is no automatic
download, compilation or fallback simulator in the Python command.

## Multi-hop network replay

`openleo-network-replay.cc` is a separate GPL-2.0-only backend. It builds the
provided graph using native ns-3 IPv4 static host routes, full-duplex point-to-point
devices, UDP endpoint sockets and one DropTail device FIFO per directed edge.
Flow control is disabled so there is no second traffic-control queue. The original
two-node executable and its protocol are unchanged.

Using the same pinned, configured ns-3 tree above:

```bash
cp adapters/ns3/openleo-network-replay.cc ../ns-3.48/scratch/openleo-network-replay.cc
cp adapters/ns3/replay-common.h ../ns-3.48/scratch/replay-common.h
cd ../ns-3.48
./ns3 build openleo-network-replay -j 4
build/scratch/ns3.48-openleo-network-replay --PrintVersion
```

The exact handshake is `openleo-ns3-network/1 ns-3.48`. Run the independent
analytical checks from the OpenLEO repository:

```bash
uv run --no-dev python tests/ns3_network_checks.py \
  ../ns-3.48/build/scratch/ns3.48-openleo-network-replay
```

The standalone invocation requires all eleven arguments:

```bash
../ns-3.48/build/scratch/ns3.48-openleo-network-replay \
  --edges=edges.csv --routes=routes.csv --output=network-output \
  --source=0 --target=2 --nodeCount=3 --intervalNs=30000000 \
  --packetSize=970 --queuePackets=32 --flows=1 --seed=1
```

Input headers are exact, unquoted ASCII CSV:

```text
time_ns,edge_id,node_a,node_b,rate_bps,delay_ns,available
time_ns,state,path
```

Each route time has every edge state once. Times begin at zero, increase strictly,
and end with an exclusive terminal `stop`. Edge IDs and endpoint pairs are stable;
parallel edges and self-loops are rejected. `ready` paths contain semicolon-separated,
unique node IDs from source to target, using available edges. `disconnected`,
`acquiring`, and `stop` paths are empty. A header-only edge file is accepted when
every route is inactive; route frames still define the window.

Bounds are 2..144 nodes, 0..1024 edges, 2..4096 frames, 200000 edge-state rows,
3600 seconds, available rate 1..1e12 bit/s (zero only when unavailable), and delay
0..1 second. Payloads are 64..1400 bytes, waiting queues 1..10000 packets, flows
1..4 per direction, intervals 1..INT64_MAX nanoseconds, and seeds 1..INT32_MAX.
The route file is limited to 4 MiB and the edge file to 32 MiB. Validation precedes
topology/output creation. Offered packets are limited to 200000, and offered
packets times the longest selected path (at least one) to 500000. A runtime cap
also rejects more than 500000 actual PHY transmissions, including route-change loops.

For each direction and flow, opportunities occur at
`floor(flow * intervalNs / flows) + sequence * intervalNs`, strictly before stop.
Payload size includes the 12-byte `SeqTsHeader`; UDP, IPv4 and PPP add 30 wire bytes.
Queues are shared between flows. The initial IPv4 TTL is 255 to support a simple
143-hop path and bound route-change loops; native TTL expiry is `route_drop`.
This UDP-only experiment suppresses ICMP transmission using its native L4 callback,
so control traffic does not enter the measured queues.

At a boundary, physical states and host routes update atomically before source
events or transmissions. IPv4 selects a next hop when admitting a packet to its
outgoing device queue; a later table change does not reroute that queued packet.
The next intermediary arrival uses the then-current route. Missing forwarding
routes produce `route_drop`; new offers are `no_route` or
`acquisition_suppressed`. This distinction matters when validating hops: routes
are captured at queue admission, while rate and delay are captured at PHY TX.

Outages flush waiting packets on that edge. A transmitted packet is corrupt if
either its captured half-open TX bit interval or RX bit interval intersects a
declared outage; a fade solely in the propagation gap is harmless. Stop flushes
all waiting packets, removes forwarding routes, and ends source offers. Already
transmitted final-hop packets may deliver as `received_after_window`; intermediary
arrivals cannot start another hop and become `end_of_window_drop`. The drain is
one maximum admitted edge serialization plus its propagation delay, with a one
nanosecond scheduler margin. The terminal edge state does not invent an outage
beyond the measurement window.

The output directory contains these exact tables:

```text
packets.csv:
direction,flow,sequence,offered_time_ns,udp_tx_time_ns,rx_time_ns,status,payload_bytes,hop_count
hops.csv:
direction,flow,sequence,hop_index,edge_id,tx_node,rx_node,phy_tx_time_ns,phy_rx_time_ns,rate_bps,delay_ns,status
```

Packet rows are sorted by `(offered_time_ns,direction,flow,sequence)`, and hop rows
by `(direction,flow,sequence,hop_index)`. Direction zero runs source to target;
direction one reverses it. Times are real observed ns-3 timestamps; missing times
are empty. Packet `rx_time_ns` exists only for successful UDP delivery. Hop receive
times also exist for corrupted receptions. `hop_count` counts actual transmissions,
including a corrupt last hop. Hop statuses are `received` or `rx_outage_drop`;
packet statuses are `received`, `received_after_window`, `no_route`,
`acquisition_suppressed`, `queue_drop`, `outage_queue_drop`, `rx_outage_drop`,
`route_drop`, `end_of_window_drop`, `send_error`, or `unresolved`.
