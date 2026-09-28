# Contributing

OpenLEO accepts focused changes that preserve reproducibility, scientific provenance,
and explicit claim boundaries. Repository code, documentation, examples, generated
artifacts, issues, and pull requests are written in English.

## Development setup

Install Python 3.12 or newer and `uv`, then create the contributor environment:

```bash
uv sync --locked --group dev --extra plot
```

Browser tests use a separate dependency group:

```bash
uv sync --locked --group dev --group browser --extra plot
uv run --group browser playwright install chromium
```

Users do not need the development or browser groups; the user setup is documented in
the [README](README.md).

## Before changing scientific behavior

Read the [scope](docs/SCOPE.md), [methods](docs/METHODS.md),
[schema contracts](docs/SCHEMAS.md), and the model-specific document for the
code you are changing. Supported v1.0 APIs, units, and artifact meanings are
stable; compatible evolution is additive. Breaking changes require a new
schema version and a future major software release.

- Record source URL, retrieval time, terms or license, checksum, units, and assumptions
  for every external scientific input.
- Label synthetic station, RF, terminal, and network values as assumptions.
- Keep public times in timezone-aware UTC and identify coordinate frames.
- Keep units in schema and column names.
- Fail on invalid or non-finite inputs; do not silently discard failed propagation.
- Describe Shannon-Hartley results as theoretical upper bounds and DVB-S2 values as
  reference rates, never measured throughput.
- Do not claim PyPI availability, a DOI, calibrated validation, peer review, operator
  performance, or commercial service behavior without repository evidence.

Preserve the exact Recommendation versions, source URLs, hashes, adapted-file lists,
and full notice associated with the P.676-13 implementation. The official workbook is
validation evidence and must not be redistributed. See
[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).

Reference-profile changes must preserve the distinctions between geometric and
apparent elevation, ellipsoidal and AMSL height, total and dry pressure, numerical
refinement and physical uncertainty, and reference atmosphere and measured weather.

Coupled rain/cloud changes must retain explicit uniform-state declarations and
the P.838-3 dB/km to path-loss conversion, without implying P.618 statistics.
Network changes must preserve selected-route edge membership, shared directed
queues, acquisition, causal native hop observations, packet accounting, and
measurement-window censoring. Browser study requests must never select backend
executables or filesystem output paths; preserve loopback, Host/Origin, token,
pinned-catalog, request-rate, and bounded-artifact protections.

Never replace an archived catalog in place. Add a new dated snapshot with its source
metadata and checksum.

## Development workflow

Behavior changes use test-driven development:

1. Add a focused test and confirm the expected failure.
2. Implement the smallest correct change.
3. Run the focused test until it passes.
4. Run the complete checks below.

Keep coverage at or above 80%. Prefer immutable values and existing repository
patterns. Validate external data at the boundary and include useful error context
without leaking secrets or local credentials.

Use Conventional Commits, for example:

```text
feat: add study metric
fix: reject invalid sampling grid
docs: clarify propagation assumptions
test: cover output hash mismatch
```

## Required checks

```bash
uv lock --check
uv run ruff check src tests
uv run ruff format --check src tests
uv run pytest --cov=openleo --cov-report=term-missing \
  --cov-report=xml --cov-fail-under=80
uv run python -m build
uvx cffconvert==2.0.0 --validate
git diff --check
```

Run the dependency-free browser checks:

```bash
node --test tests/test_workbench_ui.mjs
```

Run real-browser coverage for workbench or UI changes:

```bash
uv run openleo constellation \
  examples/constellations/iridium_global.json \
  --output runs/global
uv run --group browser python tests/browser_workbench.py
uv run --group browser python tests/browser_workbench.py \
  --scenario examples/constellations/iridium_global_reference.json
uv run --group browser python tests/browser_experiment.py
```

On systems with an existing Chromium installation, pass it explicitly:

```bash
uv run --group browser python tests/browser_workbench.py \
  --executable /usr/bin/chromium
```

Changes to packet replay also require the actual simulator, not just parser tests.
Build the [optional ns-3 backends](https://github.com/SodaFMR/openleo/blob/main/adapters/ns3/README.md), then run:

```bash
uv run python tests/ns3_replay_checks.py ../ns-3.48/build/scratch/ns3.48-openleo-replay
OPENLEO_NS3_REPLAY=../ns-3.48/build/scratch/ns3.48-openleo-replay \
  uv run pytest tests/test_packet_replay.py tests/test_packet_cli.py
uv run python tests/ns3_network_checks.py \
  ../ns-3.48/build/scratch/ns3.48-openleo-network-replay
OPENLEO_NS3_NETWORK=../ns-3.48/build/scratch/ns3.48-openleo-network-replay \
  uv run pytest tests/test_network_replay.py tests/test_experiment.py \
    tests/test_benchmark.py tests/test_release_evidence.py
```

Use Linux, macOS, or WSL2 for these optional backend checks. The core remains
independent of ns-3; its distributions exclude both GPL-licensed adapters and
their shared header. Core-only runs skip optional native integration checks;
they do not establish actual simulator evidence.

Verify the published TEME fixture after changes to orbit loading or propagation:

```bash
uv run --no-dev openleo verify-orbit examples/validation/vallado_06251.json
```

The full frozen matrix is a separate release run, documented in
[Reproducibility](docs/REPRODUCIBILITY.md). Numerical reference agreement and
synthetic packet replay are not observational RF validation. Keep full release
evidence distinct from model-only benchmark output.

Regenerate only artifacts affected by the change. Inspect the generated summary,
manifest, units, assumptions, limitations, and source fingerprints. Leave `runs/`
untracked unless a task explicitly adds a reviewed fixture.

## Pull requests

A pull request should state:

- the scientific or software change;
- assumptions and provenance;
- tests and regenerated artifacts;
- cross-platform impact;
- limitations or deferred work; and
- applicable data or license terms.

Do not commit secrets, local credentials, generated environments, or unrelated output.
