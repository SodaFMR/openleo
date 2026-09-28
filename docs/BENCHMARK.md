# Frozen release benchmark

`examples/studies/release_benchmark.json` fixes the matrix before interpreting results:
12 and 20 GHz, 30 and 60 s sampling, gas-layer refinements 1 and 2, and UDP loads
10,000 and 300,000 bit/s **per flow**. Each 300 s window starts at the source
scenario's start. Packets contain 1,200 payload bytes, queues hold eight packets,
two flows run in each direction, acquisition takes 0.1 s, and the seed is 1.
The four global stations, rain/cloud layers, gains, EIRP and receiver noise are
declared controlled inputs; they are not measured weather or calibrated hardware.
Gain and EIRP stay identical across frequency cases.

```python
from openleo.benchmark import run_benchmark

summary = run_benchmark(
    "examples/studies/release_benchmark.json",
    "runs/release-benchmark",
    network_backend="build/openleo-network-replay",
)
```

The backend must be the trusted, compiled OpenLEO ns-3 network adapter. It is
optional and is never downloaded or selected from a configuration field. Full
release evidence requires it: 16 experiments, three aligned propagation variants
per experiment, and three routing models per variant (144 native replays).
Without a backend, the runner computes eight propagation experiments, collapses
the ignored load dimension, sets each run's `offered_load_bps` to `null`, and marks
`packet_evidence` as `missing`. There are no synthetic packet or placeholder rows.
Model-only experiment settings use one flow and zero acquisition delay.

The API accepts a configuration path and a new or empty output directory. JSON
must be UTF-8, at most 1 MB, have no duplicate keys and exactly the documented
fields. Matrix lists must be nonempty and unique, with at most two frequencies,
three sampling steps, two refinements and two loads; at most 24 experiments are
allowed. Frequencies must fit the shared gas/cloud domain (1–200 GHz), refinements
are 1, 2 or 4, times have microsecond resolution, and existing scenario, packet,
sample and traffic budgets apply. Every matrix variant and the experiment window
is validated before outputs. Gases and declared hydrometeors are required.

`benchmark.json` includes frozen configuration, source fingerprints, software and
runtime environment, child experiment summaries, scalar metrics with explicit
units, aligned effects and an uncertainty inventory. `metrics.csv` has one real
station, route or packet scalar per row; packet scalars come from the native
`flow_metrics`, preserving direction and flow identity. `index.html` is an offline
overview with SVG reference-rate bars, numerical tables and links to each child
report. Each child under `cases/<stable-id>` retains its frozen inputs and native
packet evidence. Root `inputs/` freezes the source scenario, orbit and a portable
benchmark configuration. `manifest.json` binds every artifact by SHA-256.
Failed runs never publish partial results or replace user files; nonempty outputs
and symbolic links are refused.

Effects compare two computed results while matching all other matrix parameters,
propagation variant, metric, station, routing model, direction and flow as
applicable. Time-step and layer-refinement comparisons are numerical sensitivity
checks. Propagation comparisons include gases against free space and declared
hydrometeors against gases. Packet comparisons include fixed capacity against
minimum delay. Absolute effect is actual minus baseline in the metric's units;
relative effect is that difference divided by baseline, dimensionless, and `null`
for a zero baseline. Zero effects and pair case counts remain visible. Null
delivered-delay values cannot form an effect and remain null in metric rows.
No hypothesis tests, confidence claims or uncertainty bars are inferred.

`numerical_metrics_sha256` fingerprints only deterministic scalar CSV bytes.
Per-run wall times and runtime environment are diagnostic and excluded from
numerical reproducibility comparisons. Bundle manifests deliberately fingerprint
the exact bytes, including diagnostics, and may differ between repetitions.

The uncertainty inventory distinguishes fitted GP orbital inputs without
covariance; unsurveyed declared station coordinates; fixed RF/noise inputs;
uniform rain/cloud sensitivity inputs; gas-grid and sampling effects; absent
MAC/PHY/interference; and synthetic packet-control assumptions. Units, evidence
class, quantified effects and unknowns are explicit. Numerical convergence does
not establish measured physical accuracy, and simulated receive-event goodput
does not establish commercial service performance.

Run the small model tests with `pytest tests/test_benchmark.py`. Set
`OPENLEO_NS3_NETWORK` to the compiled network adapter to include a real native
packet integration check. The complete frozen release matrix is intended as an
explicit release run, rather than a routine unit-test workload.
