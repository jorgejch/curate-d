# Spec Delta

## ADDED Requirements

### Requirement: Recorded SDK versions match the locked dependencies
The evidence matrix MUST name the S3 SDK versions (`aioboto3`, `aiobotocore`, `botocore`)
its rows were earned under, and those versions MUST equal the ones the dependency lock
resolves. The unit suite MUST fail, naming each differing package with both versions, when
they diverge. A matrix that cannot be parsed for its SDK row is a failure, not a pass.

#### Scenario: SDK patch lands through the lock
- **WHEN** the dependency lock moves `botocore` to a version the matrix's SDK row does not name
- **THEN** the unit suite fails and names `botocore` with the recorded and the locked version

#### Scenario: Lock and matrix agree
- **WHEN** every SDK version the matrix names equals the locked version
- **THEN** the check passes without touching the matrix

#### Scenario: SDK row missing
- **WHEN** the matrix has no SDK row, or the row does not name all three packages
- **THEN** the check fails and names the matrix file; it does not report agreement

### Requirement: SDK evidence is re-recorded by one repeatable command
Re-earning the SDK-sensitive rows after an SDK change MUST be a single command that runs the
shared contract suite on each selected backend, reads the results and the captured request
headers from the run's report, and rewrites the SDK row and those rows' backend cells with
the result, the date, the backend identity and the SDK versions. It MUST NOT change any other
cell or prose.

#### Scenario: A contract test fails
- **WHEN** a selected backend's run has a failing SDK-sensitive test
- **THEN** that backend's cell records the failure, the test name and the date, the command exits non-zero, and no cell is marked pass

#### Scenario: The suite cannot run
- **WHEN** the selected backend's endpoint does not accept a connection before the run, the suite produces no report, every test in it is skipped, or a failure in it shows the backend was never reached or its settings were refused
- **THEN** the command exits non-zero without rewriting the matrix, and the previous evidence stays in every cell

#### Scenario: Every contract test fails against a reachable backend
- **WHEN** the backend accepts connections and answers, and it rejects every test, as it would an SDK default it does not support
- **THEN** that backend's cells record the failure as for any failing test; the run is not treated as one that could not run

#### Scenario: The installed SDK is not the locked one
- **WHEN** the environment that would run the suite has an SDK package at a version other than the locked one
- **THEN** the command exits non-zero before running the suite, naming the package with both versions, and leaves the matrix unchanged

#### Scenario: Inherited test selection
- **WHEN** the caller's environment carries pytest options that would deselect contract tests
- **THEN** the command still runs the whole contract suite, so its result cannot stand for part of it

#### Scenario: Dry run
- **WHEN** the command is asked for a dry run
- **THEN** it prints the rewritten rows and the review list and leaves the matrix file unchanged

### Requirement: SDK evidence records only what ran
The command MUST record only the backends it ran. A backend not selected SHALL have its
SDK-sensitive cells marked pending, with the previous evidence kept in the cell, whenever the
SDK versions moved. The command MUST list every other line of the matrix that still names the
previous SDK versions, for review by a person.

#### Scenario: Local engine run only
- **WHEN** the command runs with the local engine selected and OVH not selected after the SDK moved
- **THEN** the local engine cells of the SDK-sensitive rows carry the new result and date, the OVH cells of those rows read pending with the previous evidence kept, and the SDK row names the locked versions

#### Scenario: SDK unchanged
- **WHEN** the command runs and the locked SDK versions equal the ones the matrix names
- **THEN** the cells of backends not selected are left untouched and only the selected backend's cells are rewritten
