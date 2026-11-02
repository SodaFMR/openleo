# Frozen OpenLEO v1.0.0 equivalence fixtures

These files record what OpenLEO **v1.0.0** computes for the four shipped example scenarios.
The 2.0 engine must reproduce them. They are generated once, from the tag, and committed;
nothing in the 2.0 code base may regenerate them as part of a normal test run.

| File | v1 source scenario | Kind |
| --- | --- | --- |
| `iss_cartagena.json.gz` | `examples/scenarios/iss_cartagena.json` | single pass (`simulate_scenario`) |
| `iridium_global.json.gz` | `examples/constellations/iridium_global.json` | constellation, free space |
| `iridium_global_reference.json.gz` | `examples/constellations/iridium_global_reference.json` | constellation, ITU gases |
| `iridium_global_hydrometeors.json.gz` | `examples/constellations/iridium_global_hydrometeors.json` | constellation, gases + declared rain/cloud, plus replay preparation |

`metadata.json` records the tag, commit, interpreter and library versions, the SHA-256 of
every fixture, the SHA-256 of each source scenario, and `calc_outputs` (see below).
`generate_v1_fixtures.py` is the generator. `tests/conftest.py` lists `fixtures` in
`collect_ignore`, so pytest never collects or imports the generator.

## Regenerating

The generator runs **only inside a v1.0.0 checkout**: it imports the v1 engine, which the 2.0
code base no longer contains, and reads the v1 example files at their v1.0.0 paths. It refuses
to run otherwise (it asserts the installed `openleo-link` version is `1.0.0`, that the checkout
is exactly tag `v1.0.0`, and that tracked files are unmodified).

```bash
# W is a 2.0 checkout (the destination of the fixtures)
W=<path-to-the-2.0-checkout>

git -C <openleo-main-checkout> worktree add --detach /tmp/openleo-v1 v1.0.0   # once; read-only
cd /tmp/openleo-v1
uv sync --locked --no-dev --extra plot
uv run --no-dev python "$W/tests/fixtures/v1/generate_v1_fixtures.py" \
    --output "$W/tests/fixtures/v1"
```

Expected output: four `*.json.gz` files and `metadata.json` in `--output`. Regenerating on the
recorded environment gives byte-identical files (verified by running twice, into different
directories and from different working directories).

Recorded environment (see `metadata.json`): tag `v1.0.0`, commit
`1d65868196c8a01e4af3cff216c4195eb40865dc`, CPython 3.13.14, numpy 2.5.2, skyfield 1.55,
sgp4 2.27, zlib 1.3.2, dependencies pinned by the tag's `uv.lock` (`--locked`).

Determinism: JSON is written with sorted keys, compact separators and floats exactly as
`json.dumps` renders them (`float.__repr__`, nothing rounded); gzip headers carry `mtime=0`
and no file name. The *compressed* bytes additionally depend on the zlib implementation, so
`metadata.json` also carries `fixture_json_sha256`, the SHA-256 of the uncompressed canonical
JSON, which is independent of zlib. Compare decoded content, not gzip bytes, in tests.

## Layout of each fixture

```
{scenario, timestamps_utc,
 satellites: [{index, norad_id, positions_ecef_m}],
 stations:   [{index, name, position_ecef_m}],
 links:      [{time_index, station_index, satellite_index, <every v1 link field>}],
 station_statistics, network_summary,
 routes:     [{time_index, model, nodes, delay_s, bottleneck_bps}],
 packet_replay_trace, network_replay_prepared}
```

`iss_cartagena` additionally has `trace_rows` and `summary`.

Field names are v1's own. Floats are v1's full-precision in-memory values (v1's CSV and JSON
writers round to 12 significant digits; these fixtures do not).

* `scenario`: constellations use v1's `scenario_document` (the validated input, with the
  catalog path relative to the scenario file); `iss_cartagena` stores the input JSON as read.
* `timestamps_utc`: the complete inclusive UTC grid, ISO 8601 with a `Z` suffix.
  `time_index` everywhere indexes this list.
* `satellites` / `stations`: ECEF (ITRS) metres. Selection: station indices 0-2 and satellite
  indices 0-9, all grid times. Indices are the original v1 indices.
* `links`: v1's per-sample, per-visible-pair dictionaries, restricted to that selection and
  prefixed with `time_index`. Constellation links carry v1's constellation field names
  (`delay_s`, `shannon_upper_bound_bps`, ...) plus the gas and hydrometeor fields when the
  scenario has them. `modcod` is `null` when unlocked.
