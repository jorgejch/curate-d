# Tasks

One work stream: both files land in one commit (design.md, Parallelism).

## 1. Exclude docs/ and openspec/ from SonarQube analysis

- [x] 1.1 Add `test_analysis_excludes_docs_and_openspec_only` to `tests/unit/infrastructure/test_sonar_properties.py`, asserting `sonar.exclusions` is exactly `{"docs/**", "openspec/**"}` with messages naming a missing and an added pattern, and update the module docstring; verify it fails with `uv run pytest tests/unit/infrastructure/test_sonar_properties.py` (the property is absent)
- [x] 1.2 Set `sonar.exclusions=docs/**,openspec/**` in `sonar-project.properties`, with a comment on why both directories are out of analysis; verify `uv run pytest tests/unit/infrastructure/test_sonar_properties.py` passes, then `make lint`, `uv run mypy` and `make test` pass
- [x] 1.3 Push the branch and verify the `checks` job's SonarQube analysis of the branch succeeds and its scanner log reports `docs/**` and `openspec/**` under excluded sources

## Workflow follow-up

- Merged in pull request #95. `dev`'s first analysis after it (revision `90a8e2c`) reports 77 open issues, down from 1,963, with no `docs/` or `openspec/` directory left.
- Synced into `openspec/specs/release-pipeline/spec.md` and archived in pull request #96, with `openspec validate --all --strict` passing.
