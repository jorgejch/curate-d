# release-pipeline Specification

## Purpose

Defines what continuous integration runs for each kind of Git event, what gates a merge
into a protected branch, and when and from where a container image is published.

## Requirements

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

### Requirement: Further gates leave the two required jobs in place
The integration job MAY pass without running the suite only when the pull request changes
none of the paths the integration suite depends on (see "The integration job runs the
suite only for changes it can observe"). Further ruleset rules (code-scanning results from
CodeQL or SonarQube) MAY gate the merge, and MUST NOT loosen the merge gate above.

#### Scenario: Documentation-only pull request
- **WHEN** a pull request into `dev` changes only files outside the integration suite's
  declared paths, such as `docs/` and `README.md`, and the checks job is successful
- **THEN** the integration job passes without running the suite and the pull request can
  merge

#### Scenario: Code scanning added to the ruleset
- **WHEN** a code-scanning rule is added to the protected branch's ruleset
- **THEN** the checks job and the integration job remain required status checks

### Requirement: Feature-branch pushes run the checks without gating
A push to any branch other than `dev` or `main` MUST run lint, the in-workflow
database-access audit, the type check and the unit suite, and MUST upload the unit coverage to SonarQube Cloud as an analysis of that
branch. A failure is reported on the commit and blocks nothing.

#### Scenario: Push to a feature branch
- **WHEN** a commit is pushed to a feature branch
- **THEN** lint, the database-access audit, type check and the unit suite run, and
  SonarQube Cloud shows an analysis
  of that branch with the run's unit coverage

#### Scenario: Lint fails on a feature branch
- **WHEN** ruff reports a violation on a feature-branch push
- **THEN** the checks job fails on that commit and no SonarQube analysis is uploaded for it

#### Scenario: Newer push to the same branch
- **WHEN** a second commit is pushed to the branch while the first's run is in progress
- **THEN** the first run is cancelled and only the second completes

### Requirement: Protected-branch pushes refresh SonarQube coverage
A push to `dev` or `main` MUST run the unit suite and upload its coverage to SonarQube
Cloud as an analysis of that branch. Lint, the database-access audit, the type check and
the integration suite MUST NOT run on that push: the merged pull request already ran them. The job gates
nothing and no publish step waits on it.

#### Scenario: Merge into dev
- **WHEN** a pull request merges into `dev`
- **THEN** SonarQube Cloud's `dev` branch shows the merge commit's unit coverage, and the
  dev image build does not wait for the analysis

#### Scenario: Analysis upload fails
- **WHEN** the SonarQube upload fails on a `dev` push
- **THEN** the job fails on that commit, the dev image is still built and pushed, and
  SonarQube Cloud keeps its previous `dev` analysis

### Requirement: Only unit coverage reaches SonarQube
The coverage report uploaded to SonarQube Cloud MUST come from the unit suite alone. An
integration run MUST NOT write a coverage report that an upload could pick up.

#### Scenario: Integration run on a pull request
- **WHEN** the integration job runs the suite
- **THEN** it runs without coverage collection and produces no coverage report

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
- **WHEN** a pull request changes a file under `ops/rustfs/` or
  `ops/postgres/`, or `ops/staging-compatibility.md`
- **THEN** the integration run is selected

#### Scenario: Unmounted infrastructure changes
- **WHEN** a pull request changes files under `ops/reset/` and nothing else
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
  `.env`, a `.gitattributes` at the root or in `tests/`, `ops/` or `docker/`, or
  `docker/Dockerfile.dockerignore`
- **THEN** the integration run is selected

### Requirement: Uncertain change detection fails the integration job
The plan MUST fail when the changed files cannot be determined or may be incomplete, and
`Integration tests` MUST fail with it. A pull request that changes 3,000 files or more, or
whose changed-file count is missing, MUST fail the plan whether the detection selected the
suite or not.

#### Scenario: Change detection fails
- **WHEN** the step that lists the pull request's changed files fails, or reports a
  value other than a match or no match
- **THEN** the plan fails before any later step, naming the detection, no shard or boot
  job starts, `Integration tests` fails, and the pull request cannot merge

#### Scenario: Changed-file list may be truncated
- **WHEN** the pull request changes 3,000 files or more, or its changed-file count is
  missing, whether or not the detection finds a declared path
- **THEN** the plan fails before any later step, naming the file count and telling the
  author to split the pull request, no shard or boot job starts, `Integration tests` fails,
  and the pull request cannot merge

