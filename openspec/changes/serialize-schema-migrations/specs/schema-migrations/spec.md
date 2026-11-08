# Spec Delta

## Purpose

Defines how the ledger schema and the DBOS system schema in the ledger database are brought to a
revision: which process migrates, how runs started at the same time are serialized, how a waiting
run gives up, and what a process that does not migrate does when the schema is behind.

## ADDED Requirements

### Requirement: Migration runs are serialized

An online migration run (`alembic upgrade` or `alembic downgrade`) SHALL take one session advisory
lock on the ledger database before it changes anything, and SHALL hold it until its changes are
committed or rolled back. A run that finds the lock held SHALL wait for it. After taking the lock a
run SHALL read the current revision again and apply only the revisions still pending. A run that
finds the schema already at its target SHALL change nothing and exit 0.

#### Scenario: Several upgrades start on an empty database

- **WHEN** two or more `alembic upgrade head` runs start at once against an empty database
- **THEN** every run exits 0, every revision is applied exactly once, and the schema is at head

#### Scenario: Several upgrades start one revision behind

- **WHEN** two or more `alembic upgrade head` runs start at once against a database one revision behind head
- **THEN** every run exits 0, the pending revision is applied exactly once, and the schema is at head

#### Scenario: A downgrade and an upgrade start at once

- **WHEN** an `alembic downgrade` and an `alembic upgrade` start at once
- **THEN** one holds the lock and runs whole before the other reads the revision

#### Scenario: The holder's connection is lost

- **WHEN** the connection of the run holding the lock is lost before it commits
- **THEN** its transaction rolls back, the lock is released with the session, the run that lost it logs `migration.lock_lost` and exits non-zero, and a waiting run takes the lock and applies what is still pending

### Requirement: A waiting run gives up after the configured timeout

A run SHALL wait for the migration lock at most `CURATE_D_MIGRATION_LOCK_TIMEOUT_SECONDS` seconds,
a positive number of seconds that defaults to 1800. When the wait ends without the lock, the run
SHALL log one ERROR event, `migration.lock_timeout`, that names the setting and describes the
holder when it is visible, SHALL exit non-zero, and SHALL NOT have changed the schema.

#### Scenario: The lock stays held past the timeout

- **WHEN** another session holds the migration lock for longer than the timeout
- **THEN** the run logs `migration.lock_timeout`, exits non-zero, and neither the ledger schema nor the DBOS system schema has changed

#### Scenario: The setting is not a positive number

- **WHEN** `CURATE_D_MIGRATION_LOCK_TIMEOUT_SECONDS` is zero, negative or not a number
- **THEN** settings load fails naming the field, and the run changes nothing

#### Scenario: The wait is visible

- **WHEN** a run waits for the lock
- **THEN** it logs `migration.lock_waiting` once, with the holder's description when it is visible, before it blocks

### Requirement: The migration lock stands apart from other locks

The migration lock's key SHALL be derived from the application's base name under its own versioned
key domain, so that it collides with the executor, version, promotion-fence and schedule-authority
keys only by a 64-bit hash collision, and SHALL be stable across processes and builds of the same
derivation.

#### Scenario: Two builds migrate at once

- **WHEN** a build with the old derivation and one with the new derivation both run the migration
- **THEN** both derive the same key and exclude each other

#### Scenario: A lock of another kind is held

- **WHEN** a worker holds its executor lease, its version lease, the promotion fence or the schedule authority
- **THEN** a migration run is not blocked by it, and the migration lock blocks none of them

### Requirement: One migrator brings every schema in the ledger database up to date

An online run SHALL bring the DBOS system schema and the ledger datasource's tracking table up to
date, through DBOS's public migration API, while it holds the migration lock and before it applies
the ledger revisions. An offline run (`alembic upgrade --sql`) SHALL take no lock and SHALL emit no
DBOS SQL; its output SHALL say that the DBOS schema must be migrated separately.

#### Scenario: Upgrade on an empty database

- **WHEN** `alembic upgrade head` runs against a database with no `dbos` schema
- **THEN** after it exits 0 the DBOS system tables, the datasource tracking table and every ledger revision exist

#### Scenario: The DBOS step fails

- **WHEN** the DBOS system-schema step raises
- **THEN** the run exits non-zero naming the cause, applies no ledger revision, and releases the lock

#### Scenario: Offline run

- **WHEN** `alembic upgrade head --sql` runs
- **THEN** it connects to no database, takes no lock, and emits only the ledger SQL

### Requirement: A run that loses its lock applies no ledger revision

The DBOS system-schema and tracking-table steps write on connections of their own. After each of
them the run SHALL ask the lock's own session whether it still holds the migration lock. A run
whose lock is lost, there or during the ledger step, SHALL log one ERROR event,
`migration.lock_lost`, naming the step, SHALL apply no ledger revision and SHALL exit non-zero.

#### Scenario: The lock's session ends while a DBOS step runs

- **WHEN** the session holding the migration lock is ended after the run started the DBOS system-schema step
- **THEN** the run logs `migration.lock_lost`, exits non-zero, and no ledger revision is applied

#### Scenario: The lock's session ends during the ledger step

- **WHEN** the session holding the migration lock is ended while Alembic applies the ledger revisions
- **THEN** Alembic's transaction rolls back, the run logs `migration.lock_lost` and exits non-zero, and `alembic_version` is as it was before the run

### Requirement: An upgrade on a schema ahead of its build changes nothing and exits 0

An online `alembic upgrade` that finds the database at a revision its build does not know SHALL
treat the schema as ahead of the build: it SHALL log one WARNING event, `migration.schema_ahead`,
naming the database revision and the build's head, SHALL apply no ledger, DBOS or datasource
migration, and SHALL exit 0. An `alembic downgrade` from an unknown revision SHALL fail with
Alembic's error, exit non-zero and change nothing.

#### Scenario: An older image migrates a newer database

- **WHEN** `alembic upgrade head` runs from a build whose migration chain does not contain the database's current revision
- **THEN** it logs `migration.schema_ahead`, exits 0, and `alembic_version`, the DBOS system schema and the tracking table are unchanged

#### Scenario: The database is behind the build

- **WHEN** `alembic upgrade head` runs on a database whose current revision is in the build's chain but is not head
- **THEN** it applies the pending revisions and exits 0

#### Scenario: Downgrade from an unknown revision

- **WHEN** `alembic downgrade <revision>` runs on a database whose current revision the build does not know
- **THEN** it exits non-zero with Alembic's error and changes nothing

### Requirement: Processes that run workflows verify the schema and never migrate it

A worker SHALL configure DBOS and the ledger datasource so that launch verifies the DBOS system
schema and the tracking table instead of changing them. A worker that finds either behind the
version its build requires SHALL fail launch with that error and SHALL NOT migrate. A schema ahead
of the build SHALL be accepted.

#### Scenario: Launch on a migrated database

- **WHEN** a worker launches after `alembic upgrade head` from the same build has run
- **THEN** it launches without writing DDL

#### Scenario: Launch on a database behind

- **WHEN** a worker launches against a database whose DBOS system schema is missing or behind its DBOS version
- **THEN** launch fails with DBOS's initialization error and the schema is unchanged

#### Scenario: Several workers launch at once

- **WHEN** several workers launch at once on a migrated database
- **THEN** none of them migrates, and none blocks another on a migration lock
