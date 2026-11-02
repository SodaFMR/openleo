# Migration from OpenLEO 1.x

OpenLEO 2.0 keeps every physical model of v1.0.0 (geometry, free-space budget, noise, C/N0,
Es/N0, the five DVB-S2 rows and their selection rule, the P.676/P.835/P.453 ray trace, the
P.838/P.840 declared layers, occlusion and the route algorithms). What changes is the scenario
format, the command line, the result format, and how time is sampled and integrated: event
times are root-found and integrals are taken over exact segments. This document maps commands
and fields, lists every convention change, and re-derives the headline numbers of the shipped
examples. v1.0.0 remains available at its tag.

## Commands

| v1 command | 2.0 replacement |
| --- | --- |
| `openleo run SCENARIO` (single pass) | `openleo run SCENARIO --output DIR` (any number of satellites and stations) |
| `openleo constellation SCENARIO` | `openleo run SCENARIO --output DIR` |
| `openleo experiment SCENARIO` | `openleo sweep SWEEP.json` with `variants`, plus a `replay` block |
| `openleo fidelity-study`, `propagation-study`, `sensitivity` | `openleo sweep SWEEP.json` (`grid`, `variants`, `oat`) |
| `openleo benchmark CONFIG` | `openleo reproduce CONFIG` (with native evidence) or `openleo sweep` |
| `openleo reproduce CONFIG` | `openleo reproduce CONFIG` (the config is now a sweep spec) |
| `openleo gases CONFIG` | `openleo calc gases CONFIG` |
| `openleo hydrometeors CONFIG` | `openleo calc hydrometeors CONFIG` |
| `openleo verify-orbit REFERENCE` | `openleo verify orbit REFERENCE` |
| `openleo plot`, `plot-sensitivity` | `openleo report RESULTS_OR_SWEEP --figures` |
| `openleo plot-gases RUN` | `openleo calc gases CONFIG --output DIR`, then `openleo.calc.gases_figure.render_gases_overview(DIR, SVG)` in Python (plot extra) |
| `openleo packet-replay BUNDLE` | `openleo replay link RESULTS --station NAME --satellite NORAD` |
| `openleo network-replay BUNDLE` | `openleo replay network RESULTS --pair SRC:DST --model MODEL` |
| `openleo app SCENARIO` (workbench UI) | `openleo app SCENARIO` (API only in S1; see [App API](WORKBENCH.md)) |
| (none) | `openleo scenario new\|validate\|explain\|upgrade`, `openleo report`, `openleo export` |

A retired command prints one line naming its replacement and exits with status 2. The shipped
v1 study configurations (`examples/studies`, `examples/sensitivity`) are replaced by
`examples/sweeps/*.json` and `examples/reproduce/release_benchmark.json`; frozen copies of the
v1 files are kept under `tests/fixtures/v1/examples/` for the equivalence tests.

## Scenario fields

`openleo scenario upgrade V1.json [-o OUT.json]` converts both v1 scenario kinds
deterministically; the output validates and runs. The mapping:

