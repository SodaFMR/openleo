# OpenLEO scientific workbench

The workbench runs a reproducible constellation experiment from an archived GP catalog
through ground-link budgets, optional reference-atmosphere propagation, adaptive
reference rates, and snapshot routing. It opens in a local browser and exports a
self-contained interactive report. The light workspace uses a compact toolbar,
numerical inspector, conventional axis plots and a route table; the 3D Earth is a
view of the same computed data, not a separate simulation.

## Start the application

From the repository root on Linux, macOS or Windows:

```bash
uv sync --locked --group dev --extra plot
uv run openleo app examples/constellations/iridium_global_reference.json
```

Open the printed `http://127.0.0.1:8765/` address if the browser does not open. Use
`--no-browser` to suppress browser launch or `--port 8766` to choose another port.
Stop the local process with Ctrl-C. Python supplies the application server; Node is
used only for development tests. The globe needs neither a map API key nor internet
access after installation.

The example uses 80 public Iridium NEXT GP records and four declared locations:
Madrid, Tromso, Singapore, and Quito. Select any station and satellite, move through
the sampled UTC timeline, inspect RF conditions and compare network routes. Drag the
globe to rotate, scroll or use the zoom buttons, and select links in the routing table.
Keyboard users can rotate the focused globe with arrow keys and use its zoom buttons.
Playback advances through archived-orbit model samples; it is not a real-time
telemetry feed. The inspector identifies the active propagation model and, when
enabled, shows gaseous loss, apparent elevation and atmospheric excess delay.

**Scenario** opens the experiment editor. Change station coordinates, RF assumptions,
sampling, adaptation, or network endpoints and recompute. The JSON editor supports
adding/removing stations within the documented bounds. Names must be unique, and the
network source and destination must name two distinct stations. Coordinates and city
presets are scenario choices; they do not assert that a calibrated station exists there.
Reference-atmosphere scenarios also require an explicit AMSL height for every station
in the JSON `propagation` object. Update those assumptions when relocating or renaming
stations; a city preset does not supply a surveyed height or a geoid conversion.

The application keeps the original catalog path and provenance pinned. To use another
catalog, start the application with another local configuration and its verified hash.
Runs never fetch replacement elements silently. Browser edits remain in memory until
exported, and a failed run preserves the preceding successful experiment.

## Reproduce and share

```bash
uv run openleo constellation examples/constellations/iridium_global_reference.json --output runs/global
```

This command writes:

| File | Contents |
| --- | --- |
| `experiment.json` | Complete parsed inputs, source provenance, model definitions, UTC timestamps, ECEF trajectories, link states, network snapshots, summary metrics |
| `links.csv` | One row per above-mask satellite–station–sample, including RF outages with zero rate |
| `routes.csv` | Every sample and routing model, including disconnected routes |
| `explorer.html` | Interactive report with all data, cartography and browser code embedded |
| `manifest.json` | SHA-256 of each exported file and package version |

Open `explorer.html` directly in a browser. Offline reports provide the same selection,
timeline and inspection controls; recomputation requires the Python application.
Report exports made after a browser rerun embed the current successful experiment and
remove the local session token. Source configuration and result JSON retain binary64
round-trip precision. CLI CSV uses 15 significant digits, explicit units and LF endings.
Browser CSV downloads include readable station/satellite names, retain JavaScript's
numeric round-trip representation, and use CRLF endings. Use CLI exports for the fixed
machine-readable column schema above.

Free-space configurations omit `propagation` and retain experiment schema `1` and
the original link-column set. Reference-atmosphere configurations use schema `2`,
adding dry, wet and total gaseous loss, free-space `C/N0`, geometric delay,
atmospheric excess delay and apparent elevation. Exported model metadata records the
Recommendation versions, source hashes and approximation boundaries. Both schemas
remain readable; the original `iridium_global.json` is still a free-space example.

`openleo.workbench.load_experiment(directory)` verifies all four artifact hashes before
returning the experiment. Checksums detect a changed artifact relative to its manifest;
they do not authenticate whoever created the manifest.

