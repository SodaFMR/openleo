# OpenLEO Research and Engineering Charter

**Status:** Canonical project specification<br>
**Last reviewed:** 2026-08-30<br>
**Working software name:** OpenLEO<br>
**Planned distribution name:** `openleo-link`<br>
**Planned import package and command:** `openleo`<br>
**Language:** English for all project artifacts

## 1. Purpose of this document

This charter is the durable source of truth for OpenLEO. It records why the project
exists, what it will and will not claim, the smallest useful release, the physics that
must be understood, the validation hierarchy, the cross-platform architecture, the
Git workflow, and the route to a scientific paper.

Future contributors should read this file before changing the project.
When implementation details conflict with this charter, the charter wins unless it is
explicitly amended with a documented scientific or architectural decision.

## 2. Mission and scientific identity

OpenLEO will provide open, inspectable, and reproducible time-varying link traces for
LEO satellite-to-ground communications. Its purpose is to help researchers replace
opaque or fixed-capacity ground-link assumptions with models whose geometry, RF
parameters, standards, uncertainties, and limitations are visible.

OpenLEO belongs primarily to satellite communications, radio propagation, scientific
software, and network simulation. It uses astrodynamics, but it is not an astronomy
observatory, an orbit-determination system, or an operational commercial-network model.

The long-term research question is:

> Under explicitly documented orbital, terminal, propagation, and adaptation
> assumptions, when does a time-varying ground-link model materially change outage,
> capacity, gateway-selection, routing, or packet-level conclusions relative to a
> fixed-capacity baseline?

## 3. Why this project should exist

Hypatia made large LEO-network experiments reproducible, but its ground-link and
physical-layer representation is intentionally basic. Other tools model parts of the
physical link, propagation, waveform, or packet network, but researchers still need a
small, reusable bridge that emits scientifically traceable link-state time series
without requiring a full RF or network simulator.

OpenLEO will not compete by containing the most models. It will contribute:

1. a minimal path from archived public orbital elements to an auditable link trace;
2. explicit model ablations from geometry through propagation and adaptive rate;
3. standards-version tracking and reproducible reference cases;
4. honest uncertainty and validation boundaries; and
5. simple outputs that independent network simulators can consume later.

## 4. Scientific integrity rules

The following rules are non-negotiable:

- Every external input records its source, retrieval time, epoch, SHA-256 checksum, and
  applicable data license or terms URL.
- Every numeric field exposes its unit in its name or schema documentation.
- Every time value is timezone-aware UTC at the public boundary.
- Every frame transformation is named; TEME, Earth-fixed, and topocentric coordinates
  are never described generically as “satellite coordinates.”
- Every equation cites a reference and is validated independently before integration.
- A theoretical Shannon-Hartley value is always labelled an upper bound, never
  throughput.
- A statistical ITU-R exceedance model is never labelled instantaneous weather.
- Public SatNOGS observations are not treated as calibrated power measurements unless
  station gain, antenna pattern, noise temperature, receiver settings, oscillator,
  timestamps, processing, and weather are independently documented.
- Real commercial constellations are not claimed without sufficient public terminal,
  payload, beam, gateway, scheduler, interference, and protocol information.
- Synthetic scenarios are labelled synthetic even when their orbital geometry is based
  on public constellation filings or element sets.
- Failed propagation, stale elements, invalid units, and non-finite results fail loudly
  or produce an explicit warning; they are never silently discarded.

## 5. Research hypotheses

These are questions to test, not conclusions to assume:

- **H1:** A constant ground-link capacity can materially overestimate pass-integrated
  information transfer when it represents a high-elevation or best-case link state.
- **H2:** Minimum-delay routing is not always the highest-availability or
  highest-throughput choice once ground-link state varies with geometry and propagation.
- **H3:** The importance of link fidelity depends on orbit altitude, elevation mask,
  frequency, terminal assumptions, atmospheric availability percentile, and gateway
  geography.
- **H4:** A predictive link-aware route can trade a small latency increase for fewer
  imminent outages or failed handovers.

v0.1 builds the measurement instrument needed to investigate H1. H2–H4 belong to later
network milestones.

## 6. Versioned scope

### 6.1 v0.1: deterministic free-space pass trace

v0.1 models one one-way downlink between one LEO spacecraft and one fixed ground
station over a bounded UTC time interval containing exactly one contiguous visible
pass. The first and last samples must be below the elevation mask so the sampled pass
is bracketed rather than truncated. A window containing no complete pass or more than
one pass is rejected with guidance to adjust the interval.

Inputs:

- one frozen CelesTrak GP CSV record using OMM-compatible field names;
- source URL, retrieval timestamp, SHA-256 checksum, and data license or terms URL;
- WGS84 geodetic station latitude, longitude, and height;
- UTC start and stop timestamps, sampling interval, and minimum elevation;
- carrier frequency and one ideal rectangular channel bandwidth, used as both channel
  bandwidth and equivalent noise bandwidth in v0.1;
