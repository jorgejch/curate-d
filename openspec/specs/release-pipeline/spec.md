# release-pipeline Specification

## Purpose

Defines what continuous integration runs for each kind of Git event, what gates a merge
into a protected branch, and when and from where a container image is published.

## Requirements

### Requirement: Pull requests into protected branches are the merge gate
A pull request into `dev` or `main` MUST pass the checks job (lint, the in-workflow
database-access audit, type check, the unit suite with its 80% line-coverage floor) and
the integration job (the whole
`tests/integration` suite, see below) before it can merge. Both are
required status checks, and the pull request MUST be up to date with its base when the
checks run. Further ruleset rules (code-scanning results from CodeQL or SonarQube) MAY
gate the merge without changing this requirement.

#### Scenario: Every check green
- **WHEN** a pull request into `dev` reports the checks job and the integration job as
  successful, and its head includes the current tip of `dev`
- **THEN** the pull request can merge

#### Scenario: Unit coverage below the floor
- **WHEN** the unit suite passes but line coverage from unit tests alone is below 80%
- **THEN** the checks job fails and the pull request cannot merge

#### Scenario: One integration test fails
- **WHEN** any test under `tests/integration` fails or errors
- **THEN** the integration job fails and the pull request cannot merge

#### Scenario: Base moved after the checks ran
- **WHEN** a commit lands on the base branch after the pull request's checks ran
- **THEN** the pull request cannot merge until it is updated and the checks run again

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
The integration job MUST run every test under `tests/integration` against a stack
holding PostgreSQL with the applied migrations, RustFS with the staging provisioning and
the rclone daemon, with Tesseract available to the test process, and with RustFS as the
only selected staging backend. The API and both worker profiles, built from this commit,
MUST start on the same database only after the suite has finished: a worker writes the
persisted schedules on launch, and their ticks would otherwise run in the test process on
the suite's application version. The job MUST fail if a job-wide lifecycle observation
budget is set.

#### Scenario: Suite runs on a pull request
- **WHEN** the integration job runs
- **THEN** no test file under `tests/integration` is deselected, and the lifecycle rows 30
  and 31 skip citing their recorded RustFS observation

#### Scenario: No scheduled tick during the suite
- **WHEN** the suite runs
- **THEN** no API or worker container is running, so none of the application's declared
  schedules is persisted to tick on the suite's version

#### Scenario: Stack fails to come up
- **WHEN** PostgreSQL, RustFS or rclone does not reach healthy within the wait
- **THEN** the job fails before any test runs, and its logs name the service

#### Scenario: Application image fails to boot
- **WHEN** the API or a worker built from this commit does not reach running after the suite
- **THEN** the job fails, and its logs name the service

#### Scenario: Observation budget set in CI
- **WHEN** `CURATE_D_TEST_STAGING_LIFECYCLE_TIMEOUT_SECONDS` is set in the job environment
- **THEN** the job fails with an error naming the variable, before the suite runs

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
