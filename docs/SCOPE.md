# Scope and scientific claim boundary

OpenLEO produces inspectable, reproducible model outputs for time-varying
satellite-to-ground link and snapshot-routing studies. It connects archived public
orbital elements to explicit geometry, RF, propagation, adaptation, and network
assumptions without presenting those assumptions as descriptions of an operational
system.

## Supported research uses

OpenLEO can:

- generate a deterministic free-space trace for one visible pass;
- compare declared inputs with a deterministic one-at-a-time study;
- calculate homogeneous P.676-13 gaseous specific attenuation;
- simulate bounded constellations and station sets from archived GP records;
- apply ideal AWGN reference MODCOD thresholds and report RF outages;
- compare minimum-delay, maximum-bottleneck, and fixed-ground-capacity snapshot routes;
- integrate an idealized P.835-7/P.453-14/P.676-13 reference atmosphere;
- compare free-space, reference-grid, and refined-grid propagation variants;
- compare multiple declared scenarios and sampling steps in a fidelity study; and
- export portable CSV, JSON, manifest, and self-contained HTML artifacts.

These capabilities support controlled numerical experiments. They do not by themselves
establish that one model is accurate enough for an operational decision.

## Evidence classes

Keep these categories separate when interpreting a result:

| Evidence class | OpenLEO examples | What it establishes |
| --- | --- | --- |
| Archived public input | CelesTrak GP records, official ITU workbook values | The exact external record used, with source and fingerprint |
| Declared assumption | Station coordinates, terminal gains, EIRP, sampling, network edges | The conditions selected for the experiment |
| Model-derived output | Geometry, loss, rate, route, integrated bits, refinement difference | The result of the declared implementation and inputs |
| Independent observation | Not included in the committed examples | A possible basis for physical validation when calibration and context are sufficient |

A real archived orbit record does not make synthetic RF or network assumptions
observations. A checksum proves byte identity, not scientific correctness.

## Scientific integrity rules

- Record source, retrieval time, applicable terms, checksum, units, and assumptions
  for external scientific data.
- Name time scales and coordinate frames; public timestamps are timezone-aware UTC.
- Put units in public field names or schema documentation.
- Distinguish geometric from apparent elevation and WGS84 ellipsoid height from AMSL.
- Treat fitted orbital elements as model inputs, not exact trajectories.
- Treat the global reference atmosphere as an idealized profile, not local weather.
- Label Shannon-Hartley capacity as a theoretical upper bound.
- Label DVB-S2 values as ideal AWGN reference rates, not achieved throughput.
- Treat deterministic ranges and refinement differences as numerical comparisons, not
  probability distributions, uncertainty intervals, or measurement errors.
- Fail explicitly on invalid inputs, stale-source warnings, propagation failures, and
  inconsistent artifacts.

## Outside the current model

OpenLEO does not currently provide:

- orbit determination, covariance propagation, maneuver inference, or operational
  prediction;
- local weather, rain, cloud, fog, scintillation, or atmospheric emission;
- antenna patterns, beam steering, polarization, interference, or terminal scheduling;
- waveform, coding, BER, FER, acquisition, or modem simulation;
- traffic demand, packet queues, congestion, retransmissions, TCP/UDP, or goodput;
- live catalog replacement during a scientific run;
- calibrated received-power validation; or
- reconstruction or prediction of real operator or commercial-service performance.

New models need their own provenance, supported domain, verification, limitations, and
claim boundary. The current [validation evidence](VALIDATION.md) documents what has and
has not been checked.
