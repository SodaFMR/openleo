# Validation

OpenLEO separates software verification from physical validation. Passing the checks below
shows numerical agreement or artifact consistency in the named domains; it does not show
agreement with an independently measured RF link. The default suite runs with
`uv run pytest`; long checks are marked `slow` (`uv run pytest -m slow`), and the 1000-satellite
benchmark is also marked `benchmark`.

## OpenLEO 2.0 engine oracles

### v1.0.0 equivalence

`tests/fixtures/v1/` holds compact extracts of what tag `v1.0.0` computes for the four shipped
examples (ISS single pass and the three Iridium scenarios): every link sample of three stations
and ten satellites at every grid time, satellite and station ECEF, station statistics, the
network summary and route frames, plus the single-pass trace. They were generated once in a
v1.0.0 checkout (`tests/fixtures/v1/README.md`); a test checks them against their recorded
SHA-256. With events disabled at the same grid times, the 2.0 engine must match:

| Quantity | Tolerance |
| --- | --- |
| angles | 1e-8 deg |
| apparent elevation | 1e-9 deg |
| range, satellite ECEF | 1e-4 m |
| range rate | 1e-6 m/s |
| Doppler | 1e-3 Hz |
| dB quantities | 1e-9 dB |
| atmospheric excess delay | 1e-13 s |
| delay | range tolerance / c |
| rates and MODCOD | exact |

(`tests/engine/test_geometry.py`, `test_evaluator.py`, `test_run.py::test_events_off_matches_v1_samples`.)
Events-off station integrals, best-link bits and route frames equal v1's
(`test_statistics.py::test_grid_left_hold_bits_match_v1`, `test_routing.py::test_grid_mode_matches_v1_routes`);
route changes differ only by v1's change at the zero-duration final sample (47 versus 48;
[Migration](MIGRATION.md)). The array kernels equal the scalar v1 kernels on 10,000 random
in-domain inputs per configuration (`tests/physics/test_path_arrays.py`, slow): ray trace
within 1e-10 dB and 1e-9 deg, hydrometeor path within 1e-12 dB. The ray tracer's array endpoint
solve is a safeguarded Newton iteration (v1: 40-step bisection); the equivalence holds at 12,
20 and 30 GHz, and above about 50 GHz v1's bisection is the less precise solve.

Off the grid, the geometry is compared with Skyfield's full TEME-GCRS-ITRS chain and the link
budget with v1's scalar arithmetic at arbitrary times
(`test_topocentric_matches_the_v1_skyfield_chain_off_grid`,
`test_evaluate_off_grid_matches_the_v1_link_budget`).

### Independent geometry of the ISS example

`tests/engine/test_geometry_oracles.py` checks the ISS example's 40 visible samples against
literal v1.0.0 values, recomputes the closest sample's range, elevation and azimuth from
Skyfield's ITRS positions with standard-library east/north/up trigonometry (within 1e-5 m and
1e-9 deg), requires negative range rate and positive Doppler while approaching and the reverse
while receding with strictly monotone range on each side, and compares the closest sample's
range rate with the central finite difference (ρ_{i+1} − ρ_{i−1})/(20 s) within 0.5 m/s. These
hold the Skyfield propagation and ITRS conversion fixed; they are not an independent orbit
propagator or a measurement.

### Analytic events

`tests/engine/test_events.py` uses synthetic providers with known roots, e(t) = a·sin(ωt + φ) + b:
AOS and LOS must lie within `tolerance_s` of the analytic crossings and culminations within
max(`tolerance_s`, the value-noise floor). A pass lasting 0.4 Δ between two samples must be
found; a hidden dip must be bisected by the monotonicity guard and persistent non-monotonicity
must raise. On the real ISS pass, AOS and LOS are compared with an independent 0.01 s dense
scan within 2 × `tolerance_s` (`test_iss_pass_matches_dense_search`).

### ACM ramps