- EIRP, receive antenna gain, system noise temperature, and scalar miscellaneous loss.

Outputs for every visible sample:

- UTC timestamp;
- azimuth and geometric elevation;
- slant range and range rate;
- one-way vacuum propagation delay;
- nominal first-order Doppler shift;
- free-space path loss;
- received carrier power;
- noise spectral density, `C/N0`, and SNR; and
- Shannon-Hartley capacity upper bound.

Run-level outputs:

- `trace.csv` with the time series;
- `summary.json` with sample-grid AOS and LOS estimates, duration, extrema, integrated
  theoretical bits, sampling interval, element epoch and signed ages, parsed scenario
  inputs, authoritative scenario-byte and leap-second-table fingerprints, warnings,
  versions, and provenance; and
- deterministic console output suitable for CI and teaching.

### 6.2 Explicit v0.1 non-goals

v0.1 does not include:

- constellation routing or inter-satellite links;
- Hypatia, ns-3, SNS-3, Mininet, packet queues, TCP, UDP, or traffic matrices;
- atmospheric gases, rain, cloud, fog, scintillation, or instantaneous weather;
- MODCOD tables, BER, FER, coding, demodulation, or waveform samples;
- live CelesTrak or Space-Track downloads during scientific runs;
- SatNOGS audio, waterfall, or HDF5 processing;
- an SDR, antenna rotor, receiver, or hardware control;
- antenna radiation patterns, beam steering, interference, or polarization dynamics;
- a GUI, web service, map, or 3D viewer;
- orbit determination, maneuver inference, covariance propagation, or operational
  prediction; or
- Starlink, Kuiper, OneWeb, or another operator’s actual performance.

### 6.3 Later milestones

- **v0.2 — propagation and uncertainty:** implement only required subsets of current,
  in-force ITU-R recommendations; add deterministic sensitivity and uncertainty
  intervals.
- **v0.3 — adaptive link state:** add cited MODCOD thresholds, implementation margin,
  hysteresis, useful-rate estimates, and outage state.
- **v0.4 — network adapter:** export versioned capacity/delay/availability traces and
  compare fixed and dynamic ground links in a small synthetic network scenario.
- **v1.0 — paper release:** freeze benchmark inputs, run full ablations and uncertainty
  analysis, regenerate all figures/tables with one command, archive code and derived
  data, and mint a DOI.

## 7. v0.1 physical model

### 7.1 Orbit data and propagation

The canonical input is CelesTrak GP CSV, whose fields follow OMM keywords. This avoids
the legacy TLE format’s fixed-width parsing, five-digit catalog limit, and lower
extensibility. The original source record is preserved verbatim.

Skyfield supplies the corrected Vallado SGP4 implementation and the transformation from
the SGP4 TEME state to a topocentric observation. SGP4 is used because the public mean
elements were fitted for SGP4; they are not ordinary Keplerian elements and must not be
propagated with a two-body solver.

Element age is reported for every run. v0.1 warns when any requested epoch is more than
14 days from the element epoch. That warning is a quality indicator, not a covariance
or accuracy guarantee.

### 7.2 Geometry

Let the satellite and station position vectors be expressed in a common frame. Define
the relative vector and slant range as

$$
\boldsymbol{\rho}=\mathbf r_{sat}-\mathbf r_{station}, \qquad
\rho=\lVert\boldsymbol{\rho}\rVert.
$$

With relative velocity $\dot{\boldsymbol{\rho}}$, range rate is

$$
\dot\rho = \frac{\boldsymbol{\rho}\cdot
\dot{\boldsymbol{\rho}}}{\rho}.
$$

The public sign convention is:

- `range_rate_mps > 0`: separation is increasing;
- `range_rate_mps < 0`: the spacecraft is approaching.

Only samples at or above the configured geometric elevation mask are emitted as
visible. Atmospheric refraction is not applied in v0.1.

### 7.3 Delay and Doppler

One-way vacuum delay is

$$
\tau(t)=\frac{\rho(t)}{c}.
$$

The nominal first-order downlink Doppler shift is

$$
\Delta f_D(t)=-f_c\frac{\dot\rho(t)}{c}.
$$

Under the declared sign convention, an approaching spacecraft has negative range rate
and positive received-frequency shift. Relativistic, ionospheric, oscillator, and
transmitter-frequency effects are outside v0.1.

### 7.4 Free-space link budget

Following ITU-R P.525, free-space basic transmission loss is

$$
L_{FSPL}(t)=20\log_{10}\left(\frac{4\pi\rho(t)f_c}{c}\right)\;\mathrm{dB}.
$$