#### Scenario: Truncated list selects the suite
- **WHEN** the detection finds a declared path and the pull request changes 3,000 files or
  more
- **THEN** the plan fails before any later step, the suite does not run, and
  `Integration tests` fails

#### Scenario: A packaging file is removed
- **WHEN** the detection finds no declared path and `README.md`, `LICENSE` or `NOTICE` is
  missing from the checkout
- **THEN** the plan fails before any later step, naming the file, and `Integration tests`
  fails

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

### Requirement: The declared paths cover everything the suite depends on
The declared paths MUST cover every file the suite imports, builds, mounts, boots or
reads, including files a tool loads implicitly once they are added. A unit test MUST pin
the set against the stack definition and the tests' own reads.

#### Scenario: Declared set matches the stack
- **WHEN** every path `compose.yaml` bind-mounts, every path `docker/Dockerfile` copies
  other than `README.md`, `LICENSE` and `NOTICE`, and every repository path an
  integration test reads is under a declared path
- **THEN** the unit suite passes

#### Scenario: Suite picks up an undeclared path
- **WHEN** `compose.yaml` bind-mounts a repository path, `docker/Dockerfile` copies one
  other than `README.md`, `LICENSE` or `NOTICE`, or an integration test reads one by
  path, and no declared path covers it
- **THEN** the unit suite fails naming the uncovered path

### Requirement: A dev image is published on every push to dev
A push to `dev` MUST build the container image from that commit and push it to the
registry with a tag of the form `dev-<UTC timestamp>-<sha>`. The build MUST NOT wait on
any test job. No other branch push MUST publish an image.

#### Scenario: Push to dev
- **WHEN** a commit lands on `dev`
- **THEN** one image tagged `dev-<timestamp>-<sha>` is pushed, with the tag as its build
  revision

#### Scenario: Push to main without a tag
- **WHEN** a commit lands on `main` and no tag is pushed
- **THEN** no image is built or pushed

#### Scenario: Pull request
- **WHEN** a pull request is opened or updated
- **THEN** no image is pushed to the registry

### Requirement: A production image is published only for a semver tag on main
A pushed tag matching `vMAJOR.MINOR.PATCH` MUST build and push the image with the
semver tags only when the tagged commit is an ancestor of `main` and the tag equals the
version in `pyproject.toml`. Either check failing MUST fail the run before any build.

#### Scenario: Release tag on a main commit
- **WHEN** `v1.4.0` is pushed, its commit is on `main`, and `pyproject.toml` says `1.4.0`
- **THEN** images tagged `v1.4.0` and `v1.4` are pushed

#### Scenario: Tag on an unmerged commit
- **WHEN** a semver tag points at a commit that is not an ancestor of `main`
- **THEN** the run fails naming the tag, and nothing is built

#### Scenario: Tag disagrees with the packaged version
- **WHEN** the tag is `v1.4.0` and `pyproject.toml` says `1.3.2`
- **THEN** the run fails naming both versions, and nothing is built

#### Scenario: Non-semver tag
- **WHEN** a tag such as `v1`, `vfoo` or `v1.2.3-rc1` is pushed
- **THEN** no workflow runs for it

### Requirement: Required check names are pinned
The job names the ruleset requires MUST be asserted by a unit test, so a rename fails the
build instead of leaving every pull request waiting on a check that never reports.

#### Scenario: Required job renamed
- **WHEN** a required job's `name` changes in a workflow file
- **THEN** the unit suite fails naming the expected context

### Requirement: The SonarQube scan skips only fork pull requests
The checks job MUST skip the SonarQube scan for a pull request from a fork, which receives
no `SONAR_TOKEN`. A run Dependabot triggers MUST run the scan with Dependabot's own
`SONAR_TOKEN` secret. Every other run of the checks job that wrote a coverage report MUST
run the scan, and MUST fail when its token is missing rather than skip the analysis.

#### Scenario: Pull request from a fork
- **WHEN** the checks job runs on a pull request whose head repository is a fork
- **THEN** the unit suite runs and the scan step is skipped

#### Scenario: Dependabot pull request
- **WHEN** the checks job runs on a pull request Dependabot opened in this repository
- **THEN** the scan runs with Dependabot's `SONAR_TOKEN` and SonarQube Cloud shows an
  analysis of the pull request