| v1 field | 2.0 field |
| --- | --- |
| (none) | `schema: "openleo.scenario/2"` |
| `name` | `name`; `description` records the upgrade and every default it introduced |
| `orbit.{path, source_url, retrieved_at_utc, terms_url, sha256}` | `space_segment.catalogs[0].{path, source_url, retrieved_at_utc, terms_url, sha256}`, `format: "celestrak_gp_csv"`; the path is made relative to the output file |
| (none) | `space_segment.select_norad_ids: null`; `space_segment.synthetic_rf` (true when a catalog object name contains IRIDIUM, STARLINK, ONEWEB or KUIPER) |
| `ground_station` (single pass) | `ground_segment.stations[0]` |
| `stations[].{name, latitude_deg, longitude_deg}` | `ground_segment.stations[].{name, latitude_deg, longitude_deg}` |
| `stations[].height_m` | `ground_segment.stations[].height_ellipsoid_m` (WGS84) |
| `propagation.station_heights_amsl_m.<name>` | `ground_segment.stations[].height_amsl_m` (null without propagation) |
| `time_window.{start_utc, stop_utc, step_s}` | `time.{start_utc, stop_utc, step_s}` |
| `time_window.minimum_elevation_deg` | `ground_segment.default_minimum_elevation_deg` (each station may override it) |
| (none) | `time.events: {enabled: true, tolerance_s: 0.001}`; `time.convergence: {mode: "warn", refinement_factor: 4, relative_tolerance: 0.01, absolute_tolerance_bits: 1}` |
| `radio_link.{carrier_frequency_hz, channel_bandwidth_hz, eirp_dbw}` | `radio.{carrier_frequency_hz, channel_bandwidth_hz, eirp_dbw}` |
| `radio_link.receiver_gain_dbi` | `terminals.default.receive_gain_dbi` |
| `radio_link.{system_noise_temperature_k, miscellaneous_loss_db}` | `terminals.default.{system_noise_temperature_k, miscellaneous_loss_db}` |
| `adaptation.{symbol_rate_baud, rolloff, implementation_margin_db, hysteresis_db}` | the same names under `adaptation`, plus `standard: "dvb-s2"`, `table: "v1-reference-subset"`, `feedback_delay_s: 0` |
| (single pass: no adaptation) | an upgrade default: `symbol_rate_baud = channel_bandwidth_hz / 1.2`, roll-off 0.2, margin and hysteresis 0 dB (recorded in `description`) |
| `propagation.{model, refinement, hydrometeors}` | `propagation.{model, refinement, hydrometeors}`; no `propagation` is `model: "free_space"` |
| `network.{source_station, target_station}` | `network.pairs: [{source, target}]` (up to 16 pairs) |
| `network.{isl_max_range_m, isl_capacity_bps}` | `network.isl: {rule: "all_pairs_within_range", max_range_m, capacity_bps}` |
| `network.fixed_capacity_bps` | `network.fixed_capacity_bps`; `network.route_models` lists the three v1 models |
| (none) | `outputs: {formats: ["csv", "olc"], satellite_positions: "grid"}` |

Convergence is `warn`, not `error`, in upgraded files so that they run and report their grid
sensitivity instead of failing; new scenarios default to `error`.

## Result fields

v1's workbench bundle (`experiment.json`, `links.csv`, `routes.csv`) becomes a results
directory (see [Schemas](SCHEMAS.md)). The main renames:

| v1 | 2.0 |
| --- | --- |
| `links` (per sample) | `samples` table; `delay_s`, `shannon_upper_bound_bps` and the propagation fields keep their names |
| station `visible_sample_fraction`, `usable_sample_fraction` | `station_metrics.visible_fraction`, `rf_usable_fraction` (time measure) |
| station `handover_count` | `station_metrics.best_link_handovers` |
| station `fixed_baseline_integrated_bits` | `station_metrics.fixed_capacity_integrated_bits` |
| network `connected_sample_fraction` | `route_metrics.connected_fraction` (time measure) |
| single-pass `sampled_aos_utc`, `sampled_los_utc`, `sampled_duration_s` | `passes.aos_offset_s`, `los_offset_s` (root-found; `aos_utc`/`los_utc` in CSV), `pair_metrics.visible_duration_s` |
| single-pass `integrated_capacity_upper_bound_bits` | `pair_metrics.integrated_shannon_upper_bound_bits` |
| single-pass `propagation_delay_s`, `capacity_upper_bound_bps` | `samples.delay_s`, `samples.shannon_upper_bound_bps` |
| (none) | `events`, `passes`, `rate_segments`, `pair_metrics`, `convergence` tables |

## Convention changes

1. **Time-measure fractions.** v1 fractions counted samples: visible samples divided by all
   samples, including the final sample, which holds no time. 2.0 fractions are measures divided
   by the window length T1 − T0. With events off (left-hold), a sample-count fraction k/n
   becomes a measure k·Δ/T; for the 2-hour examples at 60 s that is 119/121 = 0.983471 versus
   7080/7200 = 0.983333.