Received carrier power is

$$
C(t)=EIRP+G_{rx}-L_{FSPL}(t)-L_{misc}\;\mathrm{dBW}.
$$

System noise-density power is

$$
N_0=10\log_{10}(k_B T_{sys})\;\mathrm{dBW/Hz}.
$$

Therefore,

$$
\frac{C}{N_0}(t)=C(t)-N_0\;\mathrm{dB\,Hz},
$$

and for v0.1 ideal rectangular channel bandwidth $B$, which is also treated as the
equivalent noise bandwidth,

$$
SNR(t)=\frac{C}{N_0}(t)-10\log_{10}(B)\;\mathrm{dB}.
$$

The Shannon-Hartley capacity upper bound is

$$
C_{Shannon}(t)=B\log_2\left(1+10^{SNR(t)/10}\right)\;\mathrm{bit/s}.
$$

This bound does not model a modem, coding loss, spectral mask, protocol overhead,
interference, or achieved goodput.

### 7.5 Pass integration

The theoretical information bound is a sample-grid estimate. v0.1 identifies AOS and
LOS as the first and last samples meeting the elevation mask, then applies trapezoidal
integration only between adjacent emitted visible samples:

$$
I_{bound}=\int_{AOS}^{LOS} C_{Shannon}(t)\,dt.
$$

It does not interpolate the true elevation-mask crossing. The summary records the
sampling interval and names the quantity `integrated_capacity_upper_bound_bits`; it
must never be called transferred data or delivered bits. Sampling-resolution
sensitivity is required before this value supports a paper claim.

## 8. Units, frames, and time conventions

Public schemas use:

- metres, seconds, metres per second, hertz, kelvin, watts, bit/s;
- degrees for user-facing geodetic, azimuth, and elevation values;
- dBW for absolute logarithmic power;
- dBi for antenna gain;
- dB for ratios and losses;
- dB-Hz for `C/N0`; and
- ISO 8601 UTC timestamps ending in `Z`.

Internal names retain unit suffixes. Mixing dBm and dBW, kilometres and metres, naive
and aware datetimes, or linear and logarithmic values is rejected at system boundaries.

Skyfield owns the v0.1 TEME/Earth-fixed/topocentric transformations. OpenLEO records
the Skyfield, SGP4, and leap-second data versions in provenance rather than duplicating
those transformations.

## 9. Input validation and failure behavior

External data are untrusted. The parser validates:

- required GP CSV/OMM-compatible fields and a single selected record;
- absolute HTTP(S) source URL, retrieval timestamp, absolute HTTP(S) data license or
  terms URL, and optional expected checksum;
- numeric finiteness and physically meaningful ranges;
- station latitude in `[-90, 90]`, longitude in `[-180, 180]`, and finite height;
- aware UTC start/stop timestamps with `start < stop`;
- sampling interval exactly representable at Python datetime's microsecond resolution,
  positive and no larger than the run duration;
- an inclusive time grid of at most 100,000 samples, calculated before propagation;
- positive frequency, positive bandwidth, positive gain-chain temperature,
  non-negative miscellaneous loss, and finite RF values;
- elevation mask in `[0, 90)`;
- exactly one contiguous visible-pass segment in the requested window; and
- first and last time-grid samples below the elevation mask;
- SHA-256 when an expected checksum is supplied.

Errors identify the field, invalid value, expected constraint, and source file. The CLI
returns a non-zero status without a Python traceback for ordinary user-input errors.
Unexpected internal failures retain diagnostic context.

## 10. Validation hierarchy

OpenLEO distinguishes software verification from physical validation.

1. **Hand-derived invariants:** verify units and equations using literal reference
   values independent of production helpers.
2. **SGP4 reference behavior:** use a frozen element record and compare propagation with
   Vallado/Skyfield reference behavior within a declared tolerance.
3. **Geometry sanity:** compare a representative production sample against an
   independent ECEF-to-ENU topocentric calculation, then check monotonic range around
   closest approach, finite-difference range rate, delay, and both Doppler signs.
4. **Static link-budget case:** reproduce an independently hand-calculated FSPL,
   received power, noise, `C/N0`, SNR, and theoretical capacity bound.
5. **Cross-platform regression:** run the same frozen scenario on Ubuntu, macOS, and
   Windows and compare within numeric tolerances.
6. **ITU-R validation:** later atmospheric implementations reproduce the validation
   examples for the exact Recommendation version before integration.
7. **Public observational sanity check:** SatNOGS can support pass timing and signal
   presence comparisons, but not absolute RF validation by default.
8. **Calibrated experiment:** absolute RF validation requires a documented station and
   calibrated acquisition chain; this is a later collaboration or hardware phase.

Initial hand-check values include:

