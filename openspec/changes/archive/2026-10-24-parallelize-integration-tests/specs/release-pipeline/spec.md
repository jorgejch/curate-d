# Spec Delta

## ADDED Requirements

### Requirement: The shard count is configurable with a default of four
The number of integration shards MUST come from the repository variable
`INTEGRATION_SHARDS` when it is set and MUST default to 4 when it is not. The plan MUST
refuse a value that is not an integer from 1 to 32, failing before any shard starts. The
local shard target MUST default to the same count, and a unit test MUST pin the two
defaults to each other.

#### Scenario: Variable unset
- **WHEN** the repository has no `INTEGRATION_SHARDS` variable
- **THEN** a selected run starts four shards and passes `--splits 4`

#### Scenario: Variable set
- **WHEN** `INTEGRATION_SHARDS` is `6`
- **THEN** the next selected run starts six shards, each with `--splits 6` and a distinct
  group from 1 to 6, without a commit

#### Scenario: Variable invalid
- **WHEN** `INTEGRATION_SHARDS` is `0`, `abc`, `2.5` or `33`
- **THEN** the plan fails naming the variable and the accepted range, no shard or boot
  job starts, and `Integration tests` fails

#### Scenario: Defaults drift apart
- **WHEN** the workflow's default or the Makefile's `SPLITS` default changes and the
  other does not
- **THEN** the unit suite fails naming both values

### Requirement: Shards are split by committed test durations
The split MUST be deterministic for a given commit and shard count, and MUST balance the
shards by recorded test durations kept in a committed `.test_durations` file, which a Make
target regenerates from one whole local run with the same staging backend as CI. A unit
test MUST pin the file's presence and shape. A test without a recorded duration MUST still
run, in exactly one shard.

#### Scenario: Same commit, same split
- **WHEN** a selected run is rerun on the same commit with the same shard count
- **THEN** every shard runs the same tests in the same order as before

#### Scenario: New test without a duration
- **WHEN** a test is added and `.test_durations` has no entry for it
- **THEN** it runs in exactly one shard with an estimated duration, and the run passes

#### Scenario: Durations regenerated
- **WHEN** `make test-durations` runs against a `make test-stack` stack
- **THEN** `.test_durations` holds one positive duration per test the whole suite ran,
  under the node ids CI's shard command produces

#### Scenario: Durations file missing or malformed
- **WHEN** `.test_durations` is absent, is not a JSON object, is empty, holds a key
  outside `tests/integration/`, or holds a value that is not a positive number
- **THEN** the unit suite fails naming the file

#### Scenario: Durations file changes
- **WHEN** a pull request changes `.test_durations`
- **THEN** the integration run is selected

### Requirement: The gate fails closed on an incomplete run
The `Integration tests` check MUST run on every outcome of the jobs it depends on and
MUST pass only when the plan succeeded and either the suite was not selected and every
shard and the boot were skipped, or the suite was selected and every shard and the boot
succeeded. Every other combination MUST fail the check, naming the job and its result.

#### Scenario: Plan fails
- **WHEN** the plan job fails or is cancelled
- **THEN** no shard or boot job runs, and `Integration tests` fails naming the plan

#### Scenario: One shard fails
- **WHEN** one shard fails and the others and the boot succeed
- **THEN** every other shard still runs to completion, and `Integration tests` fails
  naming the shard job

#### Scenario: Boot fails
- **WHEN** every shard succeeds and the boot job fails
- **THEN** `Integration tests` fails naming the boot job

#### Scenario: Shards skipped while selected
- **WHEN** the plan selected the suite and a shard or boot job reports `skipped` or
  `cancelled`
- **THEN** `Integration tests` fails naming the job and its result

#### Scenario: Suite not selected
- **WHEN** the plan did not select the suite and every shard and the boot job were skipped
- **THEN** `Integration tests` writes the skip summary and passes

### Requirement: Shard failures are reproducible locally
A developer MUST be able to run one shard as CI ran it: the same split, group, algorithm
and durations, against a local `make test-stack` stack, by naming the group to the
integration Make target. Without a group the target MUST run the whole suite.

#### Scenario: Rerun a shard
- **WHEN** a developer runs `make test-integration GROUP=3` after `make test-stack`
- **THEN** the tests CI's shard 3 of 4 ran, and only those, run locally in the same order

