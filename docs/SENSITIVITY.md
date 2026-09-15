# Deterministic Sensitivity Benchmark

OpenLEO includes a deterministic one-at-a-time (OAT) study for the frozen
ISS–Cartagena free-space example. Its question is deliberately limited:

> How do pass-level model outputs change when one declared input assumption is
> replaced by another declared value while every other input is held fixed?

The result is a reproducible numerical experiment, not an uncertainty analysis and not
validation against a measured radio link.

## Evidence boundary

The benchmark keeps three evidence classes separate:

| Class | What the benchmark contains | What it does not establish |
| --- | --- | --- |
| Public orbit data | One frozen CelesTrak GP CSV record, with source, retrieval time, terms URL, and SHA-256 recorded in the scenario and summary | The fitted GP record is not an exact trajectory, an orbit covariance, or independently measured truth |
| Declared assumptions | A selected station and UTC window, synthetic RF terminal values, free-space propagation, and synthetic OAT sweep spans | The RF values and sweep spans are not measurements of ISS, a Cartagena installation, an operator, or a population distribution |
| Model-derived outputs | Sampled geometry, link-budget quantities, Shannon-Hartley upper bounds, OAT changes, CSV/JSON artifacts, and both figures | These outputs are not measured RF performance, achieved throughput, confidence intervals, or calibrated validation |

The orbit record and its data terms are documented in
[THIRD_PARTY_DATA.md](../THIRD_PARTY_DATA.md). The physical model is summarized in
[Methods](METHODS.md), its claim rules are defined in [Scope](SCOPE.md), and the
distinction between software verification and physical validation is maintained in
[Validation](VALIDATION.md).

## Method and terminology

For a model output $y=f(x_1,\ldots,x_n)$, this benchmark evaluates complete model
runs at explicitly declared values of one input $x_i$, leaving all other inputs at
their scenario values. Each range therefore describes only the response over those
assumed cases. OAT does not expose interactions that require two or more inputs to
change together.

