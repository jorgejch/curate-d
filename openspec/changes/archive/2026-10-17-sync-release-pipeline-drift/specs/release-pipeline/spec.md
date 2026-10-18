# Spec Delta

## ADDED Requirements

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
SonarQube Cloud MUST count coverage on `src/` only, and duplication on `src/` and
`infrastructure/` only. Test code under `tests/` MUST NOT count toward either metric. A new
top-level directory MUST be excluded from both until it is deliberately added.

#### Scenario: Uncovered helper in infrastructure
- **WHEN** a script under `infrastructure/` has no unit test
- **THEN** SonarQube's coverage metric is unchanged, and a block it copies from `src/` is
  reported as duplication

#### Scenario: Duplicated test fixture
- **WHEN** two test modules under `tests/` share a copied block
- **THEN** SonarQube reports no duplication for it

#### Scenario: New top-level directory left unlisted
- **WHEN** a top-level directory is added and not named in the SonarQube exclusions
- **THEN** its files count toward coverage and duplication, and the unit test pinning the
  measured scope fails naming the directory

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
  API or worker image then launches on the same database
- **THEN** the image registers its own version as latest, and the suite promotes no earlier
  version back