`tests/engine/test_acm.py` drives the MODCOD state machine with synthetic Es/N0 ramps whose
threshold crossings are known: acquisition, hysteresis-gated upgrades, immediate downgrades,
loss at LOS, threshold equality (a level met exactly counts as reached), tangent peaks, and
feedback delay d > 0 (zero-rate windows, decisions at the pass end, overlapping decisions with
fixed-delay causality), checking exact segment boundaries and rates. With d = 0 the
continuous-time machine equals v1's `select_modcod` at every grid sample, and a real Iridium
pass is checked against a dense v1 walk.

### Integration, statistics and routing

Exact integrals of piecewise-constant functions; Gauss-Legendre Shannon integrals against an
analytic integrand, with nodes never at breakpoints; time-measure fractions on synthetic
windows; best-link envelopes, tie-breaking and handovers on hand-built cases; route intervals on
a hand-built graph with known breakpoints; ISL edges equal to v1's at the range and tangency
thresholds (`test_integrate.py`, `test_statistics.py`, `test_routing.py`).

### Convergence gate

On the hydrometeor example (60 s grid, 4× refinement, 1 % relative tolerance), every gated link
and station metric passes with every refined value recomputed on the 15 s grid (passes, MODCOD
segments and Shannon quadrature found again, `recomputed` = 1 on all 3,556 rows; the largest
relative difference of a link or station metric is about 1.4e-5, from event times that differ
within the 1 ms tolerance); the one failing metric is a route-change count, reported and not
gated (`test_run.py::test_convergence_passes_on_hydrometeor_example`,
`test_hydrometeor_convergence_fails_only_on_route_changes_between_breakpoints`). A deliberately
coarse case fails in `error` mode and publishes with warnings in `warn` mode. In S1 route metrics
are not gated: routes are held between route breakpoints and inter-satellite range and occlusion
changes between them are not resolved ([Scope](SCOPE.md)).

### Gas-table bound

The tabulated gaseous loss used only for the Shannon integrand and MODCOD root-finding
([Methods](METHODS.md)) is compared with the exact ray trace over the whole engine domain for
every station height of the examples at 12, 20 and 30 GHz and two layer refinements, and with
the scalar v1 kernel, within 1e-10 dB (`tests/physics/test_gas_table.py`). Its recorded bound is
heuristic at the Chebyshev level; tests show that it covers the measured error between its check
points, that an accepted table stays within its bound on dense sweeps (1 to 183 GHz in the slow
suite), and that unverifiable tables (the 60 GHz oxygen complex) fall back to the exact trace.
Acceptance requires a bound of at most 2.5e-11 dB, a quarter of the 1e-10 dB limit.

### Performance

`tests/test_performance.py` (markers `slow` and `benchmark`) runs the pinned 1,000-satellite
Starlink catalog with 4 stations for 24 h at 30 s, events on, `itu_reference`, no network, one
worker, with the convergence gate passing, and requires it to finish in under 5 minutes on the
development machine (measured 284.3 s with one worker since the convergence gate recomputes
every pair's passes and MODCOD segments on the 7.5 s refined grid; 174.5 s before that, when it
reused the primary events). CI runs a 100-satellite, 2-hour variant with a 60 s bound in every job
(`tests/test_performance_ci.py`); the 24-hour benchmark is run locally because its bound is set
for the development machine.

### Cross-platform agreement

Every CI job runs the coupled example and compares it with a digest written from a Linux run
(`tests/fixtures/ci/coupled-ubuntu.json`) with `scripts/compare_results.py`: per table, row counts
and NaN counts must match, and column sums and extrema must agree within the bounds the §11
per-element tolerances imply (time columns within 2 × `tolerance_s`, integrated bits within
1 bit + 1e-6 relative, fractions within 1e-6; see the script). Within one platform, outputs are
byte-identical, and `--workers N` is byte-identical to one worker.

### Formats, API, CLI, sweeps and replays