2. **Event integration.** v1 held every state from one sample to the next (constellations) and
   sampled AOS/LOS on the grid. 2.0 root-finds AOS and LOS (within `tolerance_s`), refines
   culminations, finds MODCOD switches where Es/N0 − margin crosses a threshold, and integrates
   piecewise-constant rates exactly over those segments. Durations, rate integrals, handovers and
   route metrics therefore change wherever a boundary fell between samples. With events off,
   2.0 reproduces v1's left-hold integrals exactly (the "v2 events off" column below).
3. **No trapezoid.** v1's single-pass Shannon integral was trapezoidal between visible samples;
   2.0 uses adaptive Gauss-Legendre over each pass (events on) or left-hold (events off). No
   trapezoid rule remains.
4. **Route changes: 47 versus 48.** v1 counted node-sequence changes between consecutive
   connected samples, including a change at the final sample, which holds no time. 2.0 counts
   changes between consecutive connected route intervals that hold time. On the Iridium examples
   with events off, v1's 48 `fixed_capacity` route changes become 47: the 48th was a change at
   the zero-duration final sample. With events on, routes are re-evaluated at every event
   breakpoint of the endpoint stations, which finds additional changes (53).
5. **Events-off final-sample lock.** A MODCOD decision whose effective time is at or after the
   end of its pass is dropped. With events off, a lock decided exactly at the window stop is
   therefore not reported at that final sample, where v1 reported it. The shipped examples have
   no such decision.
6. **Newton endpoint solve.** The ray tracer's apparent-elevation solve uses a safeguarded Newton
   iteration (steps ≤ 1e-11 deg) instead of v1's 40-step bisection (about 4e-11 deg). Per-sample
   values shift by about 1e-11 in the solved elevation and pass every equivalence tolerance at
   the shipped frequencies (12, 20 and 30 GHz tested to 1e-10 dB, 1e-9 deg, 1e-13 s). Above about
   50 GHz, where the gaseous loss is large and steep near 5 deg elevation, the gaseous dB of a
   sample can differ from v1's by more than 1e-10 dB; there v1's bisection is the less precise
   solve.
7. **Symlinked scenarios.** A scenario reached through a symbolic link resolves relative catalog
   paths from the directory of the file the link points to; v1 used the link's own directory.
8. **Results v2.** CSV floats are full precision (`repr`), where v1 rounded to 12 significant
   digits; NaN is an empty cell; time columns are offsets in seconds plus derived `*_utc` text
   columns.
9. **Limits.** v1's caps (128 satellites, 1 MB catalogs, 3600 s study windows) are lifted to
   20,000 objects, 10 MB per catalog, 31-day windows, 1,000,000 grid samples and a memory budget.

## Re-derived headline metrics

The tables below compare the headline metrics of each shipped example. "v1.0.0" is v1's own
output (the frozen fixtures in `tests/fixtures/v1`), "v2 events off" the 2.0 engine with
`time.events.enabled = false`, and "v2 events on" the example as shipped. They are generated by
`scripts/migration_table.py` from the fixtures and fresh 2.0 runs;
`uv run python scripts/migration_table.py --check` fails when this document is stale.

Reason codes:

- `=`: unchanged.
- `M`: time-measure fraction instead of a sample-count fraction (convention 1).
- `E`: exact events: AOS/LOS root-found and MODCOD switches found between samples, integrated
  over exact segments (convention 2).
- `B`: routes re-evaluated at event-aware route breakpoints (convention 2), which also moves the
  connected time and the bottleneck integral.
- `F`: v1 counted a route change at the zero-duration final sample (convention 4).
- `H`: v1's single-pass duration was LOS sample − AOS sample; 2.0 events off holds the last
  visible sample to the next grid sample (one more step).
- `T`: v1's single-pass Shannon integral was trapezoidal; 2.0 events off is left-held
  (convention 3).
- `Q`: adaptive Gauss-Legendre over the exact pass instead of a grid rule (convention 3).
- `C`: maximum elevation and minimum range found at the refined culmination and range-rate root
  instead of the best grid sample.

Every v2 events-off integral equals v1's; every remaining difference comes from a convention
change (`M`, `F`, `H`, `T`) or from exact events (`E`, `B`, `Q`, `C`). The large relative
changes (Madrid in the hydrometeor example, the route integrals there) are short RF-usable
intervals that a 60 s grid samples coarsely.

