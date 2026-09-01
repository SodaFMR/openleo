# OpenLEO Validation Boundaries

OpenLEO separates software regression checks from physical validation. Passing the
checks below shows that the frozen inputs produce internally consistent results; it
does not show agreement with an independently measured RF link.

## Engine Regression

The frozen GP record is propagated by Skyfield's corrected Vallado SGP4 engine. At the
element epoch, tests lock the geocentric position to
`[2541.22048664, -6305.90893063, -6.41569470] km` and velocity to
`[4.41995972, 1.78637061, 5.99594561] km/s`, each with absolute tolerance `1e-8` in
its stated unit. This detects dependency or platform drift. It is an engine regression,
not independent measurement validation.

## Independent Topocentric Geometry

At the frozen pass's closest sampled approach, Skyfield supplies only the propagated
spacecraft position converted to ITRS ECEF and the WGS84 station's ITRS ECEF position.
The test then uses standard-library trigonometry to rotate the ECEF difference into
east, north, and up and independently calculate slant range, geometric elevation, and
azimuth. These agree with the production topocentric `TraceRow` within `1e-5 m` for
range and `1e-9 deg` for each angle.

This independently checks the topocentric rotation and observable calculations while
holding the Skyfield orbit propagation and ITRS conversion fixed. It is not an
independent orbit propagator or a calibrated measurement.

## Hand-Derived Invariants

Tests use literal values calculated independently of production helpers for vacuum
delay, first-order Doppler, free-space path loss, receiver noise, and Shannon capacity.
The charter receiver check uses 10 dBW EIRP, 161 dB path loss, 23.010299956639813 dBi
receiver gain, 200 K system noise temperature, and zero miscellaneous loss to obtain
$C/N_0=77.59916717321767\,\mathrm{dB\,Hz}$.

The range-rate and Doppler sign checks encode the public convention: approach has
negative range rate and positive downlink Doppler; departure has positive range rate
and negative Doppler. Sampled range must strictly decrease to one minimum and then
strictly increase.

## Finite-Difference Check

At the frozen pass's closest sampled approach, the reported radial range rate is
compared with the central finite difference
$\left(\rho_{i+1}-\rho_{i-1}\right)/(20\,\mathrm{s})$. The absolute tolerance is
`0.5 m/s`, allowing the
expected truncation error of a 10-second sample interval without reusing the engine's
range-rate calculation.

## Deterministic Sensitivity Verification

The v0.2a1 benchmark reruns the complete pass while changing one declared input at a
time. Tests prove that every nominal sweep row exactly reproduces the single unchanged
baseline and that every non-nominal case replaces only its named field. Independent
link-budget invariants require a 1 dB EIRP increase to add 1 dB to `C/N0`, a 1 dB loss
increase to subtract 1 dB, and a doubling of noise temperature to change `C/N0` by
`-10 log10(2)` dB.

The sensitivity summary carries immutable baseline context copied from that completed
unchanged run: fitted-record element epoch, signed endpoint ages, maximum absolute age,
leap-table provenance, and warnings. This is retrospective benchmark evidence, not a
pre-pass prediction artifact or an orbit-accuracy guarantee.

Frozen integration checks cover every published percentage: relative to the nominal
10 s scenario, 1, 20, 30, and 60 s sampling change the integrated Shannon-Hartley upper
bound by `+1.39554703487%`, `-1.76263346877%`, `-0.0841572102157%`, and
`-5.78236557037%`; 5, 20, and 30 deg elevation masks change it by
`+17.2710744088%`, `-29.9627342354%`, and `-50.0114931452%`. These are regression
values for one frozen configuration, not measurements or universal convergence rates.

Sampling-step results are not required to be monotonic because the grid is anchored at
the scenario start, mask crossings are not interpolated, and different steps select
different boundary and quadrature samples. The 1 s case is only the densest declared
numerical reference in the figure; it is not truth or proof of convergence.

The deterministic OAT ranges are not confidence, credible, coverage, tolerance, or
standard-uncertainty intervals. They contain no probability distributions, input
standard uncertainties, covariance propagation, or interaction study. Different RF
sweeps use heterogeneous dimensions and spans, so their plotted response ranges are not
an importance ranking. The full method and terminology boundary are documented in
[Deterministic Sensitivity](SENSITIVITY.md).

## P.676-13 Official Workbook Verification

The v0.2b1 implementation evaluates the ITU-R P.676-13 Annex 1 line-by-line method with
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

This verification establishes agreement for specific attenuation in dB/km at five
frequencies under one homogeneous state. It does not validate a vertical or slant-path
loss, atmospheric profile, local weather, rain, cloud, scintillation, availability,
received power, or operator performance. Exact equations, values, commands, and source
links are in [P.676-13 Specific Gaseous Attenuation](GASES.md).

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