#### Scenario: Group without a stack
- **WHEN** `make test-integration GROUP=3` runs without a stack
- **THEN** the run fails on the first database connection, as the whole-suite run does

#### Scenario: Whole suite
- **WHEN** a developer runs `make test-integration` with no `GROUP`
- **THEN** the whole suite runs, unsplit

## MODIFIED Requirements

### Requirement: Pull requests into protected branches are the merge gate
A pull request into `dev` or `main` MUST pass the checks job (lint, the in-workflow
database-access audit, type check, the unit suite with its 80% line-coverage floor) and
the `Integration tests` check (the aggregate over the whole `tests/integration` suite run
in shards and the application boot, see below) before it can merge. Both are required
status checks, and the pull request MUST be up to date with its base when the checks run.

#### Scenario: Every check green
- **WHEN** a pull request into `dev` reports the checks job and `Integration tests` as
  successful, and its head includes the current tip of `dev`
- **THEN** the pull request can merge

#### Scenario: Unit coverage below the floor
- **WHEN** the unit suite passes but line coverage from unit tests alone is below 80%
- **THEN** the checks job fails and the pull request cannot merge

#### Scenario: One integration test fails
- **WHEN** any test under `tests/integration` fails or errors, on any shard
- **THEN** that shard fails, `Integration tests` fails, and the pull request cannot merge

#### Scenario: Base moved after the checks ran
- **WHEN** a commit lands on the base branch after the pull request's checks ran
- **THEN** the pull request cannot merge until it is updated and the checks run again

### Requirement: Integration suite runs whole, against the full stack
The integration run MUST run every test under `tests/integration` exactly once, split
across shards that each run serially, in one process, on their own runner against their
own full stack (migrated PostgreSQL, provisioned RustFS, rclone, Tesseract) with RustFS
as the only selected staging backend. The gate MUST fail when the shards' reports do not
together account for every collected test, and the plan MUST fail if a job-wide
lifecycle observation budget is set.

#### Scenario: Suite runs on a pull request
- **WHEN** the integration run is selected
- **THEN** the plan collects the suite once, every shard runs its group of that
  collection against a fresh stack of its own, no test file under `tests/integration` is
  deselected from the union, and the lifecycle rows 30 and 31 skip citing their recorded
  RustFS observation

#### Scenario: Shards together ran the suite
- **WHEN** every shard job succeeded and one report per shard is present
- **THEN** `Integration tests` passes only if the reports' test counts sum to the plan's
  collected count, and otherwise fails naming the counts

#### Scenario: A shard's report is missing
- **WHEN** a shard job ended without producing its report
- **THEN** `Integration tests` fails naming the missing shard, whatever the job results

#### Scenario: Stack fails to come up
- **WHEN** PostgreSQL, RustFS or rclone does not reach healthy within the wait on a shard
- **THEN** that shard fails before any test runs, its logs name the service, and the
  other shards keep running

#### Scenario: Observation budget set in CI
- **WHEN** `CURATE_D_TEST_STAGING_LIFECYCLE_TIMEOUT_SECONDS` is set in the plan's
  environment
- **THEN** the plan fails with an error naming the variable, before any shard starts

#### Scenario: No stack is shared
- **WHEN** two shards run at once
- **THEN** each has its own PostgreSQL, RustFS and rclone containers and volumes on its
  own runner, removed when the shard ends on every outcome, and neither reads the other's
  database, bucket or ports

### Requirement: Application boots on its own stack, beside no test
The API and both worker profiles, built from this commit, MUST start on a migrated
database of their own, in a job that runs no test: a worker writes the persisted
schedules on launch, and their ticks would otherwise run in a test process on the suite's
application version. The job MUST then wait until each is ready: the API until its
readiness route answers, and each worker until it logs `worker.ready`, the event its
startup ends on.

#### Scenario: No scheduled tick during the suite
- **WHEN** a shard runs
- **THEN** no API or worker container runs on that shard's stack, so none of the
  application's declared schedules is persisted to tick on the suite's version

#### Scenario: Boot runs on its own stack
- **WHEN** the integration run is selected
- **THEN** the boot job starts its own stack, applies the migrations, builds the image
  from this commit and starts the API and both workers on that database, beside no test

#### Scenario: Application image fails to boot
- **WHEN** the API or a worker built from this commit exits, the API's health check never
  passes, or a worker does not log `worker.ready` within the wait
- **THEN** the boot job fails, its logs name the service, and `Integration tests` fails