<!-- migration-table:start (generated by scripts/migration_table.py; do not edit) -->

#### `examples/scenarios/iss_cartagena.json`

| Metric | Unit | v1.0.0 | v2 events off | v2 events on | Change (on vs v1) | Reason |
| --- | --- | ---: | ---: | ---: | ---: | --- |
| visible_duration_s | s | 390 | 400 | 398.693 | +2.23 % | H, E |
| integrated_shannon_upper_bound_bits | bit | 1.86733e+09 | 1.89959e+09 | 1.89559e+09 | +1.51 % | T, Q |
| max_elevation_deg | deg | 61.58 | 61.58 | 61.5821 | +0.00342 % | C |
| min_range_m | m | 473677 | 473677 | 473670 | -0.00136 % | C |

#### `examples/constellations/iridium_global.json`

| Metric | Unit | v1.0.0 | v2 events off | v2 events on | Change (on vs v1) | Reason |
| --- | --- | ---: | ---: | ---: | ---: | --- |
| Madrid visible_fraction | 1 | 1 | 1 | 1 | 0 | = |
| Madrid rf_usable_fraction | 1 | 0.429752 | 0.433333 | 0.44798 | +4.24 % | M, E |
| Madrid best_link_integrated_bits | bit | 1.82873e+09 | 1.82873e+09 | 1.8961e+09 | +3.68 % | E |
| Madrid best_link_handovers | count | 2 | 2 | 5 | +150 % | E |
| Madrid fixed_capacity_integrated_bits | bit | 1.60425e+10 | 1.60425e+10 | 1.60425e+10 | 0 | = |
| Tromso visible_fraction | 1 | 1 | 1 | 1 | 0 | = |
| Tromso rf_usable_fraction | 1 | 0.958678 | 0.958333 | 0.938817 | -2.07 % | M, E |
| Tromso best_link_integrated_bits | bit | 4.01093e+09 | 4.01093e+09 | 3.94814e+09 | -1.57 % | E |
| Tromso best_link_handovers | count | 32 | 32 | 29 | -9.38 % | E |
| Tromso fixed_capacity_integrated_bits | bit | 1.60425e+10 | 1.60425e+10 | 1.60425e+10 | 0 | = |
| Singapore visible_fraction | 1 | 0.983471 | 0.983333 | 0.969403 | -1.43 % | M, E |
| Singapore rf_usable_fraction | 1 | 0.396694 | 0.4 | 0.382789 | -3.51 % | M, E |
| Singapore best_link_integrated_bits | bit | 1.65124e+09 | 1.65124e+09 | 1.6216e+09 | -1.79 % | E |
| Singapore best_link_handovers | count | 3 | 3 | 2 | -33.3 % | E |
| Singapore fixed_capacity_integrated_bits | bit | 1.57751e+10 | 1.57751e+10 | 1.55516e+10 | -1.42 % | E |
| Quito visible_fraction | 1 | 0.983471 | 0.983333 | 0.984095 | +0.0634 % | M, E |
| Quito rf_usable_fraction | 1 | 0.38843 | 0.391667 | 0.376221 | -3.14 % | M, E |
| Quito best_link_integrated_bits | bit | 1.71157e+09 | 1.71157e+09 | 1.64923e+09 | -3.64 % | E |
| Quito best_link_handovers | count | 2 | 2 | 1 | -50 % | E |
| Quito fixed_capacity_integrated_bits | bit | 1.57751e+10 | 1.57751e+10 | 1.57873e+10 | +0.0775 % | E |
| fixed_capacity connected_fraction | 1 | 1 | 1 | 1 | 0 | = |
| fixed_capacity integrated_bottleneck_bits | bit | 1.60425e+10 | 1.60425e+10 | 1.60425e+10 | 0 | = |
| fixed_capacity route_changes | count | 48 | 47 | 53 | +10.4 % | F, B |
| maximum_rate connected_fraction | 1 | 0.38843 | 0.391667 | 0.395573 | +1.84 % | M, B |
| maximum_rate integrated_bottleneck_bits | bit | 1.38249e+09 | 1.38249e+09 | 1.39627e+09 | +0.997 % | B |
| maximum_rate route_changes | count | 15 | 15 | 17 | +13.3 % | B |
| minimum_delay connected_fraction | 1 | 0.38843 | 0.391667 | 0.395573 | +1.84 % | M, B |
| minimum_delay integrated_bottleneck_bits | bit | 1.38249e+09 | 1.38249e+09 | 1.39627e+09 | +0.997 % | B |
| minimum_delay route_changes | count | 15 | 15 | 17 | +13.3 % | B |

