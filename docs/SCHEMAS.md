# Schemas and compatibility

OpenLEO 2.0 has four public data contracts: the scenario (`openleo.scenario/2`), the results
directory (`openleo.results`, schema `"2"`), the OLC v1 binary table format, and the sweep spec
(`openleo.sweep/2`). The Python parsers are authoritative; every error starts with a JSON
pointer (`/time/step_s=...`) naming the offending value. OpenLEO 1.x files are not read by the
2.0 engine: `openleo scenario upgrade` converts 1.x scenarios (see [Migration](MIGRATION.md)).

## Scenario v2

A scenario is one UTF-8 JSON file of at most 1 MB with no duplicate object keys, no unknown keys
and no non-finite numbers. `schema` must be exactly `"openleo.scenario/2"`; a 1.x file fails with
"This is an OpenLEO 1.x scenario; run `openleo scenario upgrade FILE`". Relative catalog paths
resolve against the directory of the scenario file (for a symbolic link, the directory of the
file it points to).

| Key | Content |
| --- | --- |
| `schema`, `name`, `description` | identification; `description` optional |
| `time` | `start_utc`, `stop_utc` (UTC, `Z`), `step_s`; `events {enabled, tolerance_s}`; `convergence {mode, refinement_factor, relative_tolerance, absolute_tolerance_bits}` |
| `space_segment` | `catalogs` (path, `format: celestrak_gp_csv`, `source_url`, `retrieved_at_utc`, `terms_url`, `sha256`), `select_norad_ids` (null for all), `synthetic_rf` |
| `terminals` | named map of `{receive_gain_dbi, system_noise_temperature_k, miscellaneous_loss_db}` |
| `ground_segment` | `default_minimum_elevation_deg`; `stations` with `name`, `latitude_deg`, `longitude_deg`, `height_ellipsoid_m` (WGS84), `height_amsl_m`, `terminal` (default `"default"`), `minimum_elevation_deg` (null inherits the default) |
| `radio` | `carrier_frequency_hz`, `channel_bandwidth_hz`, `eirp_dbw` |
| `adaptation` | `standard: dvb-s2`, `table: v1-reference-subset`, `symbol_rate_baud`, `rolloff`, `implementation_margin_db`, `hysteresis_db`, `feedback_delay_s` |
| `propagation` | `model` (`free_space` or `itu_reference`), `refinement`, `hydrometeors` (null or `declared_uniform_layers` per station) |
| `network` | optional: `pairs` (1-16 ordered source/target station pairs), `isl {rule: all_pairs_within_range, max_range_m, capacity_bps}`, `fixed_capacity_bps`, `route_models` |
| `outputs` | `formats` (`csv` always; `olc`, `parquet`), `satellite_positions` (`grid` or `none`) |

Every numeric field carries its unit in its name or in the schema `description`, and the bounds
are v1's except where 2.0 lifts or adds them: window at most 31 days; at most 1,000,000 grid
samples; `step_s` a whole number of microseconds, at most the window and, with events on, at most
one eighth of the shortest orbital period in the catalogs; `events.tolerance_s` in [1e-6, 1] s;
`refinement_factor` an integer in [2, 16]; `convergence.mode` in `error`, `warn`, `off`;
`feedback_delay_s` in [0, 10] s; catalogs at most 10 MB per file and 20,000 objects; per-station
masks in [0, 90) deg and at least 5 deg under `itu_reference`. A memory estimate refuses runs
above `--memory-budget` (default 4 GiB) before propagation.

### JSON Schema

[`docs/schemas/scenario-2.schema.json`](schemas/scenario-2.schema.json) (draft 2020-12) is
generated from the field metadata of `openleo.scenario.model`; a test regenerates it and requires
byte equality, and another validates a corpus of valid and invalid scenarios with both the parser
and `jsonschema`, requiring identical outcomes outside the cases below.

**The schema is never looser than the parser.** Whatever the schema accepts, the parser accepts,
apart from rules JSON Schema cannot express, which only the parser checks (listed in the schema's
top-level `description`): bandwidth ≥ R_s(1 + α); the symbol-rate overflow guard; terminal
references and unique station names; route pairs naming distinct stations; hydrometeor stations
covering every station with rain tops above the station; stop after start, window and sample
limits and real calendar dates; `step_s ≤ P_min/8` (needs the catalogs); catalog hashes,
duplicate NORAD ids and size limits (checked when read); file size, duplicate keys and
non-finite numbers (properties of the text, not of a parsed instance).

