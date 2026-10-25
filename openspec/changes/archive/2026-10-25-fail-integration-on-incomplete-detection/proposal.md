# Proposal

## Why

The `release-pipeline` requirement "Uncertain change detection fails the integration job" says
the plan MUST fail when the changed files may be incomplete. GitHub lists at most 3,000 of a pull
request's files, so a pull request at that size may carry a declared path the detection never
saw. The plan's guard ("Refuse an undecided detection" in `.github/workflows/integration.yml`)
fails such a pull request only when the detection answered `false`
(`[ "$SUITE" = "false" ] && [ "${CHANGED_FILES:-3000}" -ge 3000 ]`). With `true`, the plan goes
on and the whole suite runs on a file list the requirement calls uncertain. The spec's own
truncation scenario narrows the same way ("finds no declared path and the pull request changes
3,000 files or more"), so spec and workflow agree with each other and disagree with the
requirement they sit under (issue #66).

## What Changes

- The guard fails the plan on a pull request that changes 3,000 files or more, or whose file
  count is missing, whatever the detection reported. It keeps refusing a detection that is
  neither `true` nor `false`, and it keeps the error that names the file count and tells the
  author to split the pull request.
- The packaging-file check (`README.md`, `LICENSE`, `NOTICE` present on a skip) is unchanged: it
  applies to `false` only.
- The truncation scenario is rewritten to hold for either detection result, and a scenario pins
  the `true` case.
- A unit test runs the guard's script with both detection results and the file counts at and
  above the limit, empty and unset, and asserts each fails naming the count, and that a
  pull request under the limit passes.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `release-pipeline`: "Uncertain change detection fails the integration job" fails a pull
  request that may exceed GitHub's file listing whether or not the detection selected the suite.

## Impact

- `.github/workflows/integration.yml`, `tests/unit/infrastructure/test_integration_workflow.py`.
- A pull request with 3,000 or more changed files that selects the suite now fails
  `Integration tests` instead of running the suite. Such a pull request must be split; none is
  open today.
