# Proposal

## Why

The integration suite runs serially on one runner, about nine of the job's ten minutes,
and it is the slow half of every pull request's merge gate. It is serial because every test
shares one database and some test advisory locks across connections, so it cannot simply
run under xdist. Giving each of several runners its own full stack keeps that invariant
(one database, one serial test process per database) and cuts the wall time to roughly a
quarter of the suite plus setup. Issue #67.

## What Changes

- The integration workflow becomes four jobs. A `plan` job detects the changed paths (as
  today), validates the shard count and, when the suite is selected, collects it once to
  learn how many tests the shards must run between them. A matrix `shard` job runs one
  shard per runner, each against its own `make test-stack` stack (PostgreSQL with PostGIS,
  RustFS and rclone, migrated), with `fail-fast: false` so every shard reports. A `boot`
  job builds the application image and boots the API and both workers on its own migrated
  stack, with the schedule check. An aggregate job keeps the required name `Integration
  tests`: it runs on every outcome, fails unless every job it needs succeeded (or was
  skipped because the suite was not selected), checks that the shards together ran every
  collected test, and merges the shards' JUnit reports into the staging-evidence summary.
- The suite is split with `pytest-split` (new dev dependency) by recorded durations, with
  the `least_duration` algorithm: deterministic, evenly timed shards that keep collection
  order inside each shard. The durations live in a committed `.test_durations` file, which
  a new `make test-durations` target regenerates from a full local run. Each shard stays
  serial: no `-n`.
- The shard count is configurable without a commit through the repository variable
  `INTEGRATION_SHARDS`; the workflow defaults it to 4. `make test-integration` accepts
  `GROUP=<n>` (and `SPLITS`, default 4) to rerun one shard locally, so a shard failure can
  be reproduced as CI ran it. A unit test pins the two defaults to each other.
- `tests/unit/infrastructure/test_integration_workflow.py` is reshaped to the new
  topology: required name on the aggregate job, detection in `plan`, the shard matrix and
  its command, the boot job's order, the fail-closed aggregate, the artifact round trip,
  the durations file's shape and the shard-count defaults.
- The workflow header, the `Makefile` and `pyproject.toml` comments that say the suite is
  serial, `CONTRIBUTING.md`, `docs/staging.md` and `checks.yml`'s header say it is serial
  per shard, across isolated stacks, and how to rerun a shard or refresh the durations.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `release-pipeline`: the integration gate is an aggregate check over a planned, sharded
  run. "Integration suite runs whole, against the full stack" becomes a requirement that
  every test runs exactly once across isolated shards and that the gate fails when the
  shards' union is not the collected suite. "Application boots after the suite, on the same
  database" becomes a boot on its own migrated stack, in its own job. The change-detection
  requirements move from "the integration job" to the plan job, with the aggregate failing
  when the plan fails. New requirements: the shard count is configurable with a default of
  four; the split is driven by committed durations; the aggregate fails closed on any
  non-success result.

## Impact

- `.github/workflows/integration.yml`: rewritten as `plan`, `shard` (matrix), `boot` and
  `integration` (the required check), with artifact upload and download for the shard
  reports.
- `pyproject.toml` and `uv.lock`: `pytest-split~=0.11` in the dev group; the xdist comment.
- `.test_durations` (new, committed) and `Makefile`: `test-durations`, `test-integration`
  with `SPLITS`/`GROUP`.
- `tests/unit/infrastructure/test_integration_workflow.py`: reshaped pins.
- `openspec/specs/release-pipeline/spec.md` (on sync).
- Docs and comments: `CONTRIBUTING.md`, `docs/staging.md`, `.github/workflows/checks.yml`.
- Runner cost: one short plan job, four shard runners, one boot runner and one short
  aggregate per selected run, in place of one ten-minute runner. Expected wall time about
  six to seven minutes, dominated by per-runner setup (Tesseract, dependencies, image pulls,
  stack start) rather than the tests.
- The ruleset is unchanged: `Integration tests` stays the required check's name and still
  reports a result on every pull request. No runtime, image or deployment change.