OLC round trips, hashes, corruption and golden files (`tests/fixtures/olc/`); Parquet round trips
(skipped without the extra); CSV determinism; manifest verification; atomic publication under
injected failure (`tests/results/`). The app API ports the v1 security checks: Host, Origin,
session token, 60 requests per second, body size, catalog pinning, no browser-supplied paths or
executables, the asynchronous job lifecycle, the cache by id and failure preserving prior
results (`tests/app/`). Every CLI subcommand's success and user-error path (no traceback) and
every retired command's pointer (`tests/test_cli.py`). OAT nominals equal the base byte for byte
in tables, the grid run limit, the variants against the v1 aligned cases, and every shipped sweep
and reproduce configuration resolving and expanding (`tests/test_sweep.py`). The replay bridges
produce the same ns-3 input CSVs from 2.0 tables as v1 did from its bundles on the same grid
(`tests/replay/`).

## Published TEME numerical reference

`openleo verify orbit examples/validation/vallado_06251.json` prints JSON for
five published Vallado case 06251 TEME states. Position and velocity vector
residuals must pass `1e-6 km` and `1e-9 km/s` respectively at every epoch.
The check exercises TLE-to-OMM conversion, the checksum-verifying catalog
loader, Skyfield propagation, and TEME state extraction. Expected vectors are
fixed literals from the published ephemeris, independent of production output.

The result records the fixture and converted OMM hashes, source fingerprints,
frame, units, epoch convention, residuals, tolerances, and package versions.
Elapsed times are referenced to the loaded SGP4 epoch; conversion precision and
its reported epoch shift do not establish absolute UTC accuracy. The case is
one near-Earth moderate-drag orbit and shares SGP4/Vallado theory and lineage.
It is numerical verification, not independent observation, deep-space or frame
transformation validation, or end-to-end RF evidence. Details and primary
attribution are in [Orbit verification](ORBIT_VERIFICATION.md) and
[Third-party data](../THIRD_PARTY_DATA.md).

## Engine Regression

The frozen GP record is propagated by Skyfield's corrected Vallado SGP4 engine. At the
element epoch, tests (`tests/engine/test_catalog_v1_loader.py`) lock the geocentric position to
`[2541.22048664, -6305.90893063, -6.41569470] km` and velocity to
`[4.41995972, 1.78637061, 5.99594561] km/s`, each with absolute tolerance `1e-8` in
its stated unit. This detects dependency or platform drift. It is an engine regression,
not independent measurement validation.

## Hand-Derived Invariants

Tests use literal values calculated independently of production helpers for vacuum
delay, first-order Doppler, free-space path loss, receiver noise, and Shannon capacity.
The reference receiver check uses 10 dBW EIRP, 161 dB path loss, 23.010299956639813 dBi
receiver gain, 200 K system noise temperature, and zero miscellaneous loss to obtain
$C/N_0=77.59916717321767\,\mathrm{dB\,Hz}$.

The range-rate and Doppler sign checks encode the public convention: approach has
negative range rate and positive downlink Doppler; departure has positive range rate
and negative Doppler. Sampled range must strictly decrease to one minimum and then
strictly increase.

## P.676-13 Official Workbook Verification

The specific-attenuation implementation evaluates the ITU-R P.676-13 Annex 1
line-by-line method with
dry-air pressure 1013.25 hPa, temperature 288.15 K, and water-vapour density 7.5 g/m³.
The derived water-vapour partial pressure is 9.97288878634056 hPa using
$e=\rho T/216.7$.

At 12, 20, 60, 90, and 130 GHz, tests compare dry-air, water-vapour, and total specific
attenuation against all 15 literal values from official workbook Rev8.3.0. Every
component must agree with relative tolerance `1e-12` and absolute tolerance `1e-13`;
the in-memory total must also equal the implementation's single dry-plus-water
addition exactly. Boundary tests cover the 1–1,000 GHz validity range, finite physical
inputs, immutable cases, strict artifact schemas, deterministic output, and exact
summary-to-CSV SHA-256 binding before visualization.

The workbook SHA-256 is
`e2d8d864c80f59752318548cdd75d818792b44574da6e41dbdc5cb722aab7546`. It is used as
validation evidence and is not redistributed. Equations and coefficient data are
adapted from the pinned MIT-licensed ITU-Rpy source documented in
[`THIRD_PARTY_NOTICES.md`](../THIRD_PARTY_NOTICES.md); the official Recommendation and
workbook, not that secondary implementation, are the scientific authorities.

