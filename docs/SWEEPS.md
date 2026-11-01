# Sweeps

A sweep runs a family of scenarios derived from one base scenario and compares their metrics.
It replaces the four v1 study commands: the one-at-a-time sensitivity study (`oat`), the
fidelity and propagation studies (`grid` and `variants`), and the integrated experiment
(`variants` with a `replay` block). The format is specified in [Schemas](SCHEMAS.md#sweep-v2).

```bash
uv run --no-dev openleo sweep examples/sweeps/propagation_cases.json --output runs/cases --workers 4
uv run --no-dev openleo report runs/cases --output runs/cases-report
```

Output: `sweep.json` (the spec and its SHA-256, the base scenario's SHA-256, every run's id,
label, directory, scenario SHA-256 and replay summary), `comparisons.csv`, one results directory
per run under `runs/`, and `manifest.json`. Every generated scenario is validated by the scenario
parser and stored with its own hash, linked to the base hash.

## Modes

- **`oat` (one at a time).** Each parameter has a `nominal` that must equal the base scenario's
  value; the runs are the nominal run (`oat:nominal`) and one run per value that differs from its
  nominal, all other inputs held at their base values. The nominal run reproduces the base
  scenario exactly, byte for byte in its tables (a test checks this). OAT exposes no interactions
  between inputs.
- **`grid`.** The Cartesian product of the parameters' values, the first parameter varying
  slowest (`grid:0`, `grid:1`, ...); the baseline is `grid:0`. At most 512 runs (`max_runs`).
- **`variants`.** An explicit list of RFC 6902 patches (`replace`, `add`, `remove`) on the
  normalised scenario, each with an `id` and `label`; the baseline is the variant flagged
  `"baseline": true`, else the first.

Parameter paths and patch paths are RFC 6901 JSON Pointers into the normalised scenario. A sweep
may not move `time.start_utc` or `time.stop_utc`; `window` (`start_offset_s`, `duration_s`) clips
every run to one part of the base window, so that every run covers the same UTC interval.

## Comparisons

`comparisons.csv` has one row per run and metric: `run_id`, `metric`, `value`, `baseline_value`,
`delta` (value − baseline) and `ratio` (value / baseline, empty when the baseline is 0). Metric
ids name what they measure (`pair/<station>/<norad>/<metric>`, `station/<station>/<metric>`,
`route/<source>-><target>/<model>/<metric>`). Zero-effect rows are kept.

Every comparison is labelled "deterministic comparison; not an uncertainty interval". The values
are complete model runs at declared input values; they carry no probability distribution, input
standard uncertainty or correlation, and their ranges are not confidence, coverage or GUM
combined standard uncertainties (see the
[GUM, JCGM 100:2008](https://doi.org/10.59161/JCGM100-2008E)). Different parameters have
different units and spans, so their response ranges are not an importance ranking. Sampling and
layer-refinement differences measure numerical sensitivity of the declared configuration, not
the error of the physical model.

## Replays

An optional `replay` block replays every run, over its whole (clipped) window, through the
trusted ns-3 backends given on the command line (`--packet-backend`, `--network-backend`):
`replay.link` replays one station-satellite link at each of up to 8 offered loads;
`replay.network` replays the first configured route pair for each routing model
(`routing_models`, else all of the run's route models) and load, with `flows_per_direction`
(1 to 4) and `acquisition_delay_s`. Replays are published under `replays/<run id>/` next to
`runs/`. A replay needs a `window` of at most 3600 s. A kind whose backend is not given is not
replayed, and `sweep.json` records the native evidence as `actual`, `partial` or `missing`. See [Packet replay](PACKET_REPLAY.md)
and [Network replay](NETWORK_REPLAY.md).

## Shipped sweeps

| File | Mode | Runs | Purpose |
| --- | --- | --- | --- |
| `examples/sweeps/eirp_oat.json` | `oat` | 3 | ISS pass, EIRP 7/10/13 dBW around the 10 dBW nominal |
| `examples/sweeps/propagation_cases.json` | `variants` | 3 | the v1 aligned propagation cases: free space, ITU reference gases, gases plus declared hydrometeors, over the first 1800 s |
| `examples/sweeps/sampling_fidelity.json` | `grid` | 6 | the v1 fidelity study: 12/20 GHz × 30/60/120 s steps on the reference atmosphere, first 1200 s |
| `examples/sweeps/release_matrix.json` | `grid` | 8 | the frozen release matrix (12/20 GHz × 30/60 s × layer refinement 1/2, 300 s) with network replays at 10 and 300 kbit/s |

The 12 and 20 GHz cases hold the declared EIRP and antenna gains fixed; they are not a comparison
at fixed antenna aperture and do not reconstruct an operator's radio system.

## Interpretation

With events enabled (the default), changing `time.step_s` mostly changes the Shannon quadrature
nodes and the route breakpoints; contact boundaries and MODCOD switches are root-found and do not
move with the grid. A sweep over `step_s` therefore isolates the residual grid dependence that the
[convergence gate](METHODS.md#convergence-gate) also measures. Results remain model outputs of
declared inputs; see [Scope](SCOPE.md).