File-launched experiments fingerprint the original scenario file bytes. A browser
rerun normalizes its parsed scenario and records the exact canonical JSON string in
`provenance.scenario_canonical_json` alongside `scenario_sha256` and its encoding.
Hash that string's UTF-8 bytes to verify a browser export: JavaScript may otherwise
rewrite numeric spellings such as `15.0` to `15` without changing their value.

## Geometry and time

Skyfield loads GP fields through its SGP4 implementation and propagates at the explicit
UTC sample times. Trajectories use ITRS Earth-fixed coordinates in metres. Ground
positions use WGS84 geodetic latitude, longitude and ellipsoidal height. The browser
projects those coordinates; camera rotation has no effect on any computed metric.

The end timestamp is always sampled, including when the last interval is shorter than
the requested step. The report plays discrete samples, without interpolating RF or
route state. Track segments are visual connections between those sampled positions.
The built-in Skyfield time table is fingerprinted and each satellite carries its fitted
epoch and maximum requested age. Age warnings are quality indicators, not orbit-error
bars. Propagation failures raise explicit errors.

Geometric visibility requires elevation at or above the configured mask. RF usability
additionally requires a supported mode. Remaining contact is bounded by sampled
mask crossings or the end of the experiment; `contact_truncated` records the latter.
Neither sampled contact duration nor orbit prediction establishes measured reception.

## RF and adaptation

Every ground link reuses the free-space, thermal-noise and first-order Doppler
equations described in [Methods](METHODS.md). A common declared terminal
model applies to the example links. It is independent of Iridium's actual radio system.

For symbol rate $R_s$ and root-raised-cosine roll-off $\alpha$, the scenario must satisfy

$$
B\geq R_s(1+\alpha),\qquad E_s/N_0=C/N_0-10\log_{10}(R_s).
$$

The configured bandwidth $B$ also supplies the ideal equivalent noise bandwidth used
for the SNR and Shannon–Hartley upper bound, as in the original pass model.

