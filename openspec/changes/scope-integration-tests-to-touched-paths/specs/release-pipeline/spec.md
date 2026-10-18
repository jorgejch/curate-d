# Spec Delta

## MODIFIED Requirements

### Requirement: Pull requests into protected branches are the merge gate
A pull request into `dev` or `main` MUST pass the checks job (lint, the in-workflow
database-access audit, type check, the unit suite with its 80% line-coverage floor) and
the integration job (the whole
`tests/integration` suite, see below) before it can merge. Both are
required status checks, and the pull request MUST be up to date with its base when the
checks run. The integration job MAY pass without running the suite only when the pull
request changes none of the paths the integration suite depends on (see "The integration
job runs the suite only for changes it can observe"). Further ruleset rules
(code-scanning results from CodeQL or SonarQube) MAY gate the merge without changing this
requirement.

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

#### Scenario: Documentation-only pull request
- **WHEN** a pull request into `dev` changes only files outside the integration suite's
  declared paths, such as `docs/` and `README.md`, and the checks job is successful
- **THEN** the integration job passes without running the suite and the pull request can
  merge

## ADDED Requirements

### Requirement: The integration job runs the suite only for changes it can observe
The integration job MUST itself determine the pull request's changed files, as a step
before any tool is installed or any service started, and run the suite and the
application boot only when at least one changed file is under a declared path. When none
is, the job MUST run no later step but the skip summary and the stack teardown, MUST record
in that summary that the suite was not run and why, and MUST pass. The job MUST fail when the changed files
cannot be determined or may be incomplete.

#### Scenario: Application code changes
- **WHEN** a pull request changes a file under `src/`
- **THEN** the integration job runs the whole suite and the application boot

#### Scenario: Only unit tests change
- **WHEN** a pull request changes files under `tests/unit/` and nothing else
- **THEN** the integration job installs nothing, starts nothing, runs no test, writes a
  summary naming the declared paths, and passes

#### Scenario: Mounted infrastructure changes
- **WHEN** a pull request changes a file under `infrastructure/rustfs/` or
  `infrastructure/postgres/`, or `infrastructure/staging-compatibility.md`
- **THEN** the integration job runs the whole suite

#### Scenario: Unmounted infrastructure changes
- **WHEN** a pull request changes files under `infrastructure/reset/` and nothing else
- **THEN** the integration job runs no test and passes

#### Scenario: Image definition or stack definition changes
- **WHEN** a pull request changes `docker/Dockerfile`, `docker/Dockerfile.dockerignore`,
  `.dockerignore`, `compose.yaml`, a Compose override file,
  `Makefile`, `pyproject.toml`, `uv.lock`, `.python-version`, `alembic.ini`, a file under
  `migrations/`, or `.github/workflows/integration.yml`
- **THEN** the integration job runs the whole suite

#### Scenario: Change detection fails
- **WHEN** the step that lists the pull request's changed files fails, or reports a
  value other than a match or no match
- **THEN** the integration job fails before any later step, naming the detection, and
  the pull request cannot merge

#### Scenario: Changed-file list may be truncated
- **WHEN** the detection finds no declared path and the pull request changes 3,000 files
  or more
- **THEN** the integration job fails before any later step, and the pull request cannot
  merge

#### Scenario: Gate shape changes
- **WHEN** the detection step is moved out of the integration job, a step after it loses
  its condition on the detection's result, or the stack teardown stops running on every
  outcome
- **THEN** the unit suite fails naming the step

#### Scenario: A file read implicitly is added
- **WHEN** a pull request adds `conftest.py`, `tests/conftest.py`, a pytest configuration
  file in `tests/` or the root, a Compose override file (`compose.override.yml`,
  `compose.override.yaml`, `docker-compose.override.yml` or `docker-compose.override.yaml`),
  `.env`, `.gitattributes` or `docker/Dockerfile.dockerignore`
- **THEN** the integration job runs the whole suite

#### Scenario: A packaging file is removed
- **WHEN** the detection finds no declared path and `README.md`, `LICENSE` or `NOTICE` is
  missing from the checkout
- **THEN** the integration job fails before any later step, naming the file

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