#### Scenario: Dependabot secret missing
- **WHEN** the checks job runs for Dependabot and the repository has no Dependabot secret
  named `SONAR_TOKEN`
- **THEN** the scan step runs and fails, and the job fails

#### Scenario: Token missing on an in-repository run
- **WHEN** the checks job runs on a branch of this repository, not triggered by
  Dependabot, and `SONAR_TOKEN` is unset
- **THEN** the scan step runs and fails, and the job fails

### Requirement: SonarQube measures production code only
SonarQube Cloud MUST count coverage on `src/` only. All selected source paths MUST remain
eligible for duplication measurement where their language analyzer supports it, without
duplication exclusions. It MUST explicitly classify `tests/` as test code, independently
of pytest configuration, so tests count toward neither metric. Coverage MUST import
only `coverage.xml` from the unit suite. A scope or report-path drift MUST fail the unit
suite naming the setting.

#### Scenario: Uncovered helper in infrastructure
- **WHEN** a script under `ops/` has no unit test
- **THEN** SonarQube's coverage metric is unchanged, and a block it copies from `src/` is
  reported as duplication

#### Scenario: Duplicated test fixture
- **WHEN** two test modules under `tests/` share a copied block
- **THEN** SonarQube classifies them as tests and reports neither coverage nor duplication
  for them

#### Scenario: Metric setting drifts
- **WHEN** coverage exclusions no longer name exactly `ops/**`, `migrations/**`,
  `docker/**`, `pyproject.toml`, `alembic.ini` and `compose.yaml`, duplication exclusions are added,
  or the coverage report path changes
- **THEN** the unit suite fails naming the setting

#### Scenario: New top-level directory left unlisted
- **WHEN** a top-level directory is added outside the selected source and test roots
- **THEN** its files remain outside analysis and both metrics without adding exclusions

#### Scenario: Uncovered migration
- **WHEN** a migration has no unit coverage or copies a block from another source root
- **THEN** it does not lower coverage, but remains eligible for duplication findings

#### Scenario: Image and configuration coverage
- **WHEN** `docker/`, `pyproject.toml`, `alembic.ini` or `compose.yaml` has no unit coverage
- **THEN** it does not lower application coverage

### Requirement: SonarQube analyses explicitly selected source and test trees
SonarQube Cloud MUST use `src/`, `ops/`, `migrations/`, `docker/`, `pyproject.toml`,
`alembic.ini` and `compose.yaml` as its only sources, and `tests/` as tests. Scopes MUST be disjoint,
independent of pytest and free of filters but one, excluding `docs/diagrams/`. Scoped text and
`docs/` MUST get secret detection, `docs/` nothing else. Scope or text-setting drift MUST fail unit tests naming the setting.
Contributor docs MUST explain scope maintenance, metrics and language support.

#### Scenario: Existing non-source paths
- **WHEN** a workflow, unlisted root file or OpenSpec artifact changes
- **THEN** SonarQube does not analyse it because it is outside the selected paths

#### Scenario: Document changes
- **WHEN** a file under `docs/`, outside `docs/diagrams/`, changes
- **THEN** SonarQube scans it for secrets only, and it receives no other issue, coverage or
  duplication measurement

#### Scenario: Generated diagram changes
- **WHEN** a file under `docs/diagrams/` changes
- **THEN** SonarQube does not analyse it, secrets included, because `sonar.exclusions` excludes it

#### Scenario: Analysis exclusions drift
- **WHEN** `sonar.exclusions` lists anything but `docs/diagrams/**`, or another source or test
  filter is set
- **THEN** the unit suite fails naming the setting

#### Scenario: New top-level directory
- **WHEN** a directory outside the selected paths is added
- **THEN** it receives no Sonar analysis, coverage or duplication measurement, without a
  new exclusion or test update

#### Scenario: Source and test roots drift
- **WHEN** a source or test path changes or a source path overlaps the test tree
- **THEN** the unit suite fails naming the setting

#### Scenario: Migration changes
- **WHEN** a Python migration changes
- **THEN** SonarQube analyses it as source code

#### Scenario: Image definition changes
- **WHEN** `docker/Dockerfile` changes
- **THEN** it is in source scope for Docker analysis without blanket rule suppressions

#### Scenario: Project and migration configuration changes
- **WHEN** `pyproject.toml` or `alembic.ini` changes
- **THEN** the file is in source scope and eligible for text/secret detection without
  assigning a false language or claiming general TOML/INI language rules

