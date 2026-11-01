# Methods

This document describes the physical and numerical methods of the OpenLEO 2.0 engine
(`openleo run`). The physical models are those of OpenLEO v1.0.0, unchanged; what 2.0 changes
is how they are sampled, how event times are found and how integrals are taken. Model-specific
details are in [Reference propagation](REFERENCE_PROPAGATION.md),
[Coupled hydrometeors](COUPLED_PROPAGATION.md), [Gases](GASES.md) and
[Hydrometeors](HYDROMETEORS.md); the checks are in [Validation](VALIDATION.md); the
differences from v1 are in [Migration](MIGRATION.md). Every model and version a run used is
recorded in its `results.json` under `models`.

## Inputs, time and frames

Orbital inputs are archived CelesTrak GP records (OMM-compatible CSV). They are fitted mean
elements for SGP4, not measured positions or osculating Keplerian elements. Every catalog file
is checked against its declared SHA-256 before use; `results.json` records its source, retrieval
time, terms, hash, record count and each element set's epoch and age. An age above 14 days is
a warning, not an orbit-error bound.

Time is UTC. The grid is `start_utc + kΔ` for `k = 0, 1, ...` plus the stop time when it is not
on the grid (a shorter last interval); offsets are whole microseconds. Offsets count UTC
seconds as Python datetimes do (POSIX, no 23:59:60), so a window containing a leap second is
refused. Skyfield's builtin leap-second and ΔUT1 tables are used, and the leap table's SHA-256
is recorded.

Ground stations use WGS84 geodetic latitude, longitude and ellipsoidal height
(`height_ellipsoid_m`) for geometry. The reference atmosphere uses a separately declared height
above mean sea level (`height_amsl_m`); no geoid conversion is applied.

## Geometry

