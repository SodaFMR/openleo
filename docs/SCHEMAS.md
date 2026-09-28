# Public schemas and compatibility

OpenLEO v1.0 uses bounded Python validators as the authoritative input and
artifact contracts. This document indexes them; it does not introduce a
separate JSON-schema framework. A `schema_version` is a string, scoped to the
artifact's `kind` or documented workflow. Schema 1 in one result family does
not identify schema 1 in another.

## Constellation input and workbench artifacts

Constellation input JSON has exactly `name`, `orbit`, `stations`,
`time_window`, `radio_link`, `adaptation`, and `network`, plus optional
`propagation`. It has no top-level schema-version discriminator. Its source
file hash identifies original UTF-8 bytes. Orbit paths resolve relative to
the scenario file and archived bytes must match their declared SHA-256.

`load_constellation` and `parse_constellation` in
[`constellation.py`](../src/openleo/constellation.py) validate inputs.
[`input.py`](../src/openleo/input.py) provides common station, RF, UTC, units,
and finite-domain validation; [`propagation.py`](../src/openleo/propagation.py)
and [`hydrometeor_path.py`](../src/openleo/hydrometeor_path.py) validate
atmospheric declarations. Dataclasses in [`model.py`](../src/openleo/model.py)
and the model modules represent immutable declared values.

The workbench's `experiment.json` always has kind `openleo.constellation`.
Its version must match its propagation configuration:

| Version | Configuration | Link-field extension |
| --- | --- | --- |
| `1` | No propagation object; free space | Base geometry, budget, adaptation, contact fields |
| `2` | `itu_reference` gases without hydrometeors | Seven reference-gas fields |
| `3` | Reference gases with `declared_uniform_layers` hydrometeors | Schema-2 fields plus six rain/cloud fields |

Schema 2 adds `gaseous_dry_attenuation_db`,
`gaseous_water_attenuation_db`, `gaseous_attenuation_db`,
`free_space_cn0_db_hz`, `geometric_delay_s`,
`atmospheric_excess_delay_s`, and `apparent_elevation_deg`.

Schema 3 additionally carries `cloud_attenuation_db`,
`rain_specific_attenuation_db_per_km`, `rain_path_length_m`,
`rain_attenuation_db`, `hydrometeor_attenuation_db`, and
`total_atmospheric_attenuation_db`. A declaration of zero hydrometeors still
uses version 3. Legacy versions 1 and 2 retain their meanings and remain readable.

The exact top-level result fields are `schema_version`, `kind`, `scenario`,
`provenance`, `models`, `warnings`, `limitations`, `timestamps_utc`,
`satellites`, `stations`, `links`, `statistics`, and `network`.
Geometry is ITRS in metres on WGS84; public timestamps are timezone-aware UTC.
Ellipsoid heights and independently declared atmospheric AMSL heights differ.

[`workbench.py`](../src/openleo/workbench.py) defines exact JSON keys,
`LINK_FIELDS`, `ROUTE_FIELDS`, and CSV headers, and validates metadata,
configuration/version pairing, geometry, budget sums, route references, and
statistics. `load_experiment` additionally verifies bundle hashes and CSV
binding. Readers check consistency without rerunning orbit or atmospheric
models. The workbench manifest uses schema `1`, kind `openleo.bundle`,
independently of the result version.

See [Workbench](WORKBENCH.md), [Methods](METHODS.md),
[Reference propagation](REFERENCE_PROPAGATION.md), and
[Coupled propagation](COUPLED_PROPAGATION.md) for field domains and equations.

## Study and packet contracts

