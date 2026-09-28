# Scope and scientific claim boundary

OpenLEO produces inspectable model outputs for time-varying satellite-to-ground
links, snapshot routes, and bounded synthetic packet experiments. Archived
public orbital elements and explicit geometry, RF, atmosphere, and network
assumptions support repeatable comparisons; they do not describe an
operational system.

## Supported research uses

OpenLEO can:

- generate a deterministic free-space trace for a visible pass;
- compare declared inputs in one-at-a-time sensitivity studies;
- calculate homogeneous P.676-13 gaseous specific attenuation;
- calculate standalone P.838-3 rain and P.840-9 cloud cases;
- simulate bounded constellations and station sets from archived GP records;
- integrate an idealized P.835-7/P.453-14/P.676-13 atmosphere;
- optionally subtract declared uniform rain and cloud loss before adaptation;
- select ideal AWGN reference MODCODs and report RF outages;
- compare minimum-delay, maximum-rate, and fixed-capacity snapshot routes;
- compare gas grids and sampling steps as numerical sensitivity studies;
- run aligned propagation experiments with optional native packet replay;
- replay one ground link or selected multi-hop routes with actual ns-3.48 UDP,
  finite shared device queues, and declared endpoint acquisition timing;
- check a published Vallado TEME numerical orbit reference;
- run a frozen benchmark and require native evidence for complete reproduction; and
- export portable JSON, CSV, manifests, inputs, and offline HTML reports.

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
- Treat deterministic and refinement differences as comparisons, not uncertainty intervals.
- Reject invalid domains and inconsistent artifacts; retain source-age warnings in provenance.
- Identify the original configuration and each derived configuration by their own hashes.

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
See [Validation](VALIDATION.md), [Experiments](EXPERIMENTS.md), and
[Reproducibility](REPRODUCIBILITY.md).
