# App API (workbench backend)

In OpenLEO 2.0 (S1), `openleo app` serves the local run and sweep API that the new workbench will
use. **The browser workbench itself arrives in a later milestone (S8)**; the v1 interface
(globe, timeline, budget panels, aligned comparisons) is retired on this branch and remains in
v1.0.0. Until then `/` serves a placeholder page, and the API can be used directly.

```bash
uv run --no-dev openleo app examples/constellations/iridium_global_hydrometeors.json --no-browser \
  [--port 8765] [--packet-backend EXE] [--network-backend EXE] [--artifact-limit MiB]
```

The server binds to `127.0.0.1` (default port 8765) and pins the scenario it was started with,
including its catalogs. `--artifact-limit` bounds each job's results on disk (default 256 MiB).

## Routes

| Route | Method | Token | Response |
| --- | --- | --- | --- |
| `/` | GET | no | the placeholder page (`openleo/_web/dist/index.html`) with the session token |
| `/api/capabilities` | GET | no | versions, available backends, extras installed, limits |
| `/api/scenario` | GET | yes | the pinned scenario, its hash and the scenario JSON Schema |
| `/api/scenario/validate` | POST | yes | `{valid, scenario \| errors}` (JSON-pointer errors) |
| `/api/runs` | POST | yes | `{scenario}` → `202 {id}` |
| `/api/runs/{id}` | GET | yes | `{id, state, phase, error}`; state `queued`, `running`, `done`, `failed` (or `evicted`) |
| `/api/runs/{id}/results.json` | GET | yes | the run's `results.json` |
| `/api/runs/{id}/tables/{name}.olc` | GET | yes | one table in [OLC v1](SCHEMAS.md#olc-v1) |
| `/api/runs/{id}/export.zip` | GET | yes | the run directory as a ZIP with sorted entries and fixed metadata |
| `/api/sweeps` | POST | yes | `{spec[, scenario]}` → `202 {id}` |
| `/api/sweeps/{id}` | GET | yes | status, as for runs |
| `/api/sweeps/{id}/results.json` | GET | yes | the sweep's `sweep.json` |
| `/api/sweeps/{id}/tables/comparisons.olc` | GET | yes | the comparisons as OLC |
| `/api/sweeps/{id}/export.zip` | GET | yes | the sweep directory as a ZIP |
| `/api/sweeps/{id}/runs/{n}/results.json` | GET | yes | run `n` of the sweep |
| `/api/sweeps/{id}/runs/{n}/tables/{name}.olc` | GET | yes | a table of run `n` |

Runs and sweeps execute one at a time in a worker thread. A job id is the SHA-256 of the
normalised request, so resubmitting an identical request returns the cached result; a later
failure never replaces a result already produced. Finished jobs together stay within four
artifact limits: the least recently used are evicted and answer 404 until resubmitted.

## Security model

The v1 security model is kept:

- **Loopback only.** Every request must carry exactly one `Host` of `127.0.0.1:<port>` or
  `localhost:<port>` and, if it has an `Origin`, exactly `http://<that host>`; at most 60 such
  requests are served per second.
- **Session token.** The server creates a random token at start and inserts it into the page as
  `<meta name="openleo-token" content="...">` right after `<head>`. Every POST and every data GET
  (`/api/scenario`, `/api/runs/*`, `/api/sweeps/*`) must send it in exactly one
  `X-OpenLEO-Token` header. The token never appears in a URL.
- **Strict bodies.** POST bodies must be `application/json` with one decimal `Content-Length` of
  at most 1,000,000 bytes, no `Transfer-Encoding`, no repeated keys and no non-finite numbers.
- **Pinned inputs.** Every submitted scenario is validated against the directory of the startup
  scenario, and its normalised `space_segment` must equal the pinned one (403 otherwise); sweep
  parameters and patches may not touch `/space_segment`, and every generated run is checked
  again. Sweep specs carry no `base_scenario`. No request field names a file, an executable or an
  output path; backends are the ones given at startup. Error messages name catalogs only by their
  scenario-relative paths.
- **Bounded disk.** A job whose files exceed the artifact limit fails and leaves nothing behind;
  an export that would take its job past the limit is refused (507).
- **Headers.** Every response is `Cache-Control: no-store`, `X-Content-Type-Options: nosniff` and
  `X-Frame-Options: DENY`; the page also carries a restrictive `Content-Security-Policy`
  (`default-src 'self'`, no framing, no form actions). Every error is `{"error": message}`.
  Request lines are never logged, and unexpected exceptions are logged by type only.

Results live in a server-owned temporary directory that is removed at shutdown; use `export.zip`
or the CLI (`openleo run`, `openleo sweep`) for durable artifacts. The display of computed
results is not live telemetry.