#### Scenario: Worker hangs in startup
- **WHEN** a worker's container keeps running but its startup never finishes
- **THEN** the boot job fails once the readiness wait expires, naming the worker

### Requirement: Booted workers hold the declared schedules
Since a worker logs `worker.ready` even when it left its schedules to another version,
the boot job MUST then check that the image's version is DBOS's latest and that the stored
schedules are exactly the declared ones, each under its declared cron.

#### Scenario: Workers leave their schedules unapplied
- **WHEN** the workers are ready but the image's version is not DBOS's latest, a declared
  schedule is missing, an undeclared one is stored, or a stored one's cron is not its
  declared one
- **THEN** the boot job fails, naming the latest version or the schedules that differ,
  and `Integration tests` fails

### Requirement: The integration job runs the suite only for changes it can observe
A plan job MUST itself determine the pull request's changed files, as a step before any
tool is installed or any service started, and the shards and the application boot MUST
run only when at least one changed file is under a declared path. When none is, no shard
or boot job MUST run, the `Integration tests` check MUST record in its summary that the
suite was not run and why, and MUST pass.

#### Scenario: Application code changes
- **WHEN** a pull request changes a file under `src/`
- **THEN** the plan selects the run, every shard runs its group and the boot job runs

#### Scenario: Only unit tests change
- **WHEN** a pull request changes files under `tests/unit/` and nothing else
- **THEN** the plan installs nothing after the detection, no shard or boot job starts,
  `Integration tests` writes a summary naming the declared paths, and passes

#### Scenario: Mounted infrastructure changes
- **WHEN** a pull request changes a file under `infrastructure/rustfs/` or
  `infrastructure/postgres/`, or `infrastructure/staging-compatibility.md`
- **THEN** the integration run is selected

#### Scenario: Unmounted infrastructure changes
- **WHEN** a pull request changes files under `infrastructure/reset/` and nothing else
- **THEN** no shard runs and `Integration tests` passes

#### Scenario: Image definition or stack definition changes
- **WHEN** a pull request changes `docker/Dockerfile`, `docker/Dockerfile.dockerignore`,
  `.dockerignore`, `compose.yaml`, a Compose override file,
  `Makefile`, `pyproject.toml`, `uv.lock`, `.python-version`, `alembic.ini`,
  `.test_durations`, a file under `migrations/`, or `.github/workflows/integration.yml`
- **THEN** the integration run is selected

#### Scenario: Gate shape changes
- **WHEN** the detection step leaves the plan job, a plan step after it loses its
  condition on the detection's result, a shard or boot job loses its condition on the
  plan's selection, the required name leaves the aggregate job, the aggregate stops
  running on every outcome or stops needing a shard or boot job, or a stack teardown stops
  running on every outcome
- **THEN** the unit suite fails naming the job or step

#### Scenario: A file read implicitly is added
- **WHEN** a pull request adds `conftest.py`, `tests/conftest.py`, a pytest configuration
  file in `tests/` or the root, a Compose override file (`compose.override.yml`,
  `compose.override.yaml`, `docker-compose.override.yml` or `docker-compose.override.yaml`),
  `.env`, a `.gitattributes` at the root or in `tests/`, `infrastructure/` or `docker/`, or
  `docker/Dockerfile.dockerignore`
- **THEN** the integration run is selected

### Requirement: Uncertain change detection fails the integration job
The plan MUST fail when the changed files cannot be determined or may be incomplete, and
`Integration tests` MUST fail with it.

#### Scenario: Change detection fails
- **WHEN** the step that lists the pull request's changed files fails, or reports a
  value other than a match or no match
- **THEN** the plan fails before any later step, naming the detection, no shard or boot
  job starts, `Integration tests` fails, and the pull request cannot merge

#### Scenario: Changed-file list may be truncated
- **WHEN** the detection finds no declared path and the pull request changes 3,000 files
  or more
- **THEN** the plan fails before any later step, `Integration tests` fails, and the pull
  request cannot merge

#### Scenario: A packaging file is removed
- **WHEN** the detection finds no declared path and `README.md`, `LICENSE` or `NOTICE` is
  missing from the checkout
- **THEN** the plan fails before any later step, naming the file, and `Integration tests`
  fails

## RENAMED Requirements

- FROM: `### Requirement: Application boots after the suite, on the same database`
- TO: `### Requirement: Application boots on its own stack, beside no test`