The five mode efficiencies and thresholds below come from Table 13 of
[ETSI EN 302 307-1 V1.4.1](https://www.etsi.org/deliver/etsi_en/302300_302399/30230701/01.04.01_60/en_30230701v010401p.pdf).
These are ideal AWGN reference results for normal 64,800-bit FEC frames without pilots,
with the receiver assumptions stated in that table.

| Mode | Information bits per symbol | Required ideal $E_s/N_0$ (dB) |
| --- | ---: | ---: |
| QPSK 1/4 | 0.490243 | -2.35 |
| QPSK 1/2 | 0.988858 | 1.00 |
| QPSK 3/4 | 1.487473 | 4.03 |
| 8PSK 2/3 | 1.980636 | 6.62 |
| 8PSK 3/4 | 2.228124 | 7.91 |

A mode requires its threshold plus the declared implementation margin. Upgrading an
existing lock additionally requires the hysteresis margin; falling below the current
requirement immediately selects a supported lower mode or declares an RF outage.
The reference rate is $R_s\eta$; actual decoding, pilot overhead, acquisition delays,
modem dynamics and transport overhead are not simulated. The Doppler trace is reported,
but an ideal tracking assumption prevents it from independently degrading the rate.

## Reference-atmosphere propagation

The optional model combines the P.835-7 global temperature, total-pressure and
water-vapour profile, P.453-14 refractivity, and P.676-13 Annex 1 layer integration.
Specific attenuation in dB/km is integrated over each refracted layer length to obtain
loss in dB. The dry pressure supplied to the gas calculation is total pressure minus
water-vapour partial pressure.

The endpoint solve uses geometric elevation and range to find an apparent launch
elevation through a 6,371 km mean-radius spherical atmosphere ending at 100 km.
This sphere approximates the WGS84 topocentric geometry. Atmospheric heights are
explicit geometric heights above mean sea level, not automatic conversions of the
stations' ellipsoidal heights. The example's four heights are assumptions, not survey
data, even where their numerical values equal the ellipsoidal-height assumptions.

Gaseous loss reduces `cn0_db_hz`, SNR, `esn0_db` and the selected adaptive reference
rate. `delay_s` includes both geometric range divided by light speed and the excess
optical path divided by light speed. The inspector reports the excess separately.
The elevation mask, contact samples and nominal Doppler remain geometric. Receiver
system noise temperature remains fixed; atmospheric emission is not added. The delay
uses non-dispersive radio refractivity, not a frequency-dependent group-delay model.

The coupled model accepts frequencies from 1 to 1,000 GHz, station AMSL heights from
0 to 10,000 m, a geometric elevation mask of at least 5 degrees, and ray endpoints
above 100 km. `refinement` is 1, 2 or 4, splitting each reference layer evenly.
These are calculation bounds, not an assertion of measured accuracy throughout the
domain. Rain, cloud, scintillation and local weather are absent.

To isolate this model's effect and check its grid resolution:

```bash
uv run openleo propagation-study examples/constellations/iridium_global_reference.json \
  --output runs/propagation-study --figure runs/propagation-study/comparison.svg
```

The output contains `free_space/`, `reference/` and `refined/` experiment bundles,
`comparison.csv`, and `propagation-study.json` with provenance, case summaries and
maximum loss, apparent-elevation and excess-delay differences. The refined case
doubles a starting refinement of 1 or 2; a study starting at 4 is rejected. Geometry,
RF inputs and network settings are held fixed. The optional figure requires the
`plot` extra. Refinement differences are numerical evidence, not uncertainty bars or
calibrated weather validation. See [Reference propagation](REFERENCE_PROPAGATION.md)
for exact equations, official cases and configuration fields.

## Network experiment

Inter-satellite edges are hypothetical reciprocal links within `isl_max_range_m` and
with unobstructed WGS84 ellipsoid line of sight. Their capacity is the declared
`isl_capacity_bps`; pointing, topology scheduling and actual operator terminals are
outside this experiment. Only the selected endpoint ground stations participate in
routes; other stations are not implicit relays.

Three models use the same positions, geometric mask and ISL assumptions:

1. **Minimum delay:** Dijkstra routing through RF-usable adaptive ground links.
2. **Maximum rate:** maximize path bottleneck, then minimize delay among equally wide
   routes, using those same adaptive links.
3. **Fixed capacity:** minimum-delay routing with all geometric ground links assigned
   `fixed_capacity_bps`, including links where the adaptive model reports RF outage.

Path delay sums the selected ground-link delays and vacuum ISL delays; reference
atmosphere adds ground-link optical-path excess in all three routing models.
The fixed-capacity baseline changes ground-link rates, not their delays. Bottleneck
rate is the minimum edge rate. A reciprocal ground-link budget is an explicit modeling assumption, not a
separate uplink calculation. These graph snapshots contain no traffic demand, packet
queues, congestion, terminal contention, retransmissions or TCP/UDP goodput.

Integrated bits use left-held values over each successive sample interval, with zero
for an unavailable link or route. No duration is assigned beyond the final sample.
Connected/visible/usable fractions count samples and are not statistical availability
probabilities. Route changes and best-link handovers count switches between adjacent
connected samples; loss and reacquisition do not count as a switch.

The fixed baseline is a declared experiment choice. Its comparison can show the effect
of idealizing ground links for that configuration; it does not prove a universal
performance bias or superiority of a routing algorithm.

## Bounds and verification

A run accepts at most 128 satellites, 16 stations, 1,441 samples and 500,000
satellite–station–sample combinations. The archived catalog and configuration have
bounded readers; identifiers, timestamps, physical domains and source hashes are
checked before propagation. The application binds only to loopback, validates the Host
and Origin, and requires a random session token for recomputation. Browser requests
cannot change the catalog file path, fetch remote inputs or write arbitrary files.

Tests include official MODCOD thresholds, independent known orbit and local-geometry
checks, hysteresis/outages, ellipsoid occlusion, hand-derived shortest/widest graphs,
unchanged ISLs in the fixed baseline, input validation, HTTP boundary failures, export
hashes and real browser interaction. Atmospheric tests add official profile/path
reference cases, analytic homogeneous-shell and vacuum checks, endpoint recovery,
grid refinement and schema-1 compatibility. Browser tests exercise offline reports,
simulation edits, downloads, timeline synchronization and narrow layouts. Source provenance is in
[Third-party data](../THIRD_PARTY_DATA.md).