- FSPL at 2.2 GHz and 1,000 km: approximately 159.3 dB;
- one-way vacuum delay at 1,000 km: approximately 3.336 ms;
- Doppler magnitude at 2.2 GHz and 6 km/s: approximately 44.0 kHz; and
- `C/N0` for 10 dBW EIRP, 161 dB total loss, and 0 dB/K `G/T`:
  approximately 77.6 dB-Hz. The same `G/T` is obtained, for example, from
  $G_{\mathrm{rx}}=23.0103\,\mathrm{dBi}$ and $T_{\mathrm{sys}}=200\,\mathrm{K}$.

## 11. Uncertainty policy

v0.1 fingerprints the authoritative raw scenario bytes and reports parsed input values
with documented output precision, along with element epoch age, assumptions, and model
limitations. Parsed JSON numbers do not preserve their original textual precision. v0.1
does not invent a position covariance from GP elements or attach statistically
unsupported error bars.

v0.2 will introduce deterministic sensitivity ranges first. Seeded Monte Carlo or
another probabilistic method is allowed only after each input distribution is justified.
Planned uncertainty groups are:

- orbital-element age and propagation error;
- station position and timestamp error;
- EIRP and antenna-gain uncertainty;
- system-noise temperature;
- pointing, polarization, feeder, and implementation loss;
- atmospheric model and exceedance percentile; and
- temporal sampling resolution.

Reported intervals must distinguish epistemic assumptions from measured variability.

## 12. SatNOGS policy

SatNOGS Network and DB APIs expose valuable open observation, station, satellite, and
transmitter metadata. Public observations are useful for selecting real passes and
checking contact timing or signal presence.

They are not automatically calibrated link-budget measurements:

- the client compensates predicted Doppler during acquisition;
- waterfalls are processed images and normally auto-ranged;
- raw I/Q or HDF5 artifacts are optional;
- station antennas, LNAs, filters, cables, SDR gain, oscillators, noise, interference,
  and metadata quality differ; and
- many available signals are VHF/UHF amateur or educational beacons, not commercial
  broadband Ku/Ka links.

No OpenLEO paper may infer absolute power or commercial throughput from these products
without a complete calibration argument.

## 13. Atmospheric-model policy

The atmospheric milestone will use current, in-force Recommendations rather than
silently relying on older library defaults. Relevant references currently include:

- ITU-R P.618-14 for Earth-space propagation design;
- ITU-R P.676-13 for gaseous attenuation and related effects;
- ITU-R P.837-8 for precipitation characteristics;
- ITU-R P.838-3 for rain specific attenuation;
- ITU-R P.839-4 for rain height; and
- ITU-R P.840-9 for clouds and fog.

ITU-Rpy 0.4.0 is not a v0.1 dependency because it bundles older Recommendation versions
for several models and brings a large scientific dependency stack. A later milestone
may contribute updates upstream, implement a small cited subset, or wrap another
validated implementation. That decision will be based on reference-example agreement,
license compatibility, and maintenance status.

## 14. Minimal software architecture

v0.1 is a library with a thin standard-library CLI. It is not a framework.

Planned layout:

```text
openleo/
├── LICENSE
├── README.md
├── pyproject.toml
├── uv.lock
├── docs/
│   ├── FUNDAMENTALS.md
│   ├── PROJECT_CHARTER.md
│   └── VALIDATION.md
├── examples/
│   ├── data/
│   └── scenarios/
├── src/openleo/
│   ├── __init__.py
│   ├── model.py
│   ├── orbit.py
│   ├── physics.py
│   ├── simulation.py
│   ├── output.py
│   └── cli.py
└── tests/
```

Responsibilities:

- `model.py`: frozen dataclasses and boundary validation;
- `orbit.py`: frozen GP record loading and Skyfield propagation;
- `physics.py`: pure delay, Doppler, FSPL, noise, and capacity equations;
- `simulation.py`: combine validated inputs into visible trace rows and summary;
- `output.py`: deterministic CSV and JSON serialization;
- `cli.py`: `argparse` entry point and user-facing error handling.

There are no provider interfaces, plugin managers, factories, repositories, service
containers, custom exception hierarchies, or configuration frameworks. A boundary is
introduced only when a second real implementation requires it.

## 15. Cross-platform development and distribution

- Python requirement: `>=3.12`, without a speculative upper bound.
- Packaging: standard `pyproject.toml` with Hatchling and a `src/` layout.
- Development environment: `uv` and a committed cross-platform `uv.lock`.
- Standard fallback: build/install through ordinary PEP 517-compatible Python tools.
- Initial runtime dependency: Skyfield 1.55 or newer.
- Initial developer tools: pytest, coverage, and Ruff.
- Paths: `pathlib`; text: UTF-8; newlines: platform-independent.
- Frozen CSV and JSON scientific inputs use repository-enforced LF endings so raw-byte
  provenance fingerprints survive Windows checkout unchanged.
