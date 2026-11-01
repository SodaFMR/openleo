# OpenLEO

> **OpenLEO 2.0 is in development; v1.0.0 remains available at tag
> [`v1.0.0`](https://github.com/SodaFMR/openleo/tree/v1.0.0).** This branch carries the 2.0
> engine (scenario v2, exact events, results v2, sweeps). The local browser workbench of v1 is
> retired here; its successor arrives later in the 2.0 programme. See
> [Migration from 1.x](docs/MIGRATION.md).

OpenLEO is an open Python tool for reproducible studies of time-varying
low-Earth-orbit satellite-to-ground links and snapshot routes. It combines archived GP
orbital data, declared station, terminal and RF inputs, an idealized reference atmosphere with
optional declared rain and cloud, DVB-S2 reference rates, snapshot routing, and optional native
ns-3 packet replays.

The 2.0 engine evaluates one scenario of 1 to N satellites and 1 to M stations with exact event
times: acquisition and loss of signal (AOS/LOS) are root-found mask crossings, culminations are
refined maxima, and MODCOD switches are found in continuous time. Every duration, fraction and
integral uses one time-measure convention, and a grid-convergence gate checks every exported
integrated metric. The models do not reconstruct an operational network or infer local weather.

## Workflows

| Goal | Command |
| --- | --- |
| Write a commented starter scenario | `openleo scenario new [--preset single-pass\|constellation] [-o FILE]` |
| Check a scenario (JSON-pointer errors) | `openleo scenario validate SCENARIO.json` |
| List every assumption, its evidence class and the limitations | `openleo scenario explain SCENARIO.json` |
| Convert an OpenLEO 1.x scenario | `openleo scenario upgrade V1.json [-o OUT.json]` |
| Simulate a scenario and save its results | `openleo run SCENARIO.json --output DIR [--formats csv,olc,parquet] [--workers N] [--memory-budget GiB]` |
| Vary a scenario (one at a time, grid or variants) | `openleo sweep SWEEP.json --output DIR [--workers N]` |
| Replay one ground link through ns-3 | `openleo replay link RESULTS --station NAME --satellite NORAD --backend EXE --output DIR` |
| Replay one routed station pair through ns-3 | `openleo replay network RESULTS --pair SRC:DST --model MODEL --backend EXE --output DIR` |
| Reproduce the frozen release evidence | `openleo reproduce CONFIG.json --reference ORBIT_REF.json --network-backend EXE --output DIR` |
| Check published Vallado TEME states | `openleo verify orbit REFERENCE.json` |
| Homogeneous P.676-13 gaseous attenuation | `openleo calc gases CONFIG.json --output DIR` |
| Standalone P.838-3 rain and P.840-9 cloud cases | `openleo calc hydrometeors CONFIG.json --output DIR` |
| Markdown and CSV report (optional figures) | `openleo report RESULTS_OR_SWEEP --output DIR [--figures]` |
| Re-export a results directory | `openleo export RESULTS --to csv\|parquet --output DIR` |
| Serve the local run and sweep API | `openleo app SCENARIO.json [--port N] [--no-browser] [--packet-backend EXE] [--network-backend EXE] [--artifact-limit MiB]` |

The retired 1.x commands (`constellation`, `experiment`, `fidelity-study`, `propagation-study`,
`sensitivity`, `benchmark`, `gases`, `hydrometeors`, `verify-orbit`, `plot*`, `packet-replay`,
`network-replay`) print one line naming their replacement and exit with status 2. User-input
errors print `error: <json-pointer>: <message>` and exit with status 1, without a traceback.

The [scope](docs/SCOPE.md) and [validation evidence](docs/VALIDATION.md) define what these
workflows can establish.

## Install from the repository

OpenLEO requires Python 3.12 or newer. Install it with [`uv`](https://docs.astral.sh/uv/):

```bash
git clone https://github.com/SodaFMR/openleo.git
cd openleo
uv sync --locked --no-dev
```

Optional extras: `plot` (matplotlib, for `report --figures`) and `parquet` (pyarrow, for
Parquet tables):

```bash
uv sync --locked --no-dev --extra plot --extra parquet
```

The Python core runs on Linux, macOS and Windows. The optional packet replays need separately
built ns-3.48 backends on Linux or macOS; on Windows, run the backends and their Python caller
inside WSL2. See the [backend setup](adapters/ns3/README.md). Contributor dependencies are
separate; see [Contributing](CONTRIBUTING.md).

## Quick start

Every command below runs from the repository root.

```bash
uv run --no-dev openleo scenario explain examples/constellations/iridium_global_hydrometeors.json
uv run --no-dev openleo run examples/constellations/iridium_global_hydrometeors.json --output runs/coupled
uv run --no-dev --extra plot openleo report runs/coupled --output runs/coupled-report --figures
```

`runs/coupled` holds `results.json` (metadata, models, provenance, warnings, limitations,
convergence and headline `summary`), `tables/*.csv` and `tables/*.olc` (samples, events,
passes, rate segments, pair, station and route metrics, routes, positions, convergence), the
archived `inputs/` and a `manifest.json` of SHA-256 hashes. See [Schemas](docs/SCHEMAS.md).

The example uses 80 archived public CelesTrak GP records and four approximate station
locations. RF parameters, terminal settings, inter-satellite links, rain rate and cloud water
are declared research inputs, not descriptions of Iridium hardware, service or observed
weather; the scenario marks this with `space_segment.synthetic_rf: true`.

Shipped examples:

| Path | Content |
| --- | --- |
| `examples/scenarios/iss_cartagena.json` | One ISS pass over Cartagena, free space, 10 s grid |
| `examples/constellations/iridium_global.json` | 80 Iridium NEXT satellites, 4 stations, 2 h, free space, Madrid-Tromso routes |
| `examples/constellations/iridium_global_reference.json` | the same with the ITU-R reference gaseous atmosphere |
| `examples/constellations/iridium_global_hydrometeors.json` | the same with declared uniform rain and cloud |
| `examples/sweeps/*.json` | an EIRP one-at-a-time sweep, the aligned propagation cases, the sampling-fidelity grid, the release matrix |
| `examples/reproduce/release_benchmark.json` | the frozen release benchmark (`openleo reproduce`) |
| `examples/performance/starlink_1000_24h.json` | the 1000-satellite 24-hour performance scenario |

## Sweeps and reproduction

```bash
uv run --no-dev openleo sweep examples/sweeps/propagation_cases.json --output runs/cases
uv run --no-dev openleo report runs/cases --output runs/cases-report
```

A sweep varies a base scenario one parameter at a time (`oat`), over a Cartesian grid (`grid`)
or through an explicit list of JSON patches (`variants`), and writes every run plus a
deterministic comparison table (value, baseline, delta and ratio per metric). Optional `replay`
blocks replay every run through the trusted ns-3 backends. See [Sweeps](docs/SWEEPS.md).

`openleo reproduce` runs the frozen release benchmark: a sweep with native network replays,
gated on a passing published orbit reference and complete native evidence. See
[Reproducibility](docs/REPRODUCIBILITY.md) and [Benchmark](docs/BENCHMARK.md).

## Python API

```python
from pathlib import Path

from openleo import load_results, load_scenario, run, run_sweep, save_run

scenario = load_scenario(Path("examples/constellations/iridium_global_reference.json"))
results = run(scenario, workers=4)
print(results.metadata["summary"]["stations"]["Tromso"]["rf_usable_fraction"])
save_run(scenario, results, Path("runs/reference"))

again = load_results(Path("runs/reference"))  # verifies the manifest and every table hash
summary = run_sweep(Path("examples/sweeps/eirp_oat.json"), Path("runs/eirp"))
```

`run` never mutates its inputs and raises `ValueError` for invalid inputs. Tables are numpy
columns with units in their names (`_s`, `_m`, `_deg`, `_db`, `_bps`, `_bits`, ...) and in
`results.json`.

## Models and evidence

- [Methods](docs/METHODS.md): frames, geometry, link budget, events, the MODCOD state machine,
  integration, routing and the convergence gate.
- [Schemas](docs/SCHEMAS.md): scenario v2, results v2, OLC v1 and sweep v2.
- [Migration from 1.x](docs/MIGRATION.md): commands, fields, conventions and re-derived numbers.
- [Validation](docs/VALIDATION.md): the checks performed and the physical validation still needed.
- [Scope](docs/SCOPE.md): the scientific claim boundary.
- [Reference propagation](docs/REFERENCE_PROPAGATION.md) and
  [coupled hydrometeors](docs/COUPLED_PROPAGATION.md): the atmosphere models.
- [Gases](docs/GASES.md) and [Hydrometeors](docs/HYDROMETEORS.md): the standalone calculators.
- [Packet replay](docs/PACKET_REPLAY.md) and [Network replay](docs/NETWORK_REPLAY.md): the ns-3
  bridges.
- [Orbit verification](docs/ORBIT_VERIFICATION.md), [Sweeps](docs/SWEEPS.md),
  [Reproducibility](docs/REPRODUCIBILITY.md), [Benchmark](docs/BENCHMARK.md) and
  [App API](docs/WORKBENCH.md).

## Scientific limits

Declared rain and cloud conditions are constant sensitivity inputs. They do not infer local
weather or provide P.618 exceedance probabilities or availability. The reference atmosphere is
idealized; receiver noise stays fixed and atmospheric emission is omitted. Visibility masks and
Doppler use geometric (not refracted) elevation and range rate.

OpenLEO does not model antenna patterns, interference, beam scheduling, waveforms, decoding,
satellite MAC/PHY, TCP or retransmissions. Endpoint acquisition in network replays is a
declared timing interval rather than a modem simulation. Snapshot routing has no traffic
contention; the optional native replays add contention only within their bounded synthetic UDP
topology.

Shannon-Hartley capacity is an upper bound and DVB-S2 rates are ideal AWGN references; neither
is measured throughput. Native packet goodput is simulated payload delivery. Fitted orbital
elements are not exact trajectories; numerical Vallado agreement is not observation-based orbit
or RF validation. Grid refinement and sweep differences are deterministic comparisons, not
probabilistic uncertainty.

In the 2.0 engine, routes are re-evaluated at event-aware breakpoints and held between them:
inter-satellite range and occlusion changes between breakpoints are not resolved, so route
metrics are reported in the convergence table but not gated. The `fixed_capacity` baseline
assigns its capacity to every geometrically visible ground link of the route endpoints, with or
without MODCOD lock. `results.json` states the route-breakpoint and route-convergence lines only
when the scenario has a `network` (with events enabled), the fixed-capacity line only when
`network.route_models` lists `fixed_capacity`, and the event-time accuracy line whenever events
are enabled; see [Scope](docs/SCOPE.md#openleo-20-s1-limitations).

## Citation, license and data terms

Cite the software using [`CITATION.cff`](CITATION.cff). No DOI, PyPI publication or journal
acceptance is claimed. The Python core is [MIT licensed](LICENSE). The separately built C++
adapters and their shared header are [GPL-2.0-only](adapters/ns3/LICENSE) and excluded from the
core wheel and source distribution.

Archived input provenance and terms are in [`THIRD_PARTY_DATA.md`](THIRD_PARTY_DATA.md).
Implementation notices are in [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).
Contributions are welcome; start with [`CONTRIBUTING.md`](CONTRIBUTING.md).