#### `examples/constellations/iridium_global_reference.json`

| Metric | Unit | v1.0.0 | v2 events off | v2 events on | Change (on vs v1) | Reason |
| --- | --- | ---: | ---: | ---: | ---: | --- |
| Madrid visible_fraction | 1 | 1 | 1 | 1 | 0 | = |
| Madrid rf_usable_fraction | 1 | 0.421488 | 0.425 | 0.432573 | +2.63 % | M, E |
| Madrid best_link_integrated_bits | bit | 1.79931e+09 | 1.79931e+09 | 1.83311e+09 | +1.88 % | E |
| Madrid best_link_handovers | count | 2 | 2 | 5 | +150 % | E |
| Madrid fixed_capacity_integrated_bits | bit | 1.60425e+10 | 1.60425e+10 | 1.60425e+10 | 0 | = |
| Tromso visible_fraction | 1 | 1 | 1 | 1 | 0 | = |
| Tromso rf_usable_fraction | 1 | 0.917355 | 0.916667 | 0.912932 | -0.482 % | M, E |
| Tromso best_link_integrated_bits | bit | 3.80402e+09 | 3.80402e+09 | 3.82575e+09 | +0.571 % | E |
| Tromso best_link_handovers | count | 28 | 28 | 25 | -10.7 % | E |
| Tromso fixed_capacity_integrated_bits | bit | 1.60425e+10 | 1.60425e+10 | 1.60425e+10 | 0 | = |
| Singapore visible_fraction | 1 | 0.983471 | 0.983333 | 0.969403 | -1.43 % | M, E |
| Singapore rf_usable_fraction | 1 | 0.38843 | 0.391667 | 0.369645 | -4.84 % | M, E |
| Singapore best_link_integrated_bits | bit | 1.62182e+09 | 1.62182e+09 | 1.56849e+09 | -3.29 % | E |
| Singapore best_link_handovers | count | 3 | 3 | 2 | -33.3 % | E |
| Singapore fixed_capacity_integrated_bits | bit | 1.57751e+10 | 1.57751e+10 | 1.55516e+10 | -1.42 % | E |
| Quito visible_fraction | 1 | 0.983471 | 0.983333 | 0.984095 | +0.0634 % | M, E |
| Quito rf_usable_fraction | 1 | 0.380165 | 0.383333 | 0.368297 | -3.12 % | M, E |
| Quito best_link_integrated_bits | bit | 1.68216e+09 | 1.68216e+09 | 1.61507e+09 | -3.99 % | E |
| Quito best_link_handovers | count | 2 | 2 | 1 | -50 % | E |
| Quito fixed_capacity_integrated_bits | bit | 1.57751e+10 | 1.57751e+10 | 1.57873e+10 | +0.0775 % | E |
| fixed_capacity connected_fraction | 1 | 1 | 1 | 1 | 0 | = |
| fixed_capacity integrated_bottleneck_bits | bit | 1.60425e+10 | 1.60425e+10 | 1.60425e+10 | 0 | = |
| fixed_capacity route_changes | count | 48 | 47 | 53 | +10.4 % | F, B |
| maximum_rate connected_fraction | 1 | 0.355372 | 0.358333 | 0.361975 | +1.86 % | M, B |
| maximum_rate integrated_bottleneck_bits | bit | 1.26483e+09 | 1.26483e+09 | 1.27768e+09 | +1.02 % | B |
| maximum_rate route_changes | count | 13 | 13 | 17 | +30.8 % | B |
| minimum_delay connected_fraction | 1 | 0.355372 | 0.358333 | 0.361975 | +1.86 % | M, B |
| minimum_delay integrated_bottleneck_bits | bit | 1.26483e+09 | 1.26483e+09 | 1.27768e+09 | +1.02 % | B |
| minimum_delay route_changes | count | 13 | 13 | 17 | +30.8 % | B |