For each satellite, SGP4 (the `sgp4` package's `Satrec.sgp4_array`, WGS72 gravity) gives the
TEME position and velocity at the grid times, at the UTC Julian date Skyfield would pass it.
TEME is rotated to ITRS by `rot_z(−θ)`, with θ Skyfield's `theta_GMST1982` (GMST 1982 at UT1),
and the velocity gets Skyfield's Earth-rotation term `ω × r`. This is algebraically Skyfield's
TEME→GCRS→ITRS chain, in which precession, nutation and GAST cancel; v1 evaluated the full chain
through `EarthSatellite.at(t)`. The difference is rounding: at most about 5e-8 m in position and
6e-11 m/s in velocity, inside the v1 equivalence tolerances (1e-4 m and 1e-6 m/s) that the tests
check against the frozen v1 fixtures and against Skyfield's chain at off-grid times. Polar motion
is not applied, as in v1.

Topocentric quantities come from the ITRS difference ρ = r_sat − r_station and the station's
geodetic east, north and up unit vectors ê, n̂, û:

$$
\rho=\lVert\boldsymbol\rho\rVert,\qquad
\dot\rho=\frac{\boldsymbol\rho\cdot\mathbf v_{sat}}{\rho},\qquad
elevation=\operatorname{atan2}\!\left(\boldsymbol\rho\cdot\hat u,\ \operatorname{hypot}(\boldsymbol\rho\cdot\hat e,\ \boldsymbol\rho\cdot\hat n)\right),\qquad
azimuth=\operatorname{atan2}(\boldsymbol\rho\cdot\hat e,\ \boldsymbol\rho\cdot\hat n)\bmod 360^\circ.
$$

The station is fixed in ITRS, so the range rate uses the satellite's ITRS velocity only.
Azimuth is clockwise from north; elevation is geometric (above the local ellipsoid-normal
horizon), not apparent and not spacecraft altitude. Satellite positions in the `positions`
table are ITRS metres at the grid times.

## Range, delay and Doppler

The geometric one-way vacuum delay and first-order downlink Doppler are

$$
\tau=\frac{\rho}{c},\qquad \Delta f=-f_c\frac{\dot\rho}{c},
$$

with `c = 299792458 m/s`. The range rate is negative while approaching and positive while
receding; the Doppler sign is the opposite. With the reference atmosphere, the one-way delay is
the geometric delay plus the non-dispersive atmospheric excess delay. Worked example: for range
`1,495,209.6389 m`, range rate `-6,709.8042 m/s` and carrier `2.2 GHz`, τ = 4.98748 ms and
Δf ≈ +49.2393 kHz. This is not a two-way tracking observable or a relativistic model.

## Receiver chain

For range ρ, frequency f, EIRP, receive gain G_rx, miscellaneous loss, system temperature T,
equivalent noise bandwidth B, atmospheric loss L_atm (0 in free space) and symbol rate R_s:

$$
\begin{aligned}
L_{FSPL}&=20\log_{10}\left(\frac{4\pi\rho f}{c}\right),\\
C/N_{0,free}&=EIRP+G_{rx}-L_{FSPL}-L_{misc}-10\log_{10}(kT),\\
C/N_0&=C/N_{0,free}-L_{atm},\\
E_s/N_0&=C/N_0-10\log_{10}(R_s),\qquad SNR=C/N_0-10\log_{10}(B),\\
C_{max}&=B\log_2\left(1+10^{SNR/10}\right).
\end{aligned}
$$

The free-space loss is that of [ITU-R P.525](https://www.itu.int/rec/R-REC-P.525/en) (the
edition in force is P.525-5, 11/2024; the formula is the same in P.525-4), with
`k = 1.380649e-23 J/K`. `C_max` is the Shannon-Hartley upper bound in bit/s, not a modem rate
or throughput. The receive gain, system noise temperature and miscellaneous loss come from the
station's terminal (`terminals`); EIRP, frequency and bandwidth from `radio`. The arithmetic,
including its operation order, is v1's.

Worked example: EIRP 10 dBW, gain 20 dBi, path loss 161 dB, miscellaneous loss 2 dB, 200 K and
1 MHz give a received carrier of `-133 dBW`, noise density `-205.588867217 dBW/Hz`,
C/N0 `72.588867217 dB-Hz` and SNR `12.588867217 dB`.

## Reference atmosphere and declared hydrometeors

With `propagation.model = "itu_reference"` the engine traces each ray through the ITU-R P.835-7
global reference atmosphere with P.453-14 refractivity and P.676-13 Annex 1 line-by-line gaseous
attenuation, on a 6371 km sphere up to 100 km. The ray from the station to the geometric
endpoint is found by solving for the apparent (launch) elevation. v1 solves it with 40 bisection
steps (about 4e-11 deg); the 2.0 array kernel uses a safeguarded Newton iteration on the same
equation, stopping once a step is at most 1e-11 deg, which leaves it within the residual's
rounding floor (about 1e-12 deg). See [Reference propagation](REFERENCE_PROPAGATION.md).
Visibility, masks and Doppler stay geometric.

With `propagation.hydrometeors` (`declared_uniform_layers`), P.840-9 cloud loss and P.838-3
rain loss through a declared uniform spherical rain layer are added; the states are constant
during a run. See [Coupled hydrometeors](COUPLED_PROPAGATION.md).

## Reference adaptation (DVB-S2 MODCOD)

Configurations require `B ≥ R_s(1 + α)` for roll-off α. Five DVB-S2 modes (QPSK 1/4, 1/2,
3/4, 8PSK 2/3, 3/4) use the ideal AWGN thresholds of
[ETSI EN 302 307-1 V1.4.1, Table 13](https://www.etsi.org/deliver/etsi_en/302300_302399/30230701/01.04.01_60/en_30230701v010401p.pdf).
Let A(t) = Es/N0(t) − implementation margin. The rule is v1's `select_modcod`:

- without lock, acquire the highest mode with A ≥ its requirement;
- in mode m, keep m while A ≥ req(m);
- upgrade to the highest mode n > m with A ≥ req(n) + hysteresis;
- downgrade immediately to the highest mode with A ≥ req when A < req(m), or lose lock;
- losing visibility resets the state.

The rate in a mode is `R_s ×` its spectral efficiency (bit/s), with no MAC, network or
application overhead deducted. Acquisition, tracking dynamics and decoding are not modelled.

## Events

Events are found per (station, satellite) pair with `time.events.tolerance_s` τ (default
1 ms, range [1e-6, 1] s).

1. The geometric elevation e(t) is evaluated on the grid.
2. Every discrete local extremum of the samples (window edges included) is refined by
   golden-section search on [t_{k−1}, t_{k+1}]. Maxima are culminations; minima are refined too,
   so that e(t) is monotone between consecutive breakpoints at troughs as well.
3. Monotonicity guard: every interval between breakpoints is evaluated at its midpoint. A
   midpoint outside the endpoint range *toward the mask* (by more than 1e-9 relative) bisects
   the interval, up to 6 levels; persistent non-monotonicity raises an error naming the pair. A
   deviation away from the mask cannot change visibility and is ignored.
4. Every interval across which `e ≥ mask` changes is root-found: Illinois regula falsi,
   safeguarded by bisection, all brackets of a pair solved together, until the bracket is no
   wider than τ. The returned time is the bracket end on the far side of the crossing (the start
   of the run that follows).

Accuracy: AOS and LOS lie within τ of the crossing (geometry is evaluated at whole
microseconds, so 1e-6 s is the useful floor). A culmination is located from function values
alone, so near the peak e(t) ≈ e_max − k(t − t_max)² hides location errors up to √(ε/k) for a
value noise ε; culmination times are accurate to max(τ, ~1e-5 s) (up to about 1e-4 s), and the
maximum elevation is affected only to second order.

Short passes: a pass whose peak lies between grid samples is found through its refined
culmination, provided `step_s ≤ P_min/8`, with P_min the shortest orbital period in the
catalogs (86400 s / MEAN_MOTION). The engine checks this bound before propagating whenever
events are enabled.

Passes are the maximal half-open intervals [AOS, LOS) with e ≥ mask; a pass cut by the window is
flagged `truncated_start` or `truncated_end`. A pass of zero duration (an exact mask touch) is
not counted as a contact.

## MODCOD state machine in continuous time

Inside each pass the engine evaluates A(t) at the pass breakpoints (grid times, culmination,
ends), guards monotonicity in the same way (an interval is suspect only when its midpoint's
count of thresholds reached lies outside the endpoints' counts), refines peaks and troughs of A
that come within 0.01 dB of a threshold, and root-finds every crossing of every threshold level
(each req(m) and each req(m) + hysteresis) with the same solver and τ. It then walks the
crossings in time order, applying `select_modcod` at each. The state changes only where A crosses
a level, so the rate is piecewise constant with exact switch times. A crossing within the last τ
before a root-found LOS is not resolved (the link budget is undefined below the mask).

Hysteresis is v1's: upgrades of an existing lock need req + hysteresis; downgrades happen at the
nominal requirement.

Feedback delay d = `adaptation.feedback_delay_s` (default 0, range [0, 10] s). A decision at
t_c (acquisition, upgrade, downgrade or loss) takes effect at t_c + d; until then the mode in
effect keeps running, but its rate is 0 while A is below that mode's own requirement (its frames
are lost). Causality is fixed-delay: every decision arrives d after it is made and none is
revoked by a later one, so overlapping decisions compose in order. A decision effective at or
after the pass end is dropped (it records a `modcod_decision` event but no `modcod_effective`
one). With d = 0 this is exactly v1's rule applied in continuous time.

Output: `events` (aos, los, culmination, modcod_decision, modcod_effective) and `rate_segments`
(piecewise-constant rate per pair).

## Integration and the time-measure convention

Every duration is the Lebesgue measure of a set of times, in seconds, and every fraction is that
measure divided by the window length T1 − T0. Nothing counts samples, and no interval is added
after the last sample.

- Piecewise-constant quantities (rate, RF-usable indicator, visible indicator, route bottleneck)
  are integrated exactly as the `math.fsum` of value × duration over their segments.
- The Shannon upper bound is integrated over each pass with composite 5-point Gauss-Legendre on
  the pass's interior breakpoints (grid times, culmination, rate-segment ends), every piece first
  split to at most 10 s. Each piece is bisected adaptively until its estimate changes by less
  than 1e-9 relative or 1 bit, with at most 20 levels; failure raises. Nodes are strictly
  interior, so the integrand is never evaluated at a breakpoint, and never later than LOS − τ
  (the value there is held over the last τ).
- No trapezoid rule is used anywhere (v1's single-pass Shannon integral was trapezoidal).

Per pair: pass count, visible and RF-usable durations, integrated rate bits, integrated Shannon
bits, maximum elevation, minimum range (from the pass ends, grid times, culmination and every
root of the range rate), mean rate over visible time and residence time per MODCOD. Per station:
the best-link envelope (the visible satellite with the highest rate; ties to the lowest index;
none when every rate is 0), its integral and mean rate, best-link handovers (changes of best
satellite between adjacent envelope segments that both have one), visible and RF-usable
fractions, and the integral of the fixed-capacity baseline.

### Tabulated gaseous loss for the Shannon integrand and MODCOD root-finding

The quadrature and the threshold root-finding evaluate the link budget at many times per pass.
For those two uses only, the gaseous loss under `itu_reference` comes from a per-station table
instead of a full ray trace: the column's total loss T(e) and the ray's central angle C(e), both
functions of the apparent elevation e alone, are tabulated as piecewise Chebyshev series
(16 segments of degree 16 on [5, 90] deg, equal in log e) from the exact layer sums. The
endpoint solve keeps the exact vacuum geometry and uses C and its derivative from the table.

Error bound. At construction the series are compared with the exact sums at every segment end
and midway between consecutive Chebyshev points; with δT and δC the largest differences found,
the recorded bound is

$$
2\,\delta T+|T'|\left(\frac{2\,\delta C}{|C'|}+2\times10^{-11}\ \mathrm{deg}\right),
$$

with the slopes taken from the neighbouring check points. This bound is **heuristic at the
Chebyshev level**: it assumes the series error between check points is at most twice the error
at them, which is typical of converged Chebyshev series but is not proved. It is backed by an
end-to-end check: each table is also traced against the exact ray at fixed geometries (5 to
90 deg, 110 to 1200 km altitude) and must stay within its bound, and a table is used only when
its bound is at most 2.5e-11 dB, a quarter of the 1e-10 dB limit (a 4× margin). Otherwise
(for example in the 60 GHz oxygen complex) the exact trace is used. Tests sweep 1 to 183 GHz
densely. Each station's bound, or null for the exact trace, is recorded in
`models.shannon_integrand_atmosphere`. Every exported per-sample value (attenuations, C/N0,
Es/N0, SNR, Shannon bound, delays) uses the exact trace; the MODCOD and rate of each sample come
from the rate segments, and so from this path.

## Events disabled (grid left-hold)

With `time.events.enabled = false`, breakpoints are the grid times only: visibility and MODCOD
selection are evaluated at the samples and held to the next sample (left-hold), the Shannon bound
is left-held from each visible sample, and results carry `integration: "grid_left_hold"`. This
is v1's constellation semantics and exists for v1 comparisons and migration evidence, not for
scientific claims; `report` labels it "disabled, grid_left_hold (v1 sampled semantics)". The `step_s ≤ P_min/8` check does not apply.

## Snapshot routing

Route models, the graph rule and tie-breaking are v1's, moved unchanged. Nodes are the satellites
then the stations. Every satellite pair at range 0 < r ≤ `isl.max_range_m` with a clear line of
sight past the WGS84 ellipsoid (tangency occluded) is an inter-satellite link of delay r/c and
capacity `isl.capacity_bps`. Ground links are those of the route pair's two stations only (no
other station relays), one per visible satellite, with the link's delay.

