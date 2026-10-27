# Tasks

## 1. Stream A: Sonar configuration and regression tests

- [x] 1.1 Add `compose.yaml` to expected source, text and coverage scopes in `tests/unit/infrastructure/test_sonar_properties.py`; run the targeted tests before and after updating `sonar-project.properties`.

## 2. Stream B: Contributor guidance (independent of A)

- [x] 2.1 Document Compose scope and supported-analysis caveats in `CONTRIBUTING.md`; verify with `git diff --check`.

## 3. Final validation and specification lifecycle (depends on A and B)

- [x] 3.1 Run the full unit suite and targeted Ruff lint/format checks.
- [x] 3.2 Sync the release-pipeline delta, validate with `openspec validate --all --strict`, and archive the change.