#### `examples/constellations/iridium_global_hydrometeors.json`

| Metric | Unit | v1.0.0 | v2 events off | v2 events on | Change (on vs v1) | Reason |
| --- | --- | ---: | ---: | ---: | ---: | --- |
| Madrid visible_fraction | 1 | 1 | 1 | 1 | 0 | = |
| Madrid rf_usable_fraction | 1 | 0.0330579 | 0.0333333 | 0.0236119 | -28.6 % | M, E |
| Madrid best_link_integrated_bits | bit | 1.17658e+08 | 1.17658e+08 | 8.3344e+07 | -29.2 % | E |
| Madrid best_link_handovers | count | 0 | 0 | 0 | 0 | = |
| Madrid fixed_capacity_integrated_bits | bit | 1.60425e+10 | 1.60425e+10 | 1.60425e+10 | 0 | = |
| Tromso visible_fraction | 1 | 1 | 1 | 1 | 0 | = |
| Tromso rf_usable_fraction | 1 | 0.917355 | 0.916667 | 0.912932 | -0.482 % | M, E |
| Tromso best_link_integrated_bits | bit | 3.80402e+09 | 3.80402e+09 | 3.82575e+09 | +0.571 % | E |
| Tromso best_link_handovers | count | 28 | 28 | 25 | -10.7 % | E |
| Tromso fixed_capacity_integrated_bits | bit | 1.60425e+10 | 1.60425e+10 | 1.60425e+10 | 0 | = |
| Singapore visible_fraction | 1 | 0.983471 | 0.983333 | 0.969403 | -1.43 % | M, E |
| Singapore rf_usable_fraction | 1 | 0 | 0 | 0 | 0 | = |
| Singapore best_link_integrated_bits | bit | 0 | 0 | 0 | 0 | = |
| Singapore best_link_handovers | count | 0 | 0 | 0 | 0 | = |
| Singapore fixed_capacity_integrated_bits | bit | 1.57751e+10 | 1.57751e+10 | 1.55516e+10 | -1.42 % | E |
| Quito visible_fraction | 1 | 0.983471 | 0.983333 | 0.984095 | +0.0634 % | M, E |
| Quito rf_usable_fraction | 1 | 0.231405 | 0.233333 | 0.220587 | -4.67 % | M, E |
| Quito best_link_integrated_bits | bit | 8.83442e+08 | 8.83442e+08 | 9.05305e+08 | +2.47 % | E |
| Quito best_link_handovers | count | 1 | 1 | 1 | 0 | = |
| Quito fixed_capacity_integrated_bits | bit | 1.57751e+10 | 1.57751e+10 | 1.57873e+10 | +0.0775 % | E |
| fixed_capacity connected_fraction | 1 | 1 | 1 | 1 | 0 | = |
| fixed_capacity integrated_bottleneck_bits | bit | 1.60425e+10 | 1.60425e+10 | 1.60425e+10 | 0 | = |
| fixed_capacity route_changes | count | 48 | 47 | 53 | +10.4 % | F, B |
| maximum_rate connected_fraction | 1 | 0.00826446 | 0.00833333 | 0.00573447 | -30.6 % | M, B |
| maximum_rate integrated_bottleneck_bits | bit | 2.94146e+07 | 2.94146e+07 | 2.02412e+07 | -31.2 % | B |
| maximum_rate route_changes | count | 0 | 0 | 0 | 0 | = |
| minimum_delay connected_fraction | 1 | 0.00826446 | 0.00833333 | 0.00573447 | -30.6 % | M, B |
| minimum_delay integrated_bottleneck_bits | bit | 2.94146e+07 | 2.94146e+07 | 2.02412e+07 | -31.2 % | B |
| minimum_delay route_changes | count | 0 | 0 | 0 | 0 | = |

<!-- migration-table:end -->