- CLI: `argparse`; no shell wrapper is required.
- Tests: no live network, wall-clock dependence, local timezone dependence, or
  platform-specific process behavior.
- CI: Python 3.12 on Ubuntu, macOS, and Windows, plus newer supported Python versions on
  Ubuntu.
- Containers: excluded until a hosted service, native dependency, or demonstrated
  reproducibility problem requires one.
- Conda: not required for v0.1.

On Arch Linux/Omarchy, contributors use the same `uv` workflow as macOS and Windows;
the system Python and rolling-release package versions do not define the project
environment.

## 16. Test strategy

Behavior changes follow strict red-green-refactor development:

1. write one small test with a hand-derived expectation;
2. run it and confirm the expected failure;
3. implement the minimum behavior;
4. run the focused and full suites;
5. refactor only while green; and
6. commit the behavior and its test together.

Required test layers:

- unit tests for validation and each physical equation;
- integration tests for one frozen pass from input to CSV/JSON;
- CLI smoke tests for success and invalid input;
- cross-platform CI regression; and
- a paper reproduction test when the paper workflow exists.

Coverage must remain at least 80%, but tests exist to catch meaningful scientific and
software regressions rather than to inflate a percentage.

## 17. Git and review workflow

1. create a focused branch such as `feat/pass-trace` or `docs/link-budget-note`;
2. make small Conventional Commits;
3. push the branch, never implementation commits directly to `main`;
4. open a pull request describing the scientific change, assumptions, tests, and
   generated artifacts;
5. run formatting, linting, unit/integration tests, coverage, package build, example
   execution, and the operating-system CI matrix;
6. obtain code review and address all critical and important findings; and
7. merge only when the branch is fully verified.

Approved commit types and examples:

- `docs: define OpenLEO research charter`
- `test: specify free-space loss behavior`
- `feat: compute free-space path loss`
- `fix: reject timezone-naive scenario timestamps`
- `refactor: centralize logarithmic unit validation`
- `perf: vectorize pass geometry calculation`
- `ci: test supported operating systems`
- `chore: update locked development dependencies`

`add:` is not a Conventional Commit type; adding user-visible behavior uses `feat:`.

## 18. Study curriculum

Do not postpone all development until every advanced topic is mastered. Learn the
concept required for the next module, solve its hand exercise, implement it under test,
and then move forward.

### 18.1 Required before the first physics implementation

#### A. Units, vectors, and logarithmic quantities

Understand:

- SI units and dimensional analysis;
- vector subtraction, dot product, norm, and velocity projection;
- linear ratios versus dB;
- dBW versus dBm;
- dBi, dB/K, and dB-Hz; and
- when logarithmic quantities may be added or subtracted.

Ready when you can independently calculate:

- 30 dBm = 0 dBW;
- FSPL at 2.2 GHz and 1,000 km ≈ 159.3 dB; and
- the link-budget `C/N0` example in Section 10.

#### B. LEO orbits, GP data, and SGP4

Understand:

- circular-orbit speed and period at an order-of-magnitude level;
- mean versus osculating elements;
- why TLE/GP elements are fitted to SGP4;
- the element epoch and degradation away from it;
- perturbations represented by SGP4 at a conceptual level; and
- why OMM-compatible CSV is preferred over legacy TLE text.

Exercise: estimate the period of a 550 km circular orbit using
$T=2\pi\sqrt{a^3/\mu}$. The expected result is about 95.6 minutes.

#### C. Time scales and Earth orientation

Understand:

- UTC, TAI, TT, and UT1;
- leap seconds and timezone-aware timestamps;
- Earth rotation and polar motion conceptually; and
- why a one-second timing error corresponds to kilometres of spacecraft motion.

Exercise: at 7.6 km/s, calculate the along-track distance associated with a one-second
timestamp error.

#### D. Frames and topocentric geometry

Understand:

- geodetic versus geocentric coordinates;
- WGS84 latitude, longitude, and ellipsoidal height;
- TEME, Earth-fixed/ITRS-like, and topocentric ENU frames;
- azimuth, elevation, horizon masks, and slant range; and
- geometric line of sight versus apparent elevation with refraction.

Exercise: with Earth radius 6,378 km and altitude 550 km, estimate the horizon slant
range $\sqrt{(R_E+h)^2-R_E^2}$, approximately 2,700 km.

#### E. Range rate, Doppler, and delay

Understand the equations and sign convention in Sections 7.2 and 7.3.

Exercise: calculate the one-way delay for 1,000 km and Doppler magnitude for a 2.2 GHz
carrier at 6 km/s radial speed.

#### F. Link budget and receiver noise

Understand:

