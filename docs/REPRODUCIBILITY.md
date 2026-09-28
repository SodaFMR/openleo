# Reproducing numerical and native packet evidence

The release workflow uses committed scientific inputs and the separately built
OpenLEO ns-3.48 network backend. It requires a passing published numerical
TEME reference and actual native packet results; a model-only benchmark cannot
satisfy it.

## Setup and one-command reproduction

Install Python 3.12 or newer and the locked repository environment:

```bash
uv sync --locked --no-dev
```

Build the pinned native network executable using the
[adapter instructions](https://github.com/SodaFMR/openleo/blob/main/adapters/ns3/README.md).
Keep it in its ns-3 build tree so its shared libraries remain available.
Linux and macOS are supported; use WSL2 for both backend and caller on Windows.

After that build, run this single command from the OpenLEO repository root:

```bash
uv run --no-dev openleo reproduce \
  examples/studies/release_benchmark.json \
  --reference examples/validation/vallado_06251.json \
  --network-backend ../ns-3.48/build/scratch/ns3.48-openleo-network-replay \
  --output runs/reproduction
```

Use a new or empty output directory. The runner verifies the published orbit
fixture, runs the frozen benchmark with the trusted native executable,
requires actual packet evidence with no unresolved packets or source send
errors, and publishes the complete result after successful validation.
A missing executable, failed numerical reference, invalid native evidence, or
failed child experiment cannot silently become complete release evidence.

The [frozen configuration](../examples/studies/release_benchmark.json) requests:

| Dimension | Values |
| --- | --- |
| Frequency | 12 and 20 GHz |
| Sampling | 30 and 60 s |
| Gas-grid refinement | 1 and 2 |
| UDP load | 10000 and 300000 payload bit/s per flow |
| Window | First 300 s of the original scenario |
| Traffic | 1200-byte payloads, two flows per direction |
| Queues, acquisition, seed | Eight packets, 0.1 s, 1 |
| Propagation cases | Free space, reference gases, declared gases/rain/cloud |
| Routing models | Minimum delay, maximum rate, fixed capacity |

The four matrix dimensions yield 16 integrated experiments. Three propagation
cases and three route replays per experiment yield 144 native replays.
These are expected counts from the configuration, not a statement that the
entire matrix has already been executed in a particular published release.
Each result reports its actual completed counts.

The two frequencies retain identical declared gains, EIRP, bandwidth, and
receiver noise. The rain/cloud states and station locations are controlled
assumptions, not observations. This is not a fixed-aperture or frequency-specific
hardware comparison.

## Evidence bundle

Open `runs/reproduction/index.html`. The top-level files include:

- `release-evidence.json`: schema 1, actual completion counts and evidence links;
- `orbit-verification.json`: numerical pass/fail, residuals, units, frame,
  package versions, fixture and source hashes, epoch convention, limitations;
- `inputs/orbit-reference.json`: exact local reference fixture;
- `benchmark/`: frozen configuration and scientific inputs, metrics, effects,
  uncertainty inventory, reports, and all child/native evidence; and
- `manifest.json`: recursive fingerprints of exported files.

The benchmark freezes portable source scenario, catalog, and configuration
under its own `inputs/`. Each child experiment retains original-window inputs,
effective settings, derived case hashes, workbench tables, packet/hop
observations, backend identity, and manifests. Browser ZIP exports preserve the
same integrated child structure.

`benchmark/metrics.csv` contains deterministic scalar outputs.
`numerical_metrics_sha256` identifies those bytes for numerical comparisons.
Wall times and runtime environment are diagnostics and excluded from that
digest; full manifests identify exact bytes, including diagnostics, and can
differ between otherwise numerically identical repetitions. Compare both the
numerical digest and recorded versions/inputs when assessing reproducibility.

## Separate checks and model-only work

The numerical check can be inspected independently:

```bash
uv run --no-dev openleo verify-orbit examples/validation/vallado_06251.json
```

It prints JSON and returns success only when every declared TEME residual
passes. Its expected states come from Vallado case 06251. The comparison shares
SGP4 theory and implementation lineage, covers one near-Earth moderate-drag
case, and is not observational orbit or RF validation. See
[Orbit verification](ORBIT_VERIFICATION.md) for precision and provenance.

Run the benchmark directly when a separate reference check is appropriate:

```bash
uv run --no-dev openleo benchmark examples/studies/release_benchmark.json \
  --network-backend ../ns-3.48/build/scratch/ns3.48-openleo-network-replay \
  --output runs/benchmark
```

Omitting `--network-backend` explicitly runs a model-only study: the ignored
load dimension collapses to eight experiments, load values become `null`,
and `packet_evidence` is `missing`. No placeholder packet measurements
are generated. `reproduce` has no such fallback.

The native analytical checks are independent of the large release matrix:

```bash
uv run --no-dev python tests/ns3_network_checks.py \
  ../ns-3.48/build/scratch/ns3.48-openleo-network-replay
```

See [Benchmark](BENCHMARK.md) for metric/effect matching and the uncertainty
inventory, [Validation](VALIDATION.md) for check boundaries, and
[Contributing](../CONTRIBUTING.md) for the full software checks.

## Interpretation and compatibility

Reproducible results are evidence of a specified computation. Hash equality,
published numerical reference agreement, and queue accounting do not establish
calibrated received power, local-weather accuracy, commercial throughput, or
probabilistic uncertainty. Numerical sampling/grid effects and unknown physical
uncertainties remain distinct.

Workbench schemas 1/2/3 and the separate schema-1 result kinds are documented
in the [schema contracts](SCHEMAS.md). Version 1.0 supported contracts
remain stable. Compatible evolution is additive; changes to established field
meaning or removal require a new schema version and a future major software
release.