#### Scenario: Compose definition changes
- **WHEN** `compose.yaml` changes
- **THEN** it is in source scope and eligible for text/secret detection without claiming
  Dockerfile-specific rules apply to Compose

#### Scenario: Secret detection configuration drifts
- **WHEN** text inclusions no longer cover all scoped text files and `docs/`, add any other
  path, or are deactivated
- **THEN** the unit suite fails naming the setting

### Requirement: CI checkouts keep the token off disk
The checks job, the coverage-report job and the integration job MUST check the repository
out without persisting the workflow token in the working copy's Git configuration, so code
under test cannot read it from disk.

#### Scenario: Pull request code reads the Git config
- **WHEN** a test in a pull request reads `.git/config` during the checks or integration
  job
- **THEN** it finds no credential for the repository's remote

#### Scenario: Checkout step loses the setting
- **WHEN** one of those jobs' checkout step no longer sets `persist-credentials: false`
- **THEN** the unit suite fails naming the job

### Requirement: CI and developers start the integration stack with one target
The integration job MUST start the suite's services and apply the migrations through the
same `make test-stack` target developers run, with flags that build nothing and install
only locked dependencies. The target MUST first destroy the local stack, its data and its
persisted schedules, then start PostgreSQL, RustFS and rclone, provision RustFS and
migrate; a failing step MUST stop it before the next.

#### Scenario: Local suite after a stack-up
- **WHEN** a developer who ran `make stack-up` runs `make test-stack`
- **THEN** the API, the workers and their persisted schedules are gone before PostgreSQL,
  RustFS and rclone start, so no scheduled tick runs beside the suite

#### Scenario: A stack service fails to start
- **WHEN** PostgreSQL, RustFS or rclone does not reach healthy under `make test-stack`
- **THEN** the target fails and no migration runs

#### Scenario: Application launch after a suite
- **WHEN** the suite has run, leaving its own application version as DBOS's latest, and an
  worker image then launches on the same database
- **THEN** the worker registers its own version as latest, and the suite promotes no earlier
  version back

### Requirement: Dependency version updates are proposed against dev
The repository MUST hold a Dependabot configuration that checks `pyproject.toml` with
`uv.lock` (ecosystem `uv`, directory `/`), the workflow files (`github-actions`, `/`) and
`docker/Dockerfile` (`docker`, `/docker`) for version updates once a week, on Friday late
afternoon America/Los_Angeles, and opens every resulting pull request against `dev`. It MUST
NOT configure Compose images. A unit test MUST pin the ecosystems, the target branch and
the schedule.

#### Scenario: Weekly run
- **WHEN** Dependabot's scheduled run finds an outdated dependency in any of the three
  ecosystems
- **THEN** it opens a pull request whose base is `dev`, which the merge gate then checks
  like any other pull request

#### Scenario: Security update
- **WHEN** GitHub raises a Dependabot security update
- **THEN** it opens against the default branch, `dev`; the version-update groups and schedule do
  not apply to it

#### Scenario: Target branch dropped
- **WHEN** an ecosystem entry loses `target-branch: dev`, an ecosystem is removed, a
  Compose ecosystem is added, or the schedule leaves weekly Friday Pacific
- **THEN** the unit suite fails naming the entry

### Requirement: Patch updates are grouped, minors and majors are not
Within version updates, the `uv` ecosystem MUST group patch updates into one pull request
and open a separate pull request per minor and per major update; the `github-actions`
ecosystem MUST group patch and minor updates into one pull request and open one per
major; the `docker` ecosystem MUST group patch updates into one and open one per minor and
per major. A digest-only image refresh never joins a group.
A unit test MUST pin each group's ecosystem and update types.

#### Scenario: Several Python patches
- **WHEN** three packages in `pyproject.toml` have a new patch release and one has a new
  minor
- **THEN** one pull request raises the three patches together and a second raises the
  minor alone

#### Scenario: Major update
- **WHEN** a dependency in any ecosystem has a new major release
- **THEN** it gets a pull request of its own, grouped with nothing

#### Scenario: Image digest refreshed under the same tag
- **WHEN** the registry publishes a new digest for a `FROM` image's current tag
- **THEN** either a pull request updating only that digest opens on its own, outside the
  patch group, or, where GitHub suppresses digest-only refreshes for versioned tags, no
  pull request opens

