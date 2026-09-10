# Contributing

OpenLEO work is English-only for code, documentation, commit messages, issues, pull
requests, examples, and generated artifacts intended for the repository.

## Setup

Recommended:

```bash
uv sync --group dev --extra plot
```

Standard Python fallback:

```bash
python -m venv .venv
```

Activate the virtual environment:

```bash
source .venv/bin/activate
```

Windows PowerShell:

```powershell
.venv\Scripts\Activate.ps1
```

Then install the package and development tools:

```bash
python -m pip install -e ".[plot]"
python -m pip install build pytest pytest-cov ruff
```

## Development Workflow

Use focused branches named for the change, such as:

- `feat/pass-trace`
- `fix/reject-naive-timestamps`
- `docs/link-budget-note`
- `ci/platform-matrix`

Use Conventional Commits:

```text
feat: add visible pass simulation
fix: reject invalid RF bandwidth
docs: explain synthetic RF assumptions
test: cover CLI input errors
ci: test supported Python versions
```

Behavior changes follow TDD:

1. Add one failing test for the behavior.
2. Run the focused test and confirm the expected failure.
3. Implement the smallest correct change.
4. Run the focused test again.
5. Run the full verification commands before committing.

Coverage must stay at or above 80%.

## Scientific Provenance

Read [docs/PROJECT_CHARTER.md](docs/PROJECT_CHARTER.md) before changing scientific
behavior.

Every external scientific input must record its source URL, retrieval timestamp,
terms or license URL, checksum when practical, units, and assumptions. Synthetic
scenarios must be labelled synthetic even when their orbital geometry is sourced
from public records.

The frozen ISS example uses real/frozen CelesTrak GP geometry. Its RF values are
synthetic OpenLEO assumptions. See [THIRD_PARTY_DATA.md](THIRD_PARTY_DATA.md).

The P.676-13 benchmark uses official ITU validation outputs and attributed coefficient
data adapted from a pinned MIT-licensed ITU-Rpy commit. Preserve exact Recommendation
versions, source URLs, checksums, adapted-file lists, and the full notice in
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). Do not commit or redistribute the
official workbook. Describe these outputs as specific attenuation in dB/km at declared
homogeneous conditions, never as integrated path loss or weather.

Do not claim PyPI publication, DOI, calibrated validation, peer-reviewed results,
operator performance, achieved throughput, or commercial service behavior unless
the repository contains the supporting release or evidence.

## Required Checks

The workbench also has dependency-free JavaScript checks and a real-browser suite:

```bash
node --test tests/test_workbench_ui.mjs
uv run openleo constellation examples/constellations/iridium_global.json --output runs/global
uv run --group browser playwright install chromium
uv run --group browser python tests/browser_workbench.py
```

On Arch Linux, use the installed Chromium with
`uv run --group browser python tests/browser_workbench.py --executable /usr/bin/chromium`.
Browser packages are development-only; using the application requires neither
Playwright nor Node. Changes to the scientific producer must preserve honest source
and assumption labels in the UI and exports. Never replace the original archived
catalog bytes when refreshing an example; add a new dated snapshot with its source hash.

Run these before opening a pull request:

```bash
uv sync --locked --group dev --extra plot
uv lock --check
uv run ruff check src tests
uv run ruff format --check src tests
uv run pytest --cov=openleo --cov-report=term-missing --cov-report=xml --cov-fail-under=80
uv run python -m build
uv run openleo run examples/scenarios/iss_cartagena.json --output runs/iss
uv run openleo plot runs/iss --output docs/images/iss-cartagena-pass-overview.svg
uv run openleo sensitivity examples/scenarios/iss_cartagena.json \
  examples/sensitivity/iss_cartagena_oat.json --output runs/iss-sensitivity
uv run openleo plot-sensitivity runs/iss-sensitivity \
  --output docs/images/iss-cartagena-sensitivity-overview.svg
uv run openleo gases examples/atmosphere/p676_13_validation.json \
  --output runs/p676-validation
uv run openleo plot-gases runs/p676-validation \
  --output docs/images/p676-13-specific-attenuation.svg
uvx cffconvert==2.0.0 --validate
git diff --exit-code -- docs/images/iss-cartagena-pass-overview.svg \
  docs/images/iss-cartagena-sensitivity-overview.svg \
  docs/images/p676-13-specific-attenuation.svg
git diff --check
```

Inspect `runs/iss/summary.json` and
`runs/iss-sensitivity/sensitivity-summary.json`, plus both files in
`runs/p676-validation/`, when documentation or examples mention reference values. Leave
`runs/` untracked. Deterministic OAT ranges must not be described as uncertainty
intervals, and heterogeneous RF sweep spans must not be ranked as though they were
comparable uncertainties.

## Pull Requests

Pull requests should state:

- the scientific or software change;
- assumptions and provenance;
- tests and generated artifacts;
- cross-platform impact;
- limitations or deferred work; and
- any data terms that apply.