The schema is **stricter** than the parser in these cases, so a validator in another language
never accepts what the parser would refuse:

- Timestamps (`time.start_utc`, `time.stop_utc`, `retrieved_at_utc`): the schema takes only the
  RFC 3339 form `YYYY-MM-DDTHH:MM:SS[.frac]Z`. The parser also accepts the other ISO 8601 UTC
  forms Python reads (a space for `T`, the basic form `20260830T061400Z`, no seconds, a comma as
  decimal sign). The schema pattern does not check the calendar (year `0000` or 30 February
  match it); the parser rejects them.
- URLs (`source_url`, `terms_url`): the schema needs an ASCII host without brackets or control
  characters (write internationalised names as punycode, `xn--`); the parser also accepts
  bracketed IPv6 and IPvFuture literals and non-ASCII hosts.
- Regular-expression dialect: the patterns are portable ECMA-262 and valid under the JavaScript
  `v` flag. Whitespace is spelled out as the set Python's `str.isspace` uses (ECMA-262 `\s`
  differs: it includes U+FEFF and omits U+001C to U+001F and U+0085); a `/` in a character
  class is escaped; and the end anchor is `$(?!\n)`, because Python's `$` also matches before a
  final newline, which the parser rejects.

`openleo scenario new` prints a commented starter, `validate` checks a file, `explain` lists every
assumption with its evidence class (archived input, declared assumption or model choice), its
unit and the active limitations, and `upgrade` converts a 1.x file.

## Results v2

`openleo run` (and `save_run`) publishes a directory:

```
RUN/
  results.json            metadata (below)
  tables/<name>.csv       always
  tables/<name>.olc       when "olc" is in outputs.formats (default)
  tables/<name>.parquet   when "parquet" is requested (needs the parquet extra)
  inputs/scenario.json    the normalised scenario, catalog paths rewritten to catalogs/<file>
  inputs/catalogs/<file>  byte copies of the catalogs
  manifest.json           SHA-256 of every file except itself
```

`results.json` keys (sorted, UTF-8, `ensure_ascii=False`):

