# Reproducing numerical and native packet evidence

The release workflow uses committed scientific inputs and the separately built OpenLEO ns-3.48
network backend. It requires a passing published numerical TEME reference and actual native
packet results; a model-only run cannot satisfy it.

## Setup and one-command reproduction

Install Python 3.12 or newer and the locked environment:

```bash
uv sync --locked --no-dev
```

Build the pinned native network executable with the [adapter instructions](../adapters/ns3/README.md)
and keep it in its ns-3 build tree so that its shared libraries remain available. Linux and
macOS are supported; on Windows use WSL2 for both the backend and its caller. Then, from the
repository root:

```bash
uv run --no-dev openleo reproduce \
  examples/reproduce/release_benchmark.json \
  --reference examples/validation/vallado_06251.json \
  --network-backend ../ns-3.48/build/scratch/ns3.48-openleo-network-replay \
  --output runs/reproduction
```

The output directory must not exist. `reproduce` verifies the published orbit reference, runs the
frozen sweep with native network replays, requires actual packet evidence for every run with no
unresolved packets or source send errors, and only then publishes everything atomically. A missing
or untrusted executable, a failing orbit reference, a spec without a network replay, or invalid
native evidence publishes nothing.

The [frozen configuration](../examples/reproduce/release_benchmark.json) is a sweep spec:

| Dimension | Values |
| --- | --- |
| Base scenario | `examples/constellations/iridium_global_hydrometeors.json` (reference gases plus declared rain and cloud) |
| Frequency | 12 and 20 GHz |
| Sampling step | 30 and 60 s |
| Gas-layer refinement | 1 and 2 |
| Window | the first 300 s of the base scenario |
| UDP load | 10,000 and 300,000 payload bit/s per flow |
| Traffic | 1200-byte payloads, two flows per direction |
| Queues, acquisition, seed | eight packets, 0.1 s, 1 |
| Routing models | minimum delay, maximum rate, fixed capacity (the base scenario's) |

The grid yields 8 runs; each replays the Madrid-Tromso pair for 3 routing models at 2 loads, so
48 native replays are expected. These are expected counts from the configuration, not a statement
that a particular release has executed them; `release-evidence.json` reports the actual counts.
The aligned propagation cases of v1's benchmark (free space, gases, gases plus declared
hydrometeors) are the separate sweep `examples/sweeps/propagation_cases.json`.

The two frequencies keep identical declared gains, EIRP, bandwidth and receiver noise; this is not
a fixed-aperture or frequency-specific hardware comparison. Rain and cloud states and station
locations are controlled assumptions, not observations.

## Evidence directory

- `release-evidence.json` (kind `openleo.release-evidence`, schema `2`): completed runs, native
  replay count, the sweep manifest's SHA-256, the orbit reference's SHA-256 and verdict,
  `numerical_metrics_sha256` (the SHA-256 of `sweep/comparisons.csv`), software versions and
  limitations;
- `orbit-verification.json`: residuals, tolerances, units, frame, epoch convention, hashes;
- `inputs/orbit-reference.json` and `inputs/configuration.json`: the exact inputs;
- `sweep/`: the sweep's own layout (`sweep.json`, `comparisons.csv`, `runs/<id>/` results
  directories with their archived inputs, `replays/<id>/network/<model>/<load>/` native
  evidence, `manifest.json`);
- `manifest.json`: SHA-256 of every file.

`numerical_metrics_sha256` identifies the deterministic comparison values. Manifests bind exact
bytes, including software versions and platform strings, and can differ between numerically
identical repetitions on different machines; compare the numerical digest and the recorded
versions and inputs when assessing reproducibility. Across platforms, numeric tables agree within
the tolerances of [Validation](VALIDATION.md), not bit for bit.

## Separate checks and model-only work

```bash
uv run --no-dev openleo verify orbit examples/validation/vallado_06251.json
```

prints JSON and succeeds only when every TEME residual passes. Its expected states come from
Vallado case 06251; the comparison shares SGP4 theory and implementation lineage, covers one
near-Earth moderate-drag case, and is not observational orbit or RF validation. See
[Orbit verification](ORBIT_VERIFICATION.md).

The same matrix runs without native evidence as an ordinary sweep (a kind whose backend is not
given is not replayed, and `sweep.json` records the evidence as missing):

```bash
uv run --no-dev openleo sweep examples/sweeps/release_matrix.json --output runs/release-matrix
```

The native analytical checks are independent of the release matrix:

```bash
uv run --no-dev python tests/ns3_network_checks.py \
  ../ns-3.48/build/scratch/ns3.48-openleo-network-replay
```

## Interpretation

Reproducible results are evidence of a specified computation. Hash equality, published numerical
reference agreement and queue accounting do not establish calibrated received power, local-weather
accuracy, commercial throughput or probabilistic uncertainty. Numerical sampling and layer-grid
effects and unknown physical uncertainties remain distinct. The result contracts are in
[Schemas](SCHEMAS.md); see also [Benchmark](BENCHMARK.md) and [Contributing](../CONTRIBUTING.md).