| Workflow | Input contract | Result kind and version | Authoritative code and details |
| --- | --- | --- | --- |
| Integrated experiment | Unversioned `ExperimentConfig` settings; unknown fields rejected | `openleo.experiment`, `1` | [experiment.py](../src/openleo/experiment.py), [Experiments](EXPERIMENTS.md) |
| Frozen benchmark | Exact configuration keys, `schema_version: "1"` | `openleo.benchmark`, `1` | [benchmark.py](../src/openleo/benchmark.py), [Benchmark](BENCHMARK.md) |
| Release reproduction | Benchmark and orbit-reference paths; trusted native backend argument | `openleo.release-evidence`, `1` | [release_evidence.py](../src/openleo/release_evidence.py), [Reproducibility](REPRODUCIBILITY.md) |
| Orbit reference | Exact schema-1 fixture with TEME frame, units, TLE, sources, tolerances, states | `openleo.orbit-verification`, `1` | [orbit_verification.py](../src/openleo/orbit_verification.py), [Orbit verification](ORBIT_VERIFICATION.md) |
| Single-link replay | Frozen `ReplayConfig`, verified workbench bundle | `openleo.packet-replay`, `1` | [packet_replay.py](../src/openleo/packet_replay.py), [Packet replay](PACKET_REPLAY.md) |
| Multi-hop replay | Frozen `NetworkReplayConfig`, verified workbench bundle | `openleo.network-replay`, `1` | [network_replay.py](../src/openleo/network_replay.py), [network_packet_io.py](../src/openleo/network_packet_io.py), [Network replay](NETWORK_REPLAY.md) |
| Gas-grid propagation study | Gas-only constellation input | `openleo.propagation-study`, `1` | [propagation_study.py](../src/openleo/propagation_study.py), [Reference propagation](REFERENCE_PROPAGATION.md) |
| Fidelity study | Exact schema-1 case paths and sampling steps | `openleo.fidelity-study`, `1` | [fidelity_study.py](../src/openleo/fidelity_study.py), [fidelity_io.py](../src/openleo/fidelity_io.py), [Fidelity studies](FIDELITY_STUDIES.md) |
| Standalone hydrometeors | Exact schema-1 rain/cloud cases | `openleo.hydrometeors`, `1` | [hydrometeor_benchmark.py](../src/openleo/hydrometeor_benchmark.py), [Hydrometeors](HYDROMETEORS.md) |

Single-pass, sensitivity, and homogeneous-gas summaries also use schema `1`;
their existing summary format does not require a `kind`. They retain their
respective [output](../src/openleo/output.py),
[sensitivity](../src/openleo/sensitivity.py), and [gases](../src/openleo/gases.py)
contracts, described in [Methods](METHODS.md), [Sensitivity](SENSITIVITY.md),
and [Gases](GASES.md). Study and packet manifests have their own schema-1
kinds; they fingerprint files rather than changing the embedded result version.

Integrated windows are positive, at most 3600 s, wholly contained in the
original scenario, and at least one sampling interval, including model-only
runs. Exact native CSV protocols, quantization, queue/traffic budgets, status
enumerations, and null/censoring semantics are in the packet documents.
JSON uses finite numbers; missing delay after no delivery is `null`, not zero.
Manifest hashes cover exact bytes. Original and derived configuration hashes
are separate identities when windows or portable paths change.

## Supported API and compatibility policy

The existing exports in [`openleo.__init__`](../src/openleo/__init__.py)
remain supported. Integrated entry points are imported from their documented
modules: `ExperimentConfig`, `parse_experiment_config`, `run_experiment`;
`NetworkReplayConfig`, `run_network_replay`; `run_benchmark`;
`verify_orbit_reference`; and `reproduce_release`. Paths accept strings or
`pathlib.Path` values where documented. Validation failures raise explicit
errors; packet execution never silently substitutes a model estimate.

Within v1.x, supported API signatures, established fields, units, semantics,
and schemas 1/2/3 remain stable. Compatible evolution is additive: optional
API parameters, additional supported capabilities, or explicitly versioned
extensions that preserve the existing contract. New propagation fields use
a declared version rather than changing legacy fields.

Current validators intentionally enforce exact keys and headers. Additive
extension therefore does not promise that an older reader accepts arbitrary
new fields. A producer must preserve the requested supported version or
declare a new version; readers reject unsupported versions and unexpected
fields explicitly. Removing fields or changing existing meanings or required
arguments requires a new schema version where applicable and a future major
software release. Private helpers beginning with `_` are validator
implementation details, not a supported extension API.