This standalone benchmark establishes agreement for specific attenuation in dB/km at five
frequencies under one homogeneous state. It does not validate a vertical or slant-path
loss, atmospheric profile, local weather, rain, cloud, scintillation, availability,
received power, or operator performance. Exact equations, values, commands, and source
links are in [P.676-13 Specific Gaseous Attenuation](GASES.md).

## P.838-3 and P.840-9 Official Workbook Verification

Standalone hydrometeor tests compare three rain cases and three cloud cases with
literal oracles from official workbook Rev8.3.0, using the same workbook hash
above. Rain checks cover `k`, `alpha`, and specific attenuation in dB/km; cloud
checks cover mass absorption and instantaneous slant attenuation in dB. The cloud
calculation includes the P.840-9 equation (12) correction at 273.75 K. Each oracle
comparison uses relative tolerance `1e-12`, with small component-specific absolute
tolerances. Rain coefficients are also checked against rounded P.838-3 Table 5
values.

Additional checks cover zero hydrometeors, polarization limits, cloud-water
linearity, supported domains, non-finite inputs and outputs, immutable results,
strict batch configuration, escaped report labels, and artifact fingerprints.
The public fixture contains only inputs; expected results remain separate test
oracles. These scalar checks do not by themselves verify a rain-layer path or
constellation coupling, and do not infer weather or availability. Exact cells,
equations, sources, and the batch command are in [Rain and cloud attenuation](HYDROMETEORS.md).

## Reference Profile and Slant-Path Verification

The constellation model implements the P.835-7 Annex 1 global reference profile
and P.453-14 refractivity. Profile tests compare 14 literal midpoint states spanning
approximately 0–100 km with the official workbook: temperature, total pressure,
water-vapour density, vapour pressure and refractive index. The workbook labels the
equivalent P.835-6 global-profile equations; these were checked against P.835-7 before
reuse. Temperature and thermodynamic values use relative tolerance `5e-13`; the
refractive index uses absolute tolerance `3e-16`.

Independent checks cover geometric-to-geopotential height conversion, the published
layer-boundary behavior, the minimum upper-atmosphere water-vapour mixing ratio, and
the distinction between total and dry pressure. At sea level, P.835 supplies total
pressure 1013.25 hPa, yielding dry pressure approximately 1003.2771112136594 hPa.
This is deliberately different from the homogeneous specific-attenuation fixture,
which declares 1013.25 hPa as its **dry** pressure.

The normalized P.676-13 layer calculation reproduces two official workbook cases
from sheet `P.676-13 A_Gas_A1_2.2.1b`. Both use 28 GHz, a lower height of 1.3 km AMSL
and **apparent**, not geometric, launch elevation of 30 degrees:

| Upper height (km AMSL) | Total gaseous attenuation (dB) | Bending within the column (rad) |
| ---: | ---: | ---: |
| 8 | 0.24376211236218553 | 0.0002517972739610741 |
| 100 | 0.27744110604568128 | 0.00045579353223956787 |

Both attenuation and bending must agree within relative tolerance `1e-8`. The workbook
hash is the same Rev8.3.0 hash above; it is not redistributed. The 8 km case verifies a
partial column only. Coupled ground-to-space experiments always extend the atmosphere
to 100 km and require the target above it.

Separate analytic tests check vertical and homogeneous-shell paths, zero excess loss
and delay in vacuum, and recovery of a known refracted endpoint. The endpoint solver
converts a geometric elevation/range pair into the apparent launch elevation; using
geometric elevation directly as apparent elevation is explicitly tested as a different
calculation. Reported delay includes the bent path's additional geometric length and
its refractive optical-path excess, relative to the original endpoint chord.

Layer-refinement comparisons (a `propagation.refinement` sweep) report differences in
gaseous loss, apparent elevation and excess delay; a small grid difference does not quantify
atmospheric-model error. Exact model definitions, source fingerprints and domain restrictions are
in [Reference propagation](REFERENCE_PROPAGATION.md). These checks establish reference
implementation agreement, not local meteorological accuracy or measured RF performance.