1. `minimum_delay`: Dijkstra over RF-usable ground links (rate > 0), ties to fewer hops then
   lexicographic node indices;
2. `maximum_rate`: the widest path, ties to minimum delay;
3. `fixed_capacity`: minimum delay with `fixed_capacity_bps` on every geometrically visible
   ground link of the two stations, with or without MODCOD lock (a threshold waiver).

Routes are evaluated at **route breakpoints**: the grid plus every pass and rate-segment boundary
of the pairs whose station is a route endpoint. Satellite positions there are computed exactly,
not interpolated, and each route is held until the next breakpoint. Inter-satellite range and
occlusion changes between breakpoints are not resolved in S1 (a recorded limitation). Route
metrics: connected fraction (time measure), integrated bottleneck bits, route changes (changes of
node sequence between consecutive connected intervals that hold time) and mean path delay over
connected time. These are graph snapshots without traffic, contention, queues or transport.

## Convergence gate

After the primary run the engine recomputes every exported integrated metric on the grid refined
by `time.convergence.refinement_factor` (default 4), with the same events and evaluator, and
compares. A metric passes when

$$
|M_{ref}-M|\le\max(r\,|M_{ref}|,\ a),
$$

with r = `relative_tolerance` (default 0.01) and a = `absolute_tolerance_bits` (default 1 bit)
for bits, 1e-6 for fractions, 1e-6 s for mean delays, 1e-3 s for other durations, and 0 for
counts (pass counts, handovers and route changes must match exactly) and rates.

