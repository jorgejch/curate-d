## Why

The explicit Sonar scope omits the production image definition and two runtime/project
configuration files. Include these assets without widening analysis to unrelated paths.

## What Changes

- Add `docker/`, `pyproject.toml` and `alembic.ini` to source scope.
- Keep coverage on `src/` only and leave duplication enabled for supported source files.
- Enable text/secret analysis for scoped text files, including TOML and INI files without
  a dedicated language analyzer; do not disguise them as another language.
- Update scope tests, contributor guidance and the release-pipeline requirements.

## Capabilities

### Modified Capabilities

- `release-pipeline`: expand explicit source scope to image and configuration assets.

## Impact

`sonar-project.properties`, `tests/unit/infrastructure/test_sonar_properties.py`,
`CONTRIBUTING.md` and `openspec/specs/release-pipeline/spec.md`. No application,
Dockerfile, dependency, migration or CI workflow changes.

## Non-goals

Repository-wide analysis, package vulnerability scanning, new language analyzers,
Docker syntax changes, integration coverage or rule suppressions.