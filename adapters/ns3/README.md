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
