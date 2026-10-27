## MODIFIED Requirements

### Requirement: SonarQube measures production code only
SonarQube Cloud MUST count coverage on `src/` only, and duplication on `src/`, `ops/`
and `migrations/`. It MUST explicitly classify `tests/` as test code, independently
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
- **WHEN** coverage exclusions no longer name exactly `ops/**` and `migrations/**`,
  duplication exclusions are added, or the coverage report path changes
- **THEN** the unit suite fails naming the setting

#### Scenario: New top-level directory left unlisted
- **WHEN** a top-level directory is added outside the selected source and test roots
- **THEN** its files remain outside analysis and both metrics without adding exclusions

#### Scenario: Uncovered migration
- **WHEN** a migration has no unit coverage or copies a block from another source root
- **THEN** it does not lower coverage, but remains eligible for duplication findings

### Requirement: SonarQube analyses explicitly selected source and test trees
SonarQube Cloud MUST use `src/`, `ops/` and `migrations/` as its only source roots and
`tests/` as its only test root. These scopes MUST be disjoint and MUST NOT depend on
pytest's `testpaths`. It MUST NOT enumerate other paths through exclusions or inclusions.
Scope drift MUST fail the unit suite naming the setting. Contributor documentation MUST
explain how to expand the roots and review metric scope.

#### Scenario: Existing non-source paths
- **WHEN** a Dockerfile, workflow, root file, document or OpenSpec artifact changes
- **THEN** SonarQube does not analyse it because it is outside the selected roots

#### Scenario: New top-level directory
- **WHEN** a directory outside the selected roots is added
- **THEN** it receives no Sonar analysis, coverage or duplication measurement, without a
  new exclusion or test update

#### Scenario: Source and test roots drift
- **WHEN** a source or test root changes or a source root overlaps the test tree
- **THEN** the unit suite fails naming the setting

#### Scenario: Migration changes
- **WHEN** a Python migration changes
- **THEN** SonarQube analyses it as source code

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