## Coupled declared hydrometeor verification

`tests/physics/test_hydrometeor_path.py` checks analytic zenith, zero-thickness and slant
spherical-shell paths, zero rain, supported domains, finite arithmetic, and conversion from dB/km
to dB. The engine applies the declared layers through the same evaluator as every other loss,
and the v1 equivalence fixtures of the hydrometeor example cover the cloud, rain and total
atmospheric fields of every visible sample of stations 0-2 and satellites 0-9 (3 stations × 10
satellites, at every grid time); other pairs enter only through the station statistics compared
with v1. These establish implementation consistency for declared
uniform layers. They do not establish local weather, P.618 effective paths or exceedance
statistics, atmospheric emission, or physical RF accuracy. See
[Coupled propagation](COUPLED_PROPAGATION.md).

## Optional ns-3 replay verification

`tests/ns3_replay_checks.py` and `tests/ns3_network_checks.py` run the separately compiled
ns-3.48 executables directly. The single-link checks cover deterministic underload and overload,
finite queues, outage suppression and queue flushing, TX/RX outage intersections, recovery,
changes during transmission, simultaneous events, draining and invalid inputs; a literal
constant-link case requires a 970-byte UDP payload, 30 bytes of headers, 1 Mbit/s serialization
and 2 ms propagation to give a 10 ms one-way delay. The multi-hop checks use a literal two-hop
case (20 ms end-to-end), reciprocal flows, contention in shared finite queues, route changes in
flight, acquisition gating, missing routes, TTL-bounded loops, determinism and budget rejection.

The Python integration tests in `tests/replay/`, `tests/test_sweep.py` and
`tests/test_reproduce.py` run the backends when `OPENLEO_NS3_REPLAY` and `OPENLEO_NS3_NETWORK`
name them, and are skipped otherwise, so a core-only run does not verify the simulator:

```bash
uv run python tests/ns3_replay_checks.py ../ns-3.48/build/scratch/ns3.48-openleo-replay
uv run python tests/ns3_network_checks.py ../ns-3.48/build/scratch/ns3.48-openleo-network-replay
OPENLEO_NS3_REPLAY=../ns-3.48/build/scratch/ns3.48-openleo-replay \
OPENLEO_NS3_NETWORK=../ns-3.48/build/scratch/ns3.48-openleo-network-replay \
  uv run pytest tests/replay tests/test_sweep.py tests/test_reproduce.py
```

These establish the documented synthetic UDP experiments. They do not validate physical packet
loss, a satellite MAC/PHY, TCP, real traffic or operator performance. See
[Packet replay](PACKET_REPLAY.md) and [Network replay](NETWORK_REPLAY.md).

## Release evidence

`tests/test_reproduce.py` checks that `openleo reproduce` requires a passing published orbit
reference and actual native packet results with no unresolved packets or send errors, and
publishes nothing otherwise. The frozen configuration (`examples/reproduce/release_benchmark.json`)
expands to 20 runs (12/20 GHz × 30/60 s × five propagation cases) with 3 routing models and 2
network loads, 120 native replays (v1: 144, of which the 24 free-space replays at refinement 2
duplicated refinement 1); `tests/test_sweep.py` checks the cases against v1's. Those counts are
configuration expectations until a complete execution supplies its own evidence. See
[Benchmark](BENCHMARK.md) and [Reproducibility](REPRODUCIBILITY.md).

## Validation Still Required

Absolute physical validation needs an independent observation with documented station
position and timing plus calibrated antenna gain and pattern, feeder and receiver gain,
system noise temperature, oscillator behavior, processing, and weather. Public SatNOGS
pass timing or signal presence can provide a qualitative sanity check, but its default
products do not establish calibrated received power, `C/N0`, SNR, or throughput.

Probabilistic uncertainty propagation additionally needs justified input probability
models, standard uncertainties, and correlations. The current synthetic RF assumptions
do not provide that evidence, so passing the deterministic sensitivity benchmark does
not establish a GUM combined standard uncertainty.