| Key | Content |
| --- | --- |
| `kind`, `schema_version` | `"openleo.results"`, `"2"` |
| `software` | openleo, numpy, skyfield and sgp4 versions, Python, platform |
| `scenario` | the normalised scenario plus `source_sha256` (original file) and `normalised_sha256` (canonical JSON) |
| `provenance` | catalogs (source, retrieval time, terms, SHA-256, record and selected counts, archived path), element epochs and ages, leap-second table source and SHA-256 |
| `models` | one entry per kernel: standard, version, method; e.g. ETSI EN 302 307-1 V1.4.1 Table 13 subset, P.676-13, P.835-7, P.453-14, P.838-3, P.840-9, P.525-5, the time and integration conventions and the gas-table bounds |
| `events` | `enabled`, `tolerance_s`, `integration` (`event_segments` or `grid_left_hold`), counts by type |
| `convergence` | settings, summary by class (link, station, route), failures, `skipped` |
| `warnings` | element age above 14 days, convergence warnings, the `synthetic_rf` notice |
| `limitations` | the v1 limitation lines plus the S1 additions (route breakpoints, fixed-capacity threshold waiver, route convergence not gated, event-time accuracy), each under the condition listed in [Scope](SCOPE.md#openleo-20-s1-limitations) |
| `summary` | headline metrics with units in their key names, per station and per route |
| `tables` | name → `{rows, columns: [{name, dtype, unit, description}], files: {csv, olc, parquet: {path, sha256}}}` |

The manifest is not echoed in `results.json` (the manifest covers `results.json`); the SHA-256 of
`manifest.json` is the run's integrity anchor. `load_results` verifies the manifest and every
table hash.

### Tables

Column names carry units (`_s`, `_m`, `_deg`, `_mps`, `_hz`, `_db`, `_db_hz`, `_bps`, `_bits`,
`_fraction`, `_days`); dtypes are `f64`, `f32`, `i64`, `i32`, `u8` (`bool` unit for flags) and
`str`.

| Table | Row | Columns |
| --- | --- | --- |
| `satellites` | satellite | index, name, norad_id, epoch_utc, max_abs_element_age_days |
| `stations` | station | index, name, latitude_deg, longitude_deg, height_ellipsoid_m, height_amsl_m, minimum_elevation_deg, terminal, x/y/z_ecef_m |
| `positions` | satellite × grid time | satellite_index, time_index, t_offset_s, x/y/z_ecef_m |
| `samples` | visible (station, satellite, grid time) | station_index, satellite_index, time_index, t_offset_s, elevation_deg, azimuth_deg, range_m, range_rate_mps, doppler_hz, delay_s, cn0_db_hz, esn0_db, snr_db, shannon_upper_bound_bps, the gas and hydrometeor fields when modelled, modcod, rate_bps, margin_db, remaining_contact_s, contact_truncated |
| `events` | event | station_index, satellite_index, t_offset_s, type (aos, los, culmination, modcod_decision, modcod_effective), modcod_from, modcod_to, elevation_deg, decision_offset_s |
| `passes` | pass | station_index, satellite_index, pass_index, aos/los/culmination_offset_s, truncated_start, truncated_end, duration_s, rf_usable_duration_s, integrated_rate_bits, integrated_shannon_upper_bound_bits, max_elevation_deg |
| `rate_segments` | piecewise-constant segment | station_index, satellite_index, start/end_offset_s, modcod, rate_bps |
| `pair_metrics` | pair | station_index, satellite_index, norad_id, pass_count, visible_duration_s, rf_usable_duration_s, integrated_rate_bits, integrated_shannon_upper_bound_bits, max_elevation_deg, min_range_m, mean_rate_visible_bps, residence_<modcod>_s |
| `station_metrics` | station | station_index, name, visible_fraction, rf_usable_fraction, best_link_integrated_bits, best_link_handovers, best_link_mean_rate_bps, fixed_capacity_integrated_bits |
| `routes` | (pair, model, route interval) | pair_index, model_index, source, target, model, start/end_offset_s, connected, nodes (space-separated node indices), delay_s, bottleneck_bps |
| `route_metrics` | (pair, model) | pair_index, model_index, source, target, model, connected_fraction, integrated_bottleneck_bits, route_changes, mean_delay_connected_s |
| `convergence` | metric | metric, class, primary, refined, absolute_difference, relative_difference, passed, gated |

`routes` and `route_metrics` exist only with a `network`; `positions` only with
`satellite_positions: "grid"`. Satellites and stations are numbered in catalog and scenario
order. The `convergence` table's values carry the unit of each metric (named by its id), so its
columns declare the unit `metric unit`.

### Time columns and CSV conventions

Times are float64 seconds after `scenario.time.start_utc` in columns ending in `_offset_s` (and
`t_offset_s`). OLC and Parquet store only the offsets. CSV additionally inserts, after every
`*_offset_s` column, an ISO 8601 UTC text column named by replacing `_offset_s` with `_utc`: so
`t_offset_s` is followed by `t_utc`, `aos_offset_s` by `aos_utc` and `decision_offset_s` by
`decision_utc`. Its value is `scenario.time.start_utc` plus the offset, rounded to the
microsecond and ending in `Z`; it is empty when the offset is NaN. (The design spec's `time_utc`
for the `events` table is therefore `t_utc`.) The text width varies: whole seconds have no
fraction.

- UTF-8, LF line endings, a header row, standard CSV quoting.
- `f64` cells are `repr(float)` (shortest round trip); `f32` cells the shortest text that
  round-trips the float32. **NaN is an empty cell**; infinities are `inf` and `-inf`.
- `bool` columns are `true`/`false`.
- Rows of every format are ordered by the key columns (the `*_index` columns in column order, then
  the first `*_offset_s` column), stably, NaN last.

On one platform, identical inputs give byte-identical files.

## OLC v1

OLC is a little-endian binary columnar format for the app API and the browser. Bytes in order:

1. the magic `OLC1`;
2. a uint32 little-endian header length H;
3. H bytes of UTF-8 JSON (sorted keys, compact): `{"table", "rows", "columns": [{"name",
   "dtype", "unit", "description", "offset", "length", "sha256"}]}`, where `offset` is absolute
   from the first byte of the file, `length` counts payload bytes (no padding) and `sha256` is
   the hex digest of the payload. `description` is optional: readers must accept its absence
   (it is the only key beyond the design spec's list);
4. zero padding to the next multiple of 8 bytes;
5. one payload per column, in header order, each starting on an 8-byte boundary (zero padding
   between payloads, none after the last). Numeric payloads (`f64`, `f32`, `i64`, `i32`, `u8`)
   are little-endian arrays of `rows` values. A `str` payload is `rows + 1` little-endian
   uint32 byte offsets (starting at 0) followed by the concatenated UTF-8 bytes.

The file ends exactly at the end of the last payload. The reader checks the magic, every bound,
the alignment, the padding, the lengths and every column's SHA-256. Golden files shared with
other readers are in `tests/fixtures/olc/`.

## Sweep v2

```json
{
  "schema": "openleo.sweep/2",
  "name": "...",
  "base_scenario": "../constellations/iridium_global_hydrometeors.json",
  "mode": "oat | grid | variants",
  "parameters": [{"path": "/radio/eirp_dbw", "values": [12, 15, 18], "nominal": 15}],
  "variants": [{"id": "free_space", "label": "Free space", "baseline": true,
                "patch": [{"op": "replace", "path": "/propagation", "value": {"model": "free_space"}}]}],
  "window": {"start_offset_s": 0, "duration_s": 300},
  "replay": {"network": {"loads_bps": [10000], "flows_per_direction": 2}},
  "max_runs": 512
}
```

`base_scenario` is relative to the sweep file. `parameters` (for `oat` and `grid`) and `variants`
(for `variants`) are mutually exclusive. Paths are RFC 6901 JSON Pointers into the normalised
scenario; patches are the RFC 6902 subset `replace`, `add` and `remove`. Sweeps may not move
`time.start_utc` or `time.stop_utc`; `window` clips every run to part of the base window instead
(at most 3600 s when a replay is requested). `replay.link` (`station`, `norad_id`, `loads_bps`,
packet settings) or `replay.network` (`loads_bps` and optional `routing_models`,
`flows_per_direction`, `acquisition_delay_s`, packet settings) replays every run through the
backends given on the command line. At most 8 loads; `max_runs` at most 512.

- `oat`: every parameter has a `nominal` equal to the base value; runs are `oat:nominal` plus one
  per differing value; the baseline is the nominal run.
- `grid`: the Cartesian product, first parameter slowest, `grid:0`, `grid:1`, ...; the baseline
  is `grid:0`.
- `variants`: the listed patches, `variant:<id>`; the baseline is the variant flagged
  `"baseline": true`, else the first.

Output: `sweep.json` (spec, its SHA-256, the base scenario hash, every run's id, label,
directory, scenario SHA-256 and replay summary), `comparisons.csv` (`run_id, metric, value,
baseline_value, delta, ratio`; zero-effect rows kept; labelled "deterministic comparison; not an
uncertainty interval"), `runs/<id>/` (one results directory per run; ids are percent-encoded,
so `variant:free_space` is `runs/variant%3Afree_space`; ids that differ only by case or end with
a dot are refused) and `manifest.json`. See
[Sweeps](SWEEPS.md).

## Publication semantics

Every published directory is built in a temporary sibling and renamed into place, so readers see
either nothing or a complete directory:

- runs, sweeps, exports, reproductions and app jobs (`publish_atomically`): built in
  `<target>.tmp-<random>`; the target must not exist, which is checked before building and again
  just before one `os.rename`;
- reports, link replays and network replays: built in a hidden `.<target>.*` sibling and moved
  with `os.replace`; a report also accepts an empty existing directory as its target.

On any failure the temporary directory is removed and nothing is published. Limitations:

- **Empty-target race.** On POSIX, a rename replaces an *empty* existing directory. If another
  process creates an empty directory at the target between the final existence check and the
  rename, it is silently replaced (a non-empty one makes the rename fail, which is reported).
  Do not point concurrent jobs at the same output path.
- **No fsync.** Files and the rename are not flushed to stable storage, so a power loss or
  kernel crash shortly after publication can leave incomplete files. The manifest detects this:
  `load_results` refuses a directory whose hashes do not match.
- Archived catalog names that differ only by case are not refused; on case-insensitive file
  systems (default macOS and Windows) they collide. (Sweep run ids that differ only by case, or
  end with a dot, which Windows strips, are refused when the sweep is expanded.)

## Compatibility policy

Within 2.x, the schema identifiers above name stable contracts: new optional fields may be added
with defaults that preserve existing meaning, and a change of meaning, unit or required field
needs a new schema identifier. The parsers reject unknown keys, so an older reader does not
accept newer optional fields. Private helpers beginning with `_` are not a supported API. The
1.x contracts (workbench `experiment.json` schemas 1/2/3 and the study result kinds) are not read
by 2.0; v1.0.0 remains available at its tag.