#### Scenario: Group loosened
- **WHEN** a group's `update-types` gains `major`, or the `uv` patch group gains `minor`
- **THEN** the unit suite fails naming the group

### Requirement: aioboto3 minors wait for backend re-verification
The `uv` ecosystem MUST ignore `version-update:semver-minor` for `aioboto3` and MUST NOT
ignore its patch updates, because a new minor must be re-verified against RustFS and OVH
before it is adopted. A unit test MUST pin the ignore.

#### Scenario: aioboto3 patch
- **WHEN** `aioboto3` publishes a patch within the pinned minor
- **THEN** it is proposed in the patch group like any other patch

#### Scenario: aioboto3 minor
- **WHEN** `aioboto3` publishes a new minor
- **THEN** no version-update pull request is opened for it

#### Scenario: Ignore removed or widened
- **WHEN** the `aioboto3` ignore entry is removed, or it gains `semver-patch`
- **THEN** the unit suite fails naming `aioboto3`

### Requirement: The python image stays on the targeted minor
The `docker` ecosystem MUST ignore `version-update:semver-minor` and
`version-update:semver-major` for the `python` image and MUST NOT ignore its patch
updates, so the image follows the Python minor the repository declares elsewhere
(`.python-version`, `requires-python`) rather than moving on its own. A unit test MUST
pin the ignore.

#### Scenario: Python patch release
- **WHEN** `python:3.12.<n+1>-slim-trixie` is published
- **THEN** a pull request moves both `python` stages to it

#### Scenario: Python minor release
- **WHEN** `python:3.13.<n>-slim-trixie` is current and the repository still declares 3.12
- **THEN** no version-update pull request moves the Dockerfile to it

#### Scenario: Ignore removed
- **WHEN** the `python` ignore entry loses `semver-minor` or `semver-major`
- **THEN** the unit suite fails naming `python`

### Requirement: Dockerfile base images are literal and digest-pinned
Every `FROM` line in `docker/Dockerfile` MUST name its image as a literal
`<name>:<tag>@sha256:<64 hex>`, with no `${...}` or `$VAR` interpolation in the image
reference, so Dependabot can parse and update it, and the build is reproducible down to
the image content. A unit test MUST pin the shape.

#### Scenario: Image updated by Dependabot
- **WHEN** Dependabot bumps a `FROM` image
- **THEN** the pull request rewrites the tag and the digest on that line, and the build
  needs no build argument to select the version

#### Scenario: Interpolated or unpinned FROM reintroduced
- **WHEN** a `FROM` line references `${PYTHON_VERSION}`, omits the tag, or omits the
  `@sha256:` digest
- **THEN** the unit suite fails naming the line

### Requirement: A repository auto-apply policy bounds Gitar's code-review fixes
`.gitar/config/apply.md` on `dev` MUST state which code-review findings Gitar's auto-apply may
fix in this repository and which need human review. Eligibility MUST rest on evidence the fixer
can read (the spec requirement, the callers, the tests), never on a test run. A fix that changes
a protected data-integrity or durability guarantee MUST NOT be applied, and its finding MUST stay
open for human review.

#### Scenario: Local defect verified by inspection
- **WHEN** a finding reports a missing edge-case guard, and the spec requirement, the callers and
  the existing tests establish the correct result
- **THEN** the policy allows a focused fix with a regression test, provided the organization
  criteria allow it too

#### Scenario: Patch changes a protected guarantee
- **WHEN** a candidate patch moves a ledger write out of a `runtime/datasource.py` transaction,
  reorders copy, verification and registration, or replaces a deterministic workflow ID
- **THEN** the patch is not applied and the finding stays open for human review

### Requirement: An uncertain fix leaves its finding open
When a fix's validity, intended behaviour, scope or compliance with either the organization's or
the repository's criteria cannot be established, the policy MUST direct that no patch is applied
and the finding stays open, naming the concrete uncertainty and the evidence or decision that
would resolve it. It MUST NOT be guessed at or suppressed.

#### Scenario: Correctness cannot be established
- **WHEN** a finding's correctness depends on behaviour no code, test or spec in the repository
  establishes, such as a race against a database or provider
- **THEN** no patch is applied and the finding stays open, naming the uncertainty and what would
  resolve it

#### Scenario: Risk assessment unavailable
- **WHEN** a condition of the policy depends on a pull request's risk and no risk assessment is
  available
- **THEN** the condition is not met