* `station_statistics` (all four stations) and `network_summary` are v1's `statistics` and
  `network.summary`, complete and unfiltered.
* `routes`: one entry per frame (all frames) and per model (`minimum_delay`, `maximum_rate`,
  `fixed_capacity`). A disconnected sample keeps its slot with `nodes`, `delay_s` and
  `bottleneck_bps` all `null`. `nodes` use v1's full node numbering, computed over the
  **entire** catalog (satellite index `i`, station `k` is node `satellite_count + k`); route
  nodes may therefore name satellites outside the kept 0-9 selection.
* `packet_replay_trace`, `network_replay_prepared`: produced only for
  `iridium_global_hydrometeors`, `null` elsewhere (see below).
* `iss_cartagena` has no network layer in v1, so `station_statistics`, `network_summary`,
  `routes`, `packet_replay_trace` and `network_replay_prepared` are `null`. Its single
  satellite is index 0 (NORAD 25544) and its single station index 0. `links` holds one entry
  per row of the visible pass, with the `TraceRow` field names (minus `timestamp_utc`).
  `trace_rows` are the v1 `TraceRow` dataclass fields (`timestamp_utc` as ISO `Z`), and
  `summary` is the `SimulationSummary` dataclass (datetimes as ISO `Z`, `warnings` as a
  list). Mapping to v1's CSV header: `propagation_delay_s` is `delay_s` and
  `capacity_upper_bound_bps` is `shannon_capacity_upper_bound_bps`.

### Replay preparation (`iridium_global_hydrometeors` only)

Both entries store v1's complete prepared output, including `metadata.configuration`, the
exact configuration used. Window and load are common to both: start offset 0 s, duration 300 s,
packet size 1200 bytes, offered load 10,000 bit/s. Unspecified fields take v1's dataclass
defaults (`queue_packets=32`, `seed=1`).

* `packet_replay_trace`: `prepare_packet_replay` with `ReplayConfig(station_name="Madrid",
  norad_id=<NORAD of satellite index 3>)` (41920).
* `network_replay_prepared`: object keyed by routing model, each value the output of
  `prepare_network_replay` with `NetworkReplayConfig(routing_model=<model>,
  flows_per_direction=2, acquisition_delay_s=0.1)`.

## `calc_outputs`

`metadata.json` -> `calc_outputs` holds the SHA-256 of every file written by the v1 commands,
keyed by command name and then relative file name. `calc_commands` records the exact command
lines. The generator runs both through `openleo.cli.main` into temporary directories.

```
openleo gases examples/atmosphere/p676_13_validation.json --output <DIRECTORY>
openleo hydrometeors examples/atmosphere/hydrometeors_reference.json --output <DIRECTORY>
```

Their output does not depend on the output path (checked by running them from different
directories).

`calc/hydrometeors.json` is the `hydrometeors.json` file itself: its SHA-256 equals
`calc_outputs.hydrometeors["hydrometeors.json"]` (`tests/test_calc.py` checks this). It holds
full-precision floats, and Windows' C runtime rounds `exp`, `log10`, `pow` and `cos` differently
from glibc and macOS in the last bit, so on Windows `tests/test_calc.py` compares its values to
this file (relative 1e-12) instead of its hash. Every other calculator file, including both
CSV files and the HTML report, is still compared by hash on every OS.

## Other frozen v1 inputs

* `examples/`: byte copies of the v1.0.0 example inputs that the 2.0 tree replaced:
  `constellations/*.json` and `scenarios/iss_cartagena.json` (the four source scenarios above;
  their SHA-256 equal `fixture_sources` in `metadata.json`), `studies/*.json` and
  `sensitivity/iss_cartagena_oat.json` (the v1 study configurations the sweep examples
  replace), and `data/*.csv` (the two archived catalogs, so that the v1 files resolve their
  `../data/...` catalog paths). The upgrade, explain, CLI and sweep tests read them.
* `isl_edges.json`: v1.0.0 `openleo.network._frame(positions, (), NetworkConfig("A", "B",
  3e6, 1.0, 1.0), 72, 69, 70)["isl_edges"]` for the seeded positions of
  `tests/engine/test_routing.py::test_isl_edges_are_v1_edges_at_range_and_tangency_thresholds`,
  computed in a v1.0.0 checkout.
