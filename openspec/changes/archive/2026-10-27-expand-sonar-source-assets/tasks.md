## 1. Stream A — test contract

- [x] 1.1 Update `tests/unit/infrastructure/test_sonar_properties.py` for explicit Docker/config paths, src-only coverage and scoped text detection; correct stale Docker docstrings and assert configured paths exist. Verify red with `uv run --locked --no-build pytest tests/unit/infrastructure/test_sonar_properties.py --no-cov` before editing properties.

## 2. Stream B — contributor guidance (independent of A)

- [x] 2.1 Update `CONTRIBUTING.md` scope table, out-of-scope examples and text-analysis limitations; verify it matches the proposed properties and does not promise TOML/INI language checks.

## 3. Coordinator — settings and verification (waits for A's red check)

- [x] 3.1 Update `sonar-project.properties` sources, coverage exclusions, text settings and comments; run the targeted scope tests green, targeted ruff checks and `uv run --locked --no-build pytest -n auto tests/unit`.
- [x] 3.2 After A and B complete, validate the change, sync `openspec/specs/release-pipeline/spec.md`, validate the synced spec and archive with `openspec archive expand-sonar-source-assets --skip-specs --yes`.

## Verification results

- Old settings: three expected failures (sources, coverage exclusions, text activation),
  six passes. Updated settings: all nine targeted scope tests pass.
- Targeted ruff lint and format checks pass. All 2037 unit tests pass with 84.88% coverage.
- Strict change and synced release-pipeline spec validation pass; `git diff --check` passes.
- Cloud indexing, Docker parser support and actual findings await an authenticated CI scan.