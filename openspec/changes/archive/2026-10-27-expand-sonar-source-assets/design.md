## Context

The scanner uses explicit disjoint roots: `src,ops,migrations` and `tests`. The scope
tests pin these roots and coverage exclusions; contributor guidance describes the same
contract. The production Dockerfile uses syntax 1.19 and must not be modified just to
fit an analyzer's documented syntax support.

## Goals / Non-Goals

Include `docker/`, `pyproject.toml` and `alembic.ini` without adding unrelated root files.
Keep unit-only src coverage and normal duplication analysis. Do not add dependencies,
change application behavior or claim general TOML/INI semantic analysis.

## Decisions

Use `sonar.sources=src,ops,migrations,docker,pyproject.toml,alembic.ini`. SonarScanner CLI
accepts individual files as well as directories. Add all three new entries to coverage
exclusions, preserving src-only coverage even if language support changes later.

Enable `sonar.text.inclusions.activate=true` and `sonar.text.inclusions=**/*` so all text
within the explicitly selected scope is eligible for secret detection. This avoids
copying Sonar's default extension list merely to append TOML/INI. Binary detection
remains at its default. These settings do not widen `sonar.sources` or provide TOML/INI
language rules. Keep rule suppressions absent, including for Dockerfiles now in scope.

Official documentation checked on 2026-10-27:
- [Initial scope](https://docs.sonarsource.com/sonarqube-cloud/managing-your-projects/project-analysis/setting-analysis-scope/setting-initial-scope)
  accepts literal individual file paths.
- [Secrets](https://docs.sonarsource.com/sonarqube-cloud/analyzing-source-code/languages/secrets)
  requires unassigned-language files to match text inclusions for secret analysis.

## Work streams

After these artifacts are complete, A and B can start concurrently on disjoint files:
- A owns `tests/unit/infrastructure/test_sonar_properties.py`: expand contracts and show
  the new scope/coverage/text assertions fail against the old properties.
- B owns `CONTRIBUTING.md`: update scope and explain language-dependent analysis.
- The coordinator owns `sonar-project.properties` and all OpenSpec artifacts. Its
  properties edit waits for A's red check; combined validation waits for both streams.
  Then sync the release-pipeline delta and archive. This small change can be executed
  sequentially without delegating the streams.

## Validation and risks

Run targeted scope tests red then green; run the full unit suite with its coverage gate,
targeted ruff lint/format checks and strict OpenSpec validation. Local checks cannot prove
Cloud analyzer behavior, indexed files or Docker syntax compatibility: verify the next
authenticated scan for both configuration files, Docker analysis and parser warnings.
No application or integration execution is warranted for scanner settings alone.