### Requirement: The repository policy widens nothing the organization excludes
A fix MUST satisfy both the organization's auto-apply criteria and the repository policy. The
policy MUST NOT make eligible anything the organization criteria exclude; a wider eligibility
MUST be a separate organization-policy decision, never implied by the file.

#### Scenario: Organization exclusion
- **WHEN** a fix would change a migration, a CI workflow or a dependency version
- **THEN** it is not applied, whatever the repository policy says

### Requirement: The policy states its own reach
The policy MUST state that Gitar reads it from `dev`, that saving or merging it enables neither
auto-apply, approval nor merging, and that CI fixes, rule actions and explicit `gitar fix`
requests are outside its scope.

#### Scenario: Policy changed in a pull request
- **WHEN** a pull request edits `.gitar/config/apply.md`
- **THEN** that pull request's fixes are judged by the policy on `dev`, not by its own edit

#### Scenario: Policy merged
- **WHEN** the policy merges into `dev` and no pull request has auto-apply turned on
- **THEN** Gitar applies no fix on any pull request

### Requirement: The Claude workflow grants tools by an explicit allow-list
The Claude workflow MUST name every tool Claude may use beyond the action's defaults, and Claude
MUST be refused any tool it does not name. The list MUST NOT contain unrestricted shell access
(`Bash`, `Bash(*)`), `gh api`, or `gh pr merge`.

#### Scenario: Tool not on the list
- **WHEN** Claude, running from the workflow, attempts a shell command no allow-list entry
  matches
- **THEN** the command is refused and Claude reports that it could not run it

#### Scenario: A blanket entry is added
- **WHEN** the workflow's allow-list gains unrestricted `Bash`, a `gh api` entry or a
  `gh pr merge` entry
- **THEN** the unit suite fails naming the entry

### Requirement: The Claude workflow can run the repository's checks
The Claude workflow MUST install the locked dependencies as the checks job does and allow
`make lint`, `make typecheck`, `make test` and `make format`, so Claude can run the checks a
pull request must pass before it pushes. When a check fails or can't run, Claude MUST say so
rather than report the change as passing.

#### Scenario: Claude prepares a commit
- **WHEN** `@claude` is asked to change code and push it
- **THEN** it can run lint, the type check and the unit suite in the job, and reports their
  outcome

#### Scenario: Dependency install changes
- **WHEN** the workflow's dependency install no longer runs `uv sync --locked --no-build`
- **THEN** the unit suite fails

### Requirement: The Claude workflow's MCP servers are read-only
Every MCP server the Claude workflow configures MUST be read-only: SonarQube's server MUST run
in its read-only mode from an image pinned by digest, and only read tools of each server MAY be
allowed. A server whose secret is unset MUST be unavailable for that run without failing it.

#### Scenario: SonarQube write tool
- **WHEN** Claude attempts to change a SonarQube issue's or hotspot's status
- **THEN** the tool is not allowed, and the server refuses writes in read-only mode

#### Scenario: Logfire secret missing
- **WHEN** the Logfire secret is not set and `@claude` is mentioned
- **THEN** the run proceeds without the Logfire tools

#### Scenario: Read-only mode dropped
- **WHEN** the SonarQube server's read-only setting is removed, its image loses the digest, or a
  non-read tool of either server is allowed
- **THEN** the unit suite fails

### Requirement: A repository auto-approve policy admits only low-risk changes
`.gitar/config/approve.md` on `dev` MUST let Gitar approve only a pull request into `dev` whose
every changed file is documentation prose, a comment- or docstring-only code change, or an added
unit test, or that is an eligible Dependabot update. Any other change, an unavailable or above-low
risk assessment, or a criterion Gitar can't establish MUST leave the pull request unapproved.

#### Scenario: Documentation typo
- **WHEN** a human's pull request into `dev` corrects a typo in `docs/staging.md`, Gitar's review
  has no blocking finding and the risk assessment is low
- **THEN** the policy allows approval, provided the organization criteria allow it too

#### Scenario: One code change among docs
- **WHEN** a pull request changes `README.md` and one executable line under `src/`
- **THEN** it is not approved

#### Scenario: Risk not assessed
- **WHEN** no risk assessment is available for the pull request
- **THEN** it is not approved

### Requirement: Bot-written pull requests are auto-approved only as eligible Dependabot updates
The auto-approve policy MUST refuse a pull request opened by a bot or app account, or carrying a
commit authored or committed by one, so a bot's change merges only after a human reads it. The one
exception is an eligible Dependabot update. When an author's or committer's identity can't
be established, the pull request MUST NOT be approved.

