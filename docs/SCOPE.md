# Scope and scientific claim boundary

OpenLEO produces inspectable model outputs for time-varying satellite-to-ground
links, snapshot routes, and bounded synthetic packet experiments. Archived
public orbital elements and explicit geometry, RF, atmosphere, and network
assumptions support repeatable comparisons; they do not describe an
operational system.

## Supported research uses

OpenLEO can:

- simulate 1 to N satellites from archived GP records against 1 to M stations, with
  root-found AOS/LOS, refined culminations and continuous-time MODCOD switches;
- integrate an idealized P.835-7/P.453-14/P.676-13 atmosphere;
- optionally subtract declared uniform rain and cloud loss before adaptation;
- select ideal AWGN reference MODCODs with hysteresis and an optional feedback delay, and
  report RF outages;
- integrate rates, durations and fractions on one time-measure convention and check every
  exported integrated metric against a refined grid;
- compare minimum-delay, maximum-rate and fixed-capacity snapshot routes;
- vary declared inputs one at a time, over grids or through explicit variants (sweeps);
- calculate homogeneous P.676-13 gaseous specific attenuation and standalone P.838-3 rain and
  P.840-9 cloud cases;
- replay one ground link or one routed station pair with actual ns-3.48 UDP, finite shared
  device queues and declared endpoint acquisition timing;
- check a published Vallado TEME numerical orbit reference;
- reproduce a frozen benchmark that requires native evidence; and
- export portable results (JSON metadata, CSV, OLC, optional Parquet, archived inputs and
  SHA-256 manifests) and static Markdown reports.

These are controlled numerical experiments. Their outputs alone do not
establish operational prediction accuracy.

## Atmosphere and packet boundaries

The [standalone hydrometeor workflow](HYDROMETEORS.md) reports rain specific
attenuation in dB/km and cloud loss in dB from supplied water. Optional
[coupling](COUPLED_PROPAGATION.md) converts P.838-3 attenuation to rain loss
through a declared uniform spherical layer, adds P.840-9 cloud loss, and
subtracts both from the reference-gas budget before rates and routes are
computed. States remain fixed during a run. This geometry is an OpenLEO
sensitivity model; it is not P.618 effective-path reduction, exceedance
statistics, inferred weather, or a climatological availability calculation.

[Single-link replay](PACKET_REPLAY.md) uses one reciprocal full-duplex trace.
[Network replay](NETWORK_REPLAY.md) uses only the union of edges selected by a
routing model during the measurement window. It has native IPv4 forwarding,
shared per-device FIFO queues, and simultaneous synthetic UDP flows in both
directions. Acquisition gates selected ground interfaces at initial contact,
endpoint-satellite changes, and recovery after route loss. Route updates can
change subsequent forwarding of in-flight packets.

These service rates and queue events are simulated conditions, not hardware,
physical packet-loss calibration, independent uplink modeling, observed
traffic, or commercial goodput. Goodput counts payload received inside the
measurement window; late and censored packets remain separately identified.

## Evidence classes

| Evidence class | Examples | What it establishes |
| --- | --- | --- |
| Archived public input | CelesTrak GP records, official ITU values, Vallado states | The external record used, with source and fingerprint |
| Declared assumption | Stations, RF/noise, uniform rain/cloud, topology, UDP load, queues, acquisition | Conditions chosen for the experiment |
| Model-derived output | Geometry, attenuation, reference rate, route, simulated goodput and delay | Results of those inputs and the implementation |
| Numerical verification | Published TEME states, scalar workbook comparisons, analytic queue checks | Agreement within a named numerical domain |
| Independent observation | Not provided as calibrated RF evidence | Potential physical validation with adequate metadata and calibration |

An archived orbit does not turn RF or weather assumptions into observations.
A checksum establishes byte identity. Shared SGP4/Vallado lineage limits the
independence of a published numerical orbit comparison.

## Scientific integrity rules

- Record source, retrieval time, terms, checksum, units, and external-data assumptions.
- Name frames and time scales; public timestamps are timezone-aware UTC.
- Identify units in public field names or schema documentation.
- Distinguish geometric from apparent elevation and ellipsoid from AMSL height.
- Treat fitted elements and the idealized atmosphere as model inputs.
- Label Shannon capacity as an upper bound and DVB-S2 rates as ideal AWGN references.
- Distinguish reference rates from receive-event simulated payload goodput.
- Preserve unchanged, zero-effect, and missing-evidence cases.
- Treat deterministic sweep and grid-refinement differences as comparisons, not uncertainty
  intervals.
- Reject invalid domains and inconsistent artifacts; retain source-age warnings in provenance.
- Identify the original configuration and each derived configuration by their own hashes.

## OpenLEO 2.0 (S1) limitations

Each is a line of the `limitations` list in `results.json` (and of `openleo scenario explain`),
written exactly under the condition given here (`openleo.scenario.explain.limitations`).

- **Route breakpoints and ISLs** (events enabled and a `network`). Routes are evaluated at
  event-aware breakpoints (the grid plus every pass and MODCOD boundary of the route endpoints'
  links) and held between them. Inter-satellite range and occlusion changes between breakpoints
  are not resolved; ISL topology events belong to a later stage. The ISL rule remains v1's
  all-pairs-within-range rule.
- **Fixed-capacity threshold waiver** (a `network` whose `route_models` lists
  `fixed_capacity`). The baseline assigns `fixed_capacity_bps` to every geometrically visible
  ground link of the route endpoints regardless of the RF threshold (MODCOD lock): it can route
  over links whose adaptive rate is zero. It is a geometric baseline, not an RF-usable one.
- **Route convergence not gated** (any `network`). Route metrics are compared on the refined grid
  and reported in the `convergence` table (class `route`), but a route failure is only a warning:
  because of the first limitation, route metrics can depend on the grid step. Link and station
  metrics are gated.
- **Event-time accuracy** (events enabled). Event times are root-found to
  `time.events.tolerance_s`; culmination times are accurate to max(`tolerance_s`, about 1e-5 s),
  and a MODCOD threshold crossing within `tolerance_s` before LOS is not resolved.
- **Sampled events** (events disabled, with or without a network). v1's line: contact boundaries
  and route choices are sampled, and finer events between samples can be missed. This mode
  reproduces v1's semantics for comparison and is not intended for scientific claims.

## Outside the current model

OpenLEO does not provide orbit determination, covariance propagation, maneuver
inference, observed local weather, weather maps, P.618 rain statistics,
scintillation, atmospheric emission, frequency-dependent group delay,
calibrated antenna patterns, beam scheduling, interference, terminal hardware,
waveform/decoder simulation, a satellite MAC/PHY, TCP, retransmissions, or
measured service goodput. It does not replace live catalogs during a scientific
run or reconstruct an operator's constellation behavior.

The frozen 12/20 GHz benchmark keeps gains and EIRP identical; it does not
establish fixed-aperture performance or compare frequency-specific hardware.
Numerical refinement cannot quantify unknown physical model discrepancy.
New models need their own provenance, domains, verification, and limitations.
See [Validation](VALIDATION.md), [Sweeps](SWEEPS.md), and
[Reproducibility](REPRODUCIBILITY.md).