Link (`pair/...`) and station metrics are gated: `mode = "error"` aborts publication and names
the failing metrics and a suggested step; `warn` publishes with one warning per failure; `off`
skips the gate and records that it did. **Route metrics are reported in the `convergence` table
(class `route`) but not gated in S1**: because of the route-breakpoint limitation above, a route
failure is always a warning. With events on, only route metrics and the Shannon integral (through
node placement) can still depend on the grid step.

## Memory budget

Before propagation the run estimates its large arrays and refuses to start above
`--memory-budget` (default 4 GiB): satellites × samples × 6 float64 (ECEF position and velocity)
plus stations × satellites × samples × 4 float64 (elevation, azimuth, range, range rate), and
with a network one chunk of satellite positions at route breakpoints and the inter-satellite
link temporaries of one breakpoint. The estimate does not count the `samples` table or the other
result tables, nor the per-worker duplication of the catalogs, geometry and evaluator under
`--workers N`; peak memory can exceed it.

## Workers and determinism

`--workers N` splits the per-satellite phases over processes; results are byte-identical to one
worker. On one platform, identical inputs give byte-identical outputs (CSV floats are written
with `repr`, JSON with sorted keys). Across platforms, numeric tables agree within the tolerances
of [Validation](VALIDATION.md); CI checks this with `scripts/compare_results.py`.

## Interpretation

Reproducibility means repeating the declared computation. Physical validation requires
independent observations with adequate timing, station, antenna, receiver, processing and
meteorological metadata; passing software tests does not supply that evidence. Apply the
[scope and claim boundary](SCOPE.md) when reporting results.