#### Scenario: Pull request opened by @claude
- **WHEN** `claude[bot]` opens a documentation-only pull request into `dev`
- **THEN** it is not approved

#### Scenario: @claude pushes to a human's pull request
- **WHEN** a human's eligible pull request gains a commit authored by `claude[bot]`
- **THEN** it is not approved

#### Scenario: Dependabot update
- **WHEN** `dependabot[bot]` opens a pull request that is not an eligible Dependabot update
- **THEN** it is not approved

### Requirement: Only Python patches and minors and image patches from Dependabot are auto-approved
The auto-approve policy MUST admit a Dependabot version or security update only when Dependabot
wrote every commit and every dependency it names is a `uv` patch, a `uv` minor whose old and new
major are not `0`, or a `docker` patch, judged from the old and new versions in the diff. Any other
update, any `github-actions` or `dbos` update, or a version Gitar can't compare MUST leave it
unapproved, with the condition named.

#### Scenario: Grouped Python patches
- **WHEN** `dependabot[bot]` opens the `python-patches` group pull request into `dev`, whose commit
  metadata gives `version-update:semver-patch` for every dependency, and the review has no blocking
  finding and the risk assessment is low
- **THEN** the policy allows approval, provided the organization criteria allow it too

#### Scenario: GitHub Actions patch
- **WHEN** Dependabot's `actions-patches-and-minors` pull request moves an action from `v8.3.0` to
  `v8.3.1`, with no blocking finding and a low risk assessment
- **THEN** it is not approved, because a new action release runs in CI with the repository's
  secrets

#### Scenario: Python minor outside the declared range
- **WHEN** Dependabot opens a pull request whose metadata gives `version-update:semver-minor` for
  a `uv` dependency, its diff rewrites that dependency's specifier in `pyproject.toml` and changes
  `uv.lock`, with no blocking finding and a low risk assessment
- **THEN** the policy allows approval, provided the organization criteria allow it too, and the
  required checks are the only test of compatibility

#### Scenario: Minor of a 0.x package
- **WHEN** Dependabot opens a pull request that moves `fastapi` from `0.142.2` to `0.162.0`, typed
  `version-update:semver-minor`
- **THEN** it is not approved, and Gitar names the `0.x` version

#### Scenario: Patch of a 0.x package
- **WHEN** a `python-patches` pull request moves `fastapi` from `0.142.2` to `0.142.3`, with every
  other condition met
- **THEN** the policy allows approval, provided the organization criteria allow it too

#### Scenario: Image minor
- **WHEN** Dependabot opens a pull request whose metadata gives `version-update:semver-minor` for
  a `docker` image
- **THEN** it is not approved, and Gitar names the update type

#### Scenario: Major update
- **WHEN** any Dependabot pull request names a `version-update:semver-major` update
- **THEN** it is not approved

#### Scenario: Digest-only image refresh
- **WHEN** Dependabot changes a `FROM` image's digest under the same tag
- **THEN** it is not approved

#### Scenario: Security update within the limits
- **WHEN** Dependabot opens a security update whose metadata gives no `update-type`, and its
  `uv.lock` diff moves `pypdf` from `6.21.0` to `6.21.1`, with no blocking finding and a low risk
  assessment
- **THEN** the policy allows approval, provided the organization criteria allow it too

#### Scenario: Security update across a major
- **WHEN** a Dependabot security update moves `pillow` from `12.3.0` to `13.0.0`
- **THEN** it is not approved, and Gitar names the major

#### Scenario: DBOS update
- **WHEN** a Dependabot pull request includes a `dbos` patch or minor update
- **THEN** it is not approved, because a `dbos` bump must refresh the vendored skill in the same
  change

#### Scenario: Human commit on a Dependabot pull request
- **WHEN** an eligible Dependabot pull request gains a commit authored by any other account
- **THEN** it is not approved

### Requirement: Dependabot updates that dependabot.yml ignores are not auto-approved
The auto-approve policy MUST leave unapproved a Dependabot update, version or security, that
names a dependency update an `ignore` rule in `.github/dependabot.yml` excludes from version
updates, with the ignored update named. A security update MUST NOT get a path to auto-approval
that a version update of the same dependency does not have.