- EIRP, antenna gain, effective aperture, feeder and pointing losses;
- thermal noise, Boltzmann constant, noise temperature, and noise figure;
- `G/T`, `C/N0`, SNR, `Eb/N0`, and link margin;
- bandwidth versus bit rate; and
- why a capacity bound is not modem throughput.

Exercise: derive both the received-power/noise-density form and the equivalent `G/T`
form of `C/N0` and show that they agree.

### 18.2 Learn during v0.2–v0.3

#### G. Antennas

- aperture efficiency and parabolic gain;
- beamwidth, pointing, polarization, and gain versus elevation;
- receiver cascade temperature and noise figure; and
- calibrated versus nominal terminal parameters.

#### H. Atmospheric propagation

- gases, rain, clouds/fog, scintillation, and elevation dependence;
- rainfall rate and effective path length;
- annual/monthly exceedance probabilities and availability;
- statistical planning versus measured instantaneous weather; and
- Recommendation validity ranges and versions.

#### I. MODCOD and useful rate

- CCM, VCM, and ACM;
- `Eb/N0` or `Es/N0` thresholds;
- spectral efficiency, roll-off, coding, and frame overhead;
- implementation margin and hysteresis; and
- BER/FER target dependence.

Start with a cited lookup table. Do not implement LDPC/BCH codecs.

#### J. Uncertainty and experiment design

- verification versus validation;
- uncertainty budgets and sensitivity analysis;
- correlation and justified input distributions;
- ablation studies;
- hypothesis tests, effect sizes, and confidence intervals; and
- reproducible random seeds and provenance.

Use the JCGM Guide to the Expression of Uncertainty in Measurement as the terminology
reference.

### 18.3 Learn before network and paper milestones

#### K. Dynamic graphs and routing

- time-varying edge capacity, delay, availability, and contact time;
- shortest-path and widest-path baselines;
- handover, route churn, and snapshot resolution;
- TCP/UDP metrics and the difference between capacity and goodput; and
- discrete-event simulation assumptions.

#### L. Reproducible scientific publication

- related-work and novelty searches;
- pre-registered hypotheses and success criteria;
- configuration, data, environment, and random-seed freezing;
- figure/table regeneration;
- software and data licensing;
- CITATION metadata, semantic versions, release archives, and DOI creation; and
- limitations and negative results.

### 18.4 Minimal authoritative reading set

1. CelesTrak GP data formats and Vallado et al., “Revisiting Spacetrack Report #3.”
2. Skyfield Earth-satellite, time, and coordinate documentation.
3. ITU-R P.525-5, P.618-14, P.676-13, P.837-8, P.838-3, P.839-4, and P.840-9.
4. CCSDS 401.0-B and, when MODCOD begins, CCSDS 131.3-B / DVB-S2 documentation.
5. NASA Small Spacecraft Technology State of the Art, communications chapter.
6. JCGM 100, Guide to the Expression of Uncertainty in Measurement.

## 19. Development and learning roadmap

### Phase 0: foundation

- study Sections 18.1A–18.1F and complete the hand calculations;
- establish packaging, quality tools, CI, contribution guidance, and citation metadata;
- freeze one historical GP CSV and transparent synthetic RF scenario; and
- document formulas and allowed claims.

### Phase 1: geometry instrument

- validate inputs and provenance;
- propagate a frozen record;
- produce visible azimuth/elevation/range/range-rate samples;
- calculate delay and Doppler; and
- cross-check one pass independently.

### Phase 2: free-space RF instrument

- implement and hand-validate FSPL, noise, `C/N0`, SNR, and theoretical capacity;
- produce deterministic trace and summary files;
- integrate theoretical capacity over the pass; and
- run the same scenario across supported operating systems.

### Phase 3: uncertainty and atmosphere

- add deterministic sensitivity first;
- implement validated current-version atmospheric subsets;
- compare the free-space baseline and specified-availability models; and
- add justified uncertainty intervals.

### Phase 4: adaptive and network experiments

- add documented MODCOD selection;
- emit capacity, delay, availability, margin, and remaining-contact traces;
- define fixed-capacity baselines;
- integrate a small synthetic network scenario; and
- add Hypatia or another backend only through an adapter.

### Phase 5: publication

- freeze experiment configurations and data;
- run model ablations and uncertainty analysis;
- reproduce all figures and tables with one command;
- perform independent scientific and software review;
- release a versioned archive and derived dataset with DOI; and
- submit the technical note or research paper.

## 20. Paper plan

### 20.1 Working title

**From Fixed Capacity to Time-Varying Ground Links: A Reproducible Pass-Level
Fidelity Study for LEO Network Simulation**

This title avoids asserting that a simulator is “wrong” before calibrated truth exists.

### 20.2 Planned contribution

The first paper will not claim a new orbit propagator, atmosphere model, modem, or
packet simulator. It will contribute a reproducible fidelity study and reusable trace
generator that connects:

