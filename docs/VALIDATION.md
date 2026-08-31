# OpenLEO v0.1 Validation Boundaries

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

## Validation Still Required

Absolute physical validation needs an independent observation with documented station
position and timing plus calibrated antenna gain and pattern, feeder and receiver gain,
system noise temperature, oscillator behavior, processing, and weather. Public SatNOGS
pass timing or signal presence can provide a qualitative sanity check, but its default
products do not establish calibrated received power, `C/N0`, SNR, or throughput.