#### Scenario: Security update that dependabot.yml ignores
- **WHEN** a Dependabot security update moves `aioboto3` from `15.5.0` to `15.6.0`, a minor that
  `.github/dependabot.yml` ignores for version updates
- **THEN** it is not approved, because a new `aioboto3` minor must first be re-verified against
  RustFS and OVH

### Requirement: An auto-approved Dependabot update changes only version references
An eligible Dependabot pull request MUST change only the version references of the dependencies its
metadata names: `uv.lock` and those dependencies' specifiers in `pyproject.toml`, or the tag and
digest of `FROM` lines in `docker/Dockerfile`. Any other change, or a lock file that adds or
removes a package, MUST leave it unapproved, with the change named.

#### Scenario: Lock file adds a package
- **WHEN** a `uv` pull request's `uv.lock` diff adds a package that was not locked before
- **THEN** it is not approved, and Gitar names the added package

#### Scenario: Dockerfile change beyond a FROM line
- **WHEN** an `image-patches` pull request changes a `docker/Dockerfile` line other than a `FROM`
  image's tag and digest
- **THEN** it is not approved

#### Scenario: Large grouped update
- **WHEN** a grouped patch pull request changes more than 200 lines of `uv.lock`, and every changed
  line belongs to a named dependency's version, source or hashes
- **THEN** its size does not refuse it

### Requirement: The auto-approve policy cannot authorize itself
The policy MUST refuse any pull request that changes `.gitar/`, and MUST state that it enables
neither approval nor merging, that it narrows the organization criteria and never widens them,
and that approval leaves every required check in place.

#### Scenario: Pull request edits the criteria
- **WHEN** a pull request changes `.gitar/config/approve.md`
- **THEN** it is not approved, whichever copy of the criteria Gitar read

#### Scenario: Policy merged
- **WHEN** the policy merges into `dev` and auto-approve is off in Gitar's settings
- **THEN** Gitar approves no pull request

### Requirement: Dependabot pull requests are not handed to Gitar's auto-apply
The Dependabot configuration MUST NOT give any entry the `gitar-managed` label, so no Dependabot
version or security update opens with Gitar's auto-apply on. A Gitar fix commit would end the
update's eligibility for auto-approval, and its fix would be a code change that needs its own
reviewed pull request. If a Dependabot pull request gets the label by other means, the
auto-approve policy MUST still judge it by its commits alone.

#### Scenario: Version update opens
- **WHEN** Dependabot opens a version update for any of the three ecosystems
- **THEN** the pull request carries Dependabot's default labels and not `gitar-managed`

#### Scenario: Security update opens
- **WHEN** Dependabot opens a security update
- **THEN** the pull request does not carry `gitar-managed`

#### Scenario: Gitar commit on a labelled Dependabot pull request
- **WHEN** someone adds `gitar-managed` to a Dependabot pull request and Gitar pushes a fix to it
- **THEN** the auto-approve policy refuses it, because a commit is not authored by
  `dependabot[bot]`

### Requirement: The owner's pull requests open with Gitar's auto-apply on
A pull request that the repository owner opens from a branch in this repository MUST get the
`gitar-managed` label when it is opened, whether or not its commits carry a bot's
`Co-Authored-By` trailer. A pull request by any other author, or from a fork, MUST NOT get it.
If adding the label fails, the labelling run MUST fail visibly, MUST NOT block the merge, and the
pull request stays without the label until someone adds it by hand.

#### Scenario: Owner opens a pull request
- **WHEN** the repository owner opens a pull request into `dev` from a branch in this repository
- **THEN** it carries `gitar-managed` within the labelling run, and Gitar's auto-apply is on for
  it

#### Scenario: Commits co-authored by a bot
- **WHEN** the owner opens a pull request whose commits carry `Co-Authored-By: Claude`
- **THEN** it gets `gitar-managed` like any other pull request the owner opens

#### Scenario: Another author
- **WHEN** `dependabot[bot]`, `claude[bot]` or another account opens a pull request
- **THEN** the labelling run adds no label

#### Scenario: Pull request from a fork
- **WHEN** a pull request's head branch is in a fork
- **THEN** the labelling run adds no label

#### Scenario: Labelling fails
- **WHEN** the label cannot be added, for example because the token lacks permission or the
  GitHub API returns an error
- **THEN** the labelling run fails with the API's error, the required checks are unaffected, and
  the pull request has no `gitar-managed` label
