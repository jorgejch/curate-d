# Tasks

One work stream: three files, one logical commit.

## 1. Secret detection on docs/

- [x] 1.1 Add `docs/*` to `sonar.text.inclusions` in `sonar-project.properties` with its comment; pin the expected inclusions to the source/test mirror plus `docs/*` in `tests/unit/infrastructure/test_sonar_properties.py`; list the files directly in `docs/` as secret detection only in `CONTRIBUTING.md`'s SonarQube scope section; verify with `uv run pytest tests/unit/infrastructure/test_sonar_properties.py --no-cov`

## Workflow follow-up

- Sync the delta spec into `openspec/specs/release-pipeline/spec.md`, then archive the change.
- Run `openspec validate --all --strict` on the archive.
- After merge, check the next `dev` scan indexes the files directly in `docs/` for secrets only, and nothing under `docs/diagrams/`.
