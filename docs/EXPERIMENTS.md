# Integrated experiments

`openleo experiment` recomputes aligned propagation variants and can replay
their links or routes through separately built native ns-3.48 executables.
It reuses constellation, atmospheric, routing, and packet implementations;
model-only runs do not invent packet results.

## Command and settings

From the repository root:

```bash
uv run --no-dev openleo experiment \
  examples/constellations/iridium_global_hydrometeors.json \
  --duration-s 300 --output runs/comparison
```

A settings file contains experiment values, never backend or output paths:

```json
{
  "packet_mode": "network",
  "start_offset_s": 0,
  "duration_s": 300,
  "offered_load_bps": 10000,
  "packet_size_bytes": 1200,
  "queue_packets": 8,
  "flows_per_direction": 2,
  "acquisition_delay_s": 0.1,
  "seed": 1
}
```

Save this as `settings.json`, then run:

```bash
uv run --no-dev openleo experiment \
  examples/constellations/iridium_global_hydrometeors.json \
  --settings settings.json \
  --network-backend ../ns-3.48/build/scratch/ns3.48-openleo-network-replay \
  --output runs/comparison-network
```

Explicit CLI values override settings-file values. Unknown settings, duplicate
JSON keys, invalid domains, and non-finite values are rejected.
Build the executables using the
[adapter instructions](https://github.com/SodaFMR/openleo/blob/main/adapters/ns3/README.md).

| Setting | Default and meaning |
| --- | --- |
| `packet_mode` | `none`; also `link` or `network` |
| `station_name`, `norad_id` | Required selection for `link`; CLI uses `--station` and `--norad` |
| `start_offset_s` | 0; offset from the original scenario start |
| `duration_s` | 60; positive, at most 3600 s |
| `offered_load_bps` | 100000; payload bit/s per flow |
| `packet_size_bytes` | 512; 64–1400 bytes, including the measurement header |
| `queue_packets` | 32; 1–10000 waiting packets per device queue |
| `flows_per_direction` | 1; 1–4, multiple flows require `network` |
| `acquisition_delay_s` | 0; 0–60 s, a nonzero value requires `network` |
| `seed` | 1; integer 1–2147483647 |

Start, duration, and acquisition values must have whole-microsecond precision.
The selected window must fit wholly inside the original scenario and span at
least one declared sampling interval. This applies even when packets are
disabled. All variants recompute that same window, rather than averaging
different original time ranges. Existing constellation, sample, traffic, and
native hop budgets also apply.

## Aligned cases

A free-space case always exists. A scenario with reference propagation adds a
gas-only `reference` case. A scenario with declared hydrometeors also adds a
`hydrometeors` case with the original gas, rain, and cloud inputs. Sampling,
station geometry, RF settings, and network assumptions are held constant.

Hydrometeor declaration with zero water/rain still produces schema 3 and its
own case; physical link values reproduce gas-only results. Coupling uses
explicitly declared uniform states, not inferred weather or P.618 statistics.
See [Coupled propagation](COUPLED_PROPAGATION.md).

Station comparisons report changes in mean best reference rate and RF outage
duration against free space. Route comparisons report changes in mean
bottleneck rate and connectivity duration. These are deterministic deltas,
not uncertainty estimates or tests of universal routing superiority.

In `link` mode, configure `--packet-backend`; each case replays the selected
station/satellite pair. In `network` mode, configure `--network-backend`;
each case replays all three routing models separately. Simultaneous flows share
actual directed device queues within each replay. Selected route edges form
the topology; endpoint acquisition, atomic updates, queue overflow, outages,
and measurement censoring use the [network service model](NETWORK_REPLAY.md).
Reference bottlenecks and simulated payload goodput are separate quantities.

A single routing model can also be replayed from a completed workbench bundle:

```bash
uv run --no-dev openleo network-replay runs/global \
  --routing-model minimum_delay --duration-s 300 \
  --backend ../ns-3.48/build/scratch/ns3.48-openleo-network-replay \
  --output runs/network
```

## Portable outputs

| Artifact | Contents |
| --- | --- |
| `experiment-study.json` | Schema 1 `openleo.experiment`: configuration, cases, comparisons, provenance, limitations |
| `stations.csv`, `routes.csv` | Per-case model metrics with units |
| `index.html` | Offline report, charts, tables, links to child evidence |
| `inputs/scenario.json` | Portable source scenario retaining the original time window |
| `inputs/settings.json` | Exact effective experiment controls |
| `inputs/orbit.csv` | Archived catalog bytes, verified against source provenance |
| `cases/<case>/` | Derived window/configuration, complete workbench, optional native packet bundles |
| `manifest.json` | SHA-256 fingerprints for recursively exported artifacts |

The original source scenario hash identifies its input bytes (or canonical
configuration for browser edits). Portable input files and each normalized
derived case have their own hashes; rewritten relative paths need not preserve
the original scenario hash. Orbit bytes remain unchanged. A manifest binds
artifacts, not the correctness of the physical assumptions.

Only a new or empty output directory is accepted. Results are staged and
published after successful computation and validation; existing nonempty
results and symbolic links are refused.

## Local study interface

Start `openleo app` with trusted `--packet-backend` and/or
`--network-backend` flags. The workbench exposes declared station/hydrometeor
controls and experiment settings, renders comparisons, and exports a complete
ZIP with inputs, child bundles, reports, and manifests. Without a configured
backend, packet controls reflect the missing capability.

The HTTP application binds only to IPv4 loopback, pins the source catalog,
validates Host and Origin, and requires its session token for simulation and
study POST requests. A rolling cap of 60 requests per second applies to all
requests with matching local headers. JSON request bodies are at most 1 MB;
uncompressed study artifacts are at most 64 MB. The latest successful study
replaces the in-memory study cache. Failed requests preserve prior results.
Executable and filesystem output paths cannot be supplied by the browser.

For larger runs and durable repeated exports, use the CLI. For the frozen
matrix and numerical reference requirements, see [Reproducibility](REPRODUCIBILITY.md).