$$
\text{archived orbit}\rightarrow\text{geometry}\rightarrow\text{link budget}
\rightarrow\text{adaptation}\rightarrow\text{link-state trace}.
$$

### 20.3 Model ablations

- **M0:** explicit fixed-capacity baseline;
- **M1:** time-varying geometry and FSPL;
- **M2:** complete transparent free-space RF budget;
- **M3:** statistical atmosphere at declared availability percentile;
- **M4:** documented MODCOD and useful-rate trace; and
- **M5:** optional network consumption of the trace.

### 20.4 Metrics

- contact duration and elevation distribution;
- FSPL, `C/N0`, SNR, margin, and outage duration;
- theoretical and MODCOD-limited rate percentiles;
- pass-integrated transferable-information estimate;
- fixed-versus-dynamic relative and absolute differences;
- route changes, handovers, latency, loss, and goodput only after M5; and
- runtime and storage cost.

### 20.5 Validation required before submission

- every equation independently checked against an authoritative example;
- SGP4/geometry cross-check using frozen inputs;
- current Recommendation versions and validity ranges documented;
- at least one independent implementation or calibrated case for the quantities used
  as empirical evidence;
- full ablation and uncertainty study;
- all supported operating systems reproduce results within tolerances;
- every figure and table regenerated by one documented command; and
- limitations explicitly separate public facts, synthetic assumptions, theoretical
  bounds, and inference.

### 20.6 Publication stages

1. repository technical note explaining v0.1 methods and limitations;
2. public preprint after the ablation/uncertainty benchmark is complete;
3. software-paper submission if the package demonstrates substantial research use;
4. telecommunications or network-simulation paper for the full fidelity study; and
5. a separate predictive-routing paper only if later results support it.

## 21. Related work and positioning

OpenLEO must compare against and cite, where relevant:

- Hypatia for reproducible LEO topology, routing, ns-3 packet simulation, and
  visualization;
- ns-3-leo and SNS-3 for satellite mobility, channel, and link/network modelling;
- gr-leo for GNU Radio satellite-channel impairment simulation;
- Skyfield/SGP4 for orbit propagation and topocentric geometry;
- ITU-Rpy and official ITU-R validation products for propagation modelling; and
- SatNOGS for open observation and transmitter/station metadata.

OpenLEO’s differentiator is not ownership of these equations. It is the small,
traceable integration boundary, current standards discipline, controlled ablation,
uncertainty analysis, cross-platform reproducibility, and honest empirical claims.

## 22. Main risks and controls

| Risk | Control |
|---|---|
| Scope expands into a general simulator | Enforce the versioned non-goals and require a new charter decision. |
| GP elements are treated as truth | Preserve epoch age, provenance, limitations, and cross-checks. |
| Frames or times are mixed | Delegate transformations to Skyfield, use aware UTC boundaries, and test signs/units. |
| dB or unit errors create plausible output | Use explicit suffixes, boundary validation, hand-derived tests, and no implicit conversions. |
| Shannon bound is reported as throughput | Encode “upper bound” in schema names, docs, plots, and paper text. |
| ITU models are outdated or misused | Pin the Recommendation number, reproduce validation examples, and distinguish statistics from weather. |
| SatNOGS is overinterpreted | Apply Section 12 and require calibration for absolute claims. |
| Hypatia maintenance blocks progress | Keep it outside the core and integrate only through trace files later. |
| Cross-platform support drifts | Maintain OS CI and avoid platform-specific runtime behavior. |
| Dependency or data drift breaks a paper | Commit lockfile, frozen small inputs, checksums, terms, versions, and derived summaries. |
| Project name collides with unrelated OpenLEO work | Publish as `openleo-link`, state the satcom scope, and re-check registries before release. |

## 23. Definition of done

v0.1 is complete only when a new contributor on Linux, macOS, or Windows can:

1. clone the repository;
2. create the documented Python environment;
3. run the full verification suite;
4. execute one frozen scenario without network access;
5. obtain deterministic CSV and JSON outputs;
6. reproduce the documented hand-check values within tolerance;
7. understand every input, output, assumption, warning, and limitation from the docs;
8. build both a wheel and source distribution;
9. inspect the source distribution to exclude generated, local, absolute, or private
   worktree artifacts; and
10. see the same required CI checks pass on the pull request.

The release must also have at least 80% coverage, no critical or high-severity review
findings, an OSI-approved license, citation metadata, and an archived version tag.

## 24. Decision log

- **2026-08-30:** OpenLEO selected as the initial research direction
  because it offers a more controllable path to a useful cross-platform repository and
  article while remaining executable from home.
- **2026-08-30:** The project is positioned as satellite communications rather than
  astronomy or operational constellation prediction.