This is different from a local sensitivity coefficient. In the GUM framework, a
sensitivity coefficient is the derivative of an output estimate with respect to an
input estimate, evaluated for a measurement model. A combined standard uncertainty
also requires standard uncertainties for the input quantities and treatment of
correlation. OpenLEO has neither justified input distributions nor correlations for
this synthetic RF scenario, so it does not calculate a combined standard uncertainty.
See [BIPM JCGM 100:2008, *Guide to the expression of uncertainty in
measurement*](https://doi.org/10.59161/JCGM100-2008E).

The [2026 GUM amendment on nonlinearity in measurement
models](https://doi.org/10.59161/PPDI3267) is relevant to a later justified uncertainty
model; it does not turn an assumed OAT range into an uncertainty interval. Likewise,
Iott, Haftka, and Adelman's
[NASA TM-86382, *Selecting Step Sizes in Sensitivity Analysis by Finite
Differences*](https://ntrs.nasa.gov/citations/19850025225) concerns numerical step-size
selection for derivative approximations. The current study evaluates declared cases
and does not report finite-difference derivatives.

## Exact canonical configuration

The committed configuration is
[`examples/sensitivity/iss_cartagena_oat.json`](../examples/sensitivity/iss_cartagena_oat.json).
It uses method `deterministic_one_at_a_time`, five sweeps, and 20 cases. Every sweep
contains the unchanged scenario value exactly once.

| Parameter | Unit | Scenario value | Declared values |
| --- | ---: | ---: | --- |
| `time_window.step_s` | s | 10 | 1, 2, 5, 10, 20, 30, 60 |
| `time_window.minimum_elevation_deg` | deg | 10 | 5, 10, 20, 30 |
| `radio_link.eirp_dbw` | dBW | 10 | 7, 10, 13 |
| `radio_link.system_noise_temperature_k` | K | 300 | 200, 300, 400 |
| `radio_link.miscellaneous_loss_db` | dB | 2 | 0, 2, 4 |

The unchanged baseline is simulated once. Its metrics are reused for the five nominal
rows; every non-nominal row is produced from a new immutable scenario value. Cases are
written in configuration-file sweep order and ascending declared-value order. The raw
scenario and sensitivity JSON files are fingerprinted before parsing. For this frozen
benchmark their SHA-256 values are:

- scenario: `0da86937ac841c727ecdfd5a955b405944fd3e9a4440bf5eca16f72fbb7c6451`;
- sensitivity configuration:
  `8c7e25f73791661a37952fec3aa9e18d730e12d408e6e9e32afd8617ad6be942`; and
- orbit record: `ddb21d9a4a3a4812ee397751555b9d55d2767f30c33190afba90e4bbb8db13f9`.

## Artifacts and fields

Run the computation without Matplotlib:

```bash
uv run openleo sensitivity \
  examples/scenarios/iss_cartagena.json \
  examples/sensitivity/iss_cartagena_oat.json \
  --output runs/iss-sensitivity
```

This writes `sensitivity.csv` and `sensitivity-summary.json`. The CSV has one row per
declared case and these exact fields:

| Group | Fields |
| --- | --- |
| Case | `parameter`, `unit`, `baseline_value`, `case_value`, `is_nominal` |
| Sampled pass | `row_count`, `sampled_aos_utc`, `sampled_los_utc`, `sampled_duration_s`, `maximum_elevation_deg`, `minimum_range_m` |
| RF and bounds | `maximum_carrier_to_noise_density_db_hz`, `maximum_shannon_capacity_upper_bound_bps`, `integrated_shannon_capacity_upper_bound_bits` |
| Changes | `maximum_capacity_upper_bound_delta_bps`, `maximum_capacity_upper_bound_relative_change_percent`, `integrated_capacity_upper_bound_delta_bits`, `integrated_capacity_upper_bound_relative_change_percent` |

All change fields are signed and relative to the unchanged scenario baseline.

If a baseline denominator is zero, relative change is JSON `null` or an empty CSV
field. The summary repeats the complete portable baseline inputs, baseline metrics,
sweeps, counts, source fingerprints, package versions, ordering, numeric formatting,
and limitations. Its top-level `baseline_context` records the element epoch, signed
start/stop element ages, maximum absolute age, leap-second-table source and SHA-256,
and baseline warnings. It deliberately omits machine-specific local paths.

This context is retrospective evidence from the completed unchanged baseline run using
the frozen fitted GP record. It is not a pre-pass prediction artifact, propagated orbit
covariance, or accuracy guarantee.

The plotter reads only these completed artifacts; it does not rerun the model:

```bash
uv run openleo plot-sensitivity runs/iss-sensitivity \
  --output docs/images/iss-cartagena-sensitivity-overview.svg
```

## Frozen numerical results

The nominal 10 s run contains 40 visible samples from `2026-08-30T06:15:30Z` through
`2026-08-30T06:22:00Z`. Its maximum elevation is `61.5799734359 deg`, minimum range is
`473676.7193 m`, maximum capacity upper bound is `6336955.32414 bit/s`, and integrated
upper bound is `1867325705.98 bit`.

Selected OAT responses of the integrated upper bound are:

| Changed input | Result relative to the nominal 10 s scenario |
| --- | ---: |
| 1 s sampling | +1.39554703487% |
| 20 s sampling | −1.76263346877% |
| 30 s sampling | −0.0841572102157% |
| 60 s sampling | −5.78236557037% |
| 5 deg elevation mask | +17.2710744088% |
| 20 deg elevation mask | −29.9627342354% |
| 30 deg elevation mask | −50.0114931452% |

Sampling responses need not be monotonic. The time grid is anchored at the scenario
start, and AOS/LOS are the first and last samples at or above the mask; true mask
crossings are not interpolated. Changing the step can therefore move both boundary
samples and interior quadrature nodes. For example, the 20 s case begins at
`06:15:40Z`, while the 30 s case begins at `06:15:30Z`. Grid alignment explains why a
coarser declared step can happen to lie closer to the 10 s result than a finer one.

Panel A of the sensitivity figure subtracts the 1 s case because it is the densest
declared numerical reference. It is not truth, a converged solution, or a physical
observation. The CSV's delta fields instead use the scenario's nominal 10 s baseline.

The three RF ranges also cannot be ranked against one another. They span different
quantities and deliberately different widths: 6 dB of EIRP, a factor of two in noise
temperature, and 4 dB of miscellaneous loss. Their plotted response spans therefore
do not measure parameter importance and do not represent uncertainty budgets.

![Deterministic sensitivity overview for the frozen ISS–Cartagena example](images/iss-cartagena-sensitivity-overview.svg)

## Allowed and prohibited claims

The artifacts support statements about deterministic changes for this exact frozen
configuration. They do not support claims of:

- confidence, credible, coverage, tolerance, or standard-uncertainty intervals;
- probabilities, Monte Carlo results, input covariance, or combined standard
  uncertainty;
- convergence or truth at a 1 s sampling interval;
- importance rankings from heterogeneous RF sweep spans;
- calibrated received power, `C/N0`, SNR, or throughput;
- actual ISS, ground-station, operator, or commercial-service performance; or
- generalization beyond the declared orbit record, time window, station, free-space
  model, and synthetic assumptions.

Shannon-Hartley quantities remain theoretical upper bounds, never transferred data or
achieved throughput.

## Implementation reading order

1. [`src/openleo/sensitivity.py`](../src/openleo/sensitivity.py) defines strict loading,
   immutable case generation, metrics, and deterministic CSV/JSON output.
2. [`src/openleo/simulation.py`](../src/openleo/simulation.py) supplies each complete
   pass result without sensitivity-specific physics.
3. [`src/openleo/sensitivity_plotting.py`](../src/openleo/sensitivity_plotting.py)
   validates completed artifacts and renders the overview.
4. [`src/openleo/cli.py`](../src/openleo/cli.py) exposes `sensitivity` and
   `plot-sensitivity` while keeping Matplotlib out of the computation path.
