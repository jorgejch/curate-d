# Tasks

One work stream: both files land in one commit (design.md, Parallelism).

## 1. Exclude docs/ and openspec/ from SonarQube analysis

- [x] 1.1 Add `test_analysis_excludes_docs_and_openspec_only` to `tests/unit/infrastructure/test_sonar_properties.py`, asserting `sonar.exclusions` is exactly `{"docs/**", "openspec/**"}` with messages naming a missing and an added pattern, and update the module docstring; verify it fails with `uv run pytest tests/unit/infrastructure/test_sonar_properties.py` (the property is absent)
- [x] 1.2 Set `sonar.exclusions=docs/**,openspec/**` in `sonar-project.properties`, with a comment on why both directories are out of analysis; verify `uv run pytest tests/unit/infrastructure/test_sonar_properties.py` passes, then `make lint`, `uv run mypy` and `make test` pass
- [x] 1.3 Push the branch and verify the `checks` job's SonarQube analysis of the branch succeeds and its scanner log reports `docs/**` and `openspec/**` under excluded sources

## Workflow follow-up

- Open the PR against `dev`; after it merges, verify with `curl -s 'https://sonarcloud.io/api/issues/search?componentKeys=jorgejch_curate-d&organization=jorge-haddad-oss&issueStatuses=OPEN,CONFIRMED&ps=1&facets=directories'` that no `docs/` or `openspec/` directory remains.
- Sync the delta into `openspec/specs/release-pipeline/spec.md`, archive the change, and run `openspec validate --all --strict` on the archive PR.
