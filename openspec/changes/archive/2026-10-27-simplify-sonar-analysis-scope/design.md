## Context

The scan action runs SonarScanner-based analysis, which supports `sonar.sources` and
`sonar.tests`. The existing properties instead scan broadly, enumerate exclusions for
each metric and incorrectly infer test classification from pytest. The existing tests
pin that inference and enumerate Git directories.

## Goals / Non-Goals

- Keep src-only unit coverage and src/infrastructure duplication.
- Use a small static configuration that does not change for unrelated new directories.
- Explicitly classify test code and document the loss of broad ordinary analysis.
- Do not add generators, dependencies or CI steps, or change integration coverage.

## Decisions

Use `sonar.sources=src,infrastructure` and `sonar.tests=tests`. The roots are disjoint,
so no test exclusions are necessary. Use `sonar.coverage.exclusions=infrastructure/**`;
no duplication exclusions are needed because both source roots are intended to count.
Keep project identity, Python version and `coverage.xml` unchanged.

Remove global exclusions: docs and OpenSpec are outside the roots. Remove the Docker
rule suppression because Dockerfiles are no longer analysed. Document that migrations,
Dockerfiles, workflows, root files and all other trees no longer receive ordinary Sonar
findings; the maintainer explicitly selected this trade-off over generated exclusions.

Replace Git-directory enumeration and pytest inference with direct configuration
contract assertions. Preserve report-path and identity checks. Fix the stale Sonar
comment in the checks-workflow test. No scanner-pattern reimplementation is necessary:
the configured roots are literal directories, not patterns.

## Work streams

After these planning artifacts are complete, streams A and B may start concurrently:

- A owns `sonar-project.properties` and `CONTRIBUTING.md`.
- B owns `tests/unit/infrastructure/test_sonar_properties.py` and the comment in
  `tests/unit/infrastructure/test_checks_workflow.py`. First run the new scope tests
  against the old properties and record the failure before A updates the config.
- The parent coordinates the red-test gate, runs combined verification, syncs
  `openspec/specs/release-pipeline/spec.md` and archives the change. Shared artifacts
  are edited only by the parent.

## Risks / Trade-offs

Reduced ordinary Sonar coverage is intentional. Existing GitHub secret scanning is not
reconfigured, and this change makes no claim about other scanners' language coverage.
New production roots need an explicit source-scope decision and matching test updates.
Tests verify local settings, not the Cloud project's UI overrides or server metrics;
the next authenticated CI scan is the end-to-end verification.

## Documentation check

Verified against current official Sonar documentation on 2026-10-27:

- [Initial scope](https://docs.sonarsource.com/sonarqube-cloud/managing-your-projects/project-analysis/setting-analysis-scope/setting-initial-scope)
  documents literal, comma-separated source/test roots and explicit CLI configuration.
- [Metric exclusions](https://docs.sonarsource.com/sonarqube-cloud/managing-your-projects/project-analysis/setting-analysis-scope/exclude-from-coverage-duplication)
  still documents exclusion settings, not metric inclusion settings.
- [Python coverage](https://docs.sonarsource.com/sonarqube-cloud/analyzing-source-code/test-coverage/python-test-coverage)
  requires CI-based analysis and `sonar.python.coverage.reportPaths`.
- [Python scanner](https://docs.sonarsource.com/sonarqube-cloud/analyzing-source-code/scanners/sonarscanner-for-python)
  is a separate `pysonar` wrapper. The pinned scan action's
  [manifest](https://github.com/SonarSource/sonarqube-scan-action/blob/d209202bc7d53ff1cc128f7f907dac145c9d6ae9/action.yml)
  selects SonarScanner CLI. New Python-wrapper inference is not relied upon here.

## Validation

Run the new scope tests red, then green with the properties change. Run the entire unit
suite with its normal coverage gate, targeted ruff checks, and OpenSpec validation. No
application or database run is warranted for scanner configuration changes.