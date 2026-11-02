# Frozen release benchmark

`examples/reproduce/release_benchmark.json` fixes the release matrix before results are
interpreted. It is an `openleo.sweep/2` grid on the declared-hydrometeor Iridium example:
12 and 20 GHz, 30 and 60 s sampling and five propagation cases (free space; ITU reference gases at
layer refinement 1 and 2; reference gases plus the example's declared rain and cloud at refinement
1 and 2), 20 runs, each clipped to the first 300 s of the base window, with native network replays at 10,000 and 300,000 bit/s per
flow. Packets carry 1,200 payload bytes, queues hold eight packets, two flows run in each
direction, acquisition takes 0.1 s and the seed is 1. The four stations, rain and cloud layers,
gains, EIRP and receiver noise are declared controlled inputs, not measured weather or calibrated
hardware; gain and EIRP stay identical across the frequency cases.
`examples/sweeps/release_matrix.json` is the same matrix as an ordinary sweep. Every run replays
the Madrid-Tromso pair for the three routing models at both loads: 120 native replays. The five
cases are v1's three aligned propagation cases at both refinements, less free space at refinement
2; v1's 144 (16 configurations × 3 propagation cases × 3 models) included 24 free-space replays at layer refinement 2, which duplicate refinement 1 because free space has no layers.

```python
from pathlib import Path

from openleo.reproduce import run_reproduce

evidence = run_reproduce(
    Path("examples/reproduce/release_benchmark.json"),
    Path("examples/validation/vallado_06251.json"),
    Path("../ns-3.48/build/scratch/ns3.48-openleo-network-replay"),
    Path("runs/reproduction"),
)
```

The backend must be the trusted, compiled OpenLEO ns-3 network adapter; it is never downloaded or
selected from a configuration field. Every generated scenario is validated before anything runs.
The sweep's `comparisons.csv` compares every run with `grid:0` (12 GHz, 30 s, free space) per
metric: value, baseline, delta (in the metric's unit) and ratio (empty for a zero baseline).
Zero effects stay visible. Step and layer-refinement comparisons are numerical sensitivity checks,
and the propagation cases compare declared atmospheres;
frequency comparisons hold every other declared input fixed. Native packet results (goodput,
delay, queue and acquisition accounting per flow and direction) are in each replay's
`network-summary.json`. No hypothesis tests, confidence claims or uncertainty bars are inferred,
and every comparison is labelled "deterministic comparison; not an uncertainty interval".

`numerical_metrics_sha256` in `release-evidence.json` fingerprints `sweep/comparisons.csv`, the
deterministic scalar comparisons. Manifests fingerprint exact bytes, including software and
platform strings, and may differ between repetitions on different machines.

Known unknowns stay explicit: fitted GP orbital inputs without covariance; unsurveyed declared
station coordinates; fixed RF and noise inputs; uniform rain and cloud sensitivity inputs; grid and
layer effects; absent MAC/PHY and interference; and synthetic packet-control assumptions.
Numerical convergence does not establish measured physical accuracy, and simulated receive-event
goodput does not establish commercial service performance.

`tests/test_reproduce.py` checks the refusal paths; with `OPENLEO_NS3_NETWORK` set to the compiled
adapter it also runs a real native reproduction on a reduced matrix. The complete frozen matrix is
an explicit release run rather than a routine test workload. See
[Reproducibility](REPRODUCIBILITY.md).