- **2026-08-30:** v0.1 restricted to one archived orbit, one station, deterministic
  geometry, free-space RF, and theoretical capacity bound.
- **2026-08-30:** CelesTrak GP CSV with OMM-compatible fields selected as the canonical
  orbital input; legacy TLE remains a possible later import format.
- **2026-08-30:** Skyfield selected as the initial propagation/coordinate dependency;
  Astropy, SciPy, ITU-Rpy, Hypatia, ns-3, and containers deferred.
- **2026-08-30:** Python 3.12+, standard packaging, `uv`, Hatchling, pytest, coverage,
  Ruff, and cross-platform CI selected.
- **2026-08-30:** SatNOGS restricted to qualitative sanity checks until calibrated data
  exist.
- **2026-08-30:** English selected for all artifacts.
- **2026-08-30:** MIT selected as the initial open-source license.
- **2026-08-30:** MIT applies to original code and documentation only; every archived
  third-party data record carries its own license or terms provenance.
- **2026-08-31:** v0.1 time grids limited to microsecond-representable intervals and
  100,000 samples to prevent non-advancing or resource-exhausting runs.
- **2026-08-31:** detached summaries include parsed inputs, sampling interval, element
  epoch/ages, and a fingerprint of Skyfield's builtin leap-second table.
- **2026-08-31:** scenario provenance fingerprints the authoritative raw JSON bytes;
  serialized inputs report parsed values rather than claiming to preserve source-text
  numeric precision.
- **2026-08-31:** release verification includes stable extreme-SNR math, URL boundary
  checks, independent ECEF-to-ENU geometry, finite-difference/sign/range validation,
  and explicit sdist inspection.

## 25. Authoritative references

### Orbit, time, and coordinates

- CelesTrak, [A New Way to Obtain GP Data](https://celestrak.org/NORAD/documentation/gp-data-formats.php).
- CCSDS 502.0-B-3, [Orbit Data Messages](https://ccsds.org/Pubs/502x0b3e1.pdf).
- Vallado et al., [Revisiting Spacetrack Report #3](https://celestrak.org/publications/AIAA/2006-6753/AIAA-2006-6753-Rev3.pdf).
- Skyfield, [Earth Satellites](https://rhodesmill.org/skyfield/earth-satellites.html).
- IERS, [Conventions (2010), Technical Note 36](https://www.iers.org/IERS/EN/Publications/TechnicalNotes/tn36.html).

### Radio link and propagation

- ITU-R P.525-5, [Calculation of free-space attenuation](https://www.itu.int/rec/R-REC-P.525-5-202411-I/en).
- ITU-R P.618-14, [Earth-space propagation data and prediction methods](https://www.itu.int/rec/R-REC-P.618-14-202308-I/en).
- ITU-R P.676-13, [Attenuation by atmospheric gases and related effects](https://www.itu.int/rec/R-REC-P.676-13-202208-I/en).
- ITU-R P.837-8, [Characteristics of precipitation for propagation modelling](https://www.itu.int/rec/R-REC-P.837/en).
- ITU-R P.838-3, [Specific attenuation model for rain](https://www.itu.int/rec/R-REC-P.838/en).
- ITU-R P.839-4, [Rain height model](https://www.itu.int/rec/R-REC-P.839/en).
- ITU-R P.840-9, [Attenuation due to clouds and fog](https://www.itu.int/rec/R-REC-P.840-9-202308-I/en).
- NASA, [Small Spacecraft Technology State of the Art](https://www.nasa.gov/smallsat-institute/sst-soa/).
- CCSDS, [Blue Books: Recommended Standards](https://ccsds.org/publications/bluebooks/).
- DVB, [Specifications](https://dvb.org/specifications/).

### Validation, software, and publication

- JCGM 100:2008, [Guide to the Expression of Uncertainty in Measurement](https://www.bipm.org/documents/20126/2071204/JCGM_100_2008_E.pdf).
- Hypatia, [LEO satellite network simulation framework](https://github.com/snkas/hypatia).
- Manzanares-Lopez et al., [Review of ns-3-based LEO simulation frameworks](https://doi.org/10.1002/spe.70001).
- SatNOGS Network, [API documentation](https://docs.satnogs.org/projects/satnogs-network/en/latest/api.html).
- SatNOGS Client, [User guide](https://docs.satnogs.org/projects/satnogs-client/en/latest/userguide.html).
- Journal of Open Source Software, [Review criteria](https://joss.readthedocs.io/en/latest/review_criteria.html).
- Python Packaging User Guide, [`pyproject.toml`](https://packaging.python.org/en/latest/guides/writing-pyproject-toml/).
- uv, [Project management](https://docs.astral.sh/uv/guides/projects/).
- GitHub, [Building and testing Python](https://docs.github.com/en/actions/tutorials/build-and-test-code/python).
