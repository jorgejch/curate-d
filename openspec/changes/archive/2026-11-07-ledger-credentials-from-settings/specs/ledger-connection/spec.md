# Spec Delta

## Purpose

Defines how a process resolves the address and the credentials of the ledger database from its
settings, and how it fails when those settings are not valid.

## ADDED Requirements

### Requirement: Ledger credentials come from the environment

The system SHALL read the ledger user from `CURATE_D_LEDGER_DATABASE_USER` and the ledger password
from `CURATE_D_LEDGER_DATABASE_PASSWORD`. Each value SHALL default to `curate_d`. An empty value
MUST stop the process at settings load, and the error MUST NOT contain the value.

#### Scenario: No credential variable is set

- **WHEN** settings load with no `CURATE_D_LEDGER_DATABASE_*` variable set
- **THEN** the ledger URL is the local address `localhost:5433`, database `curate_d`, with user `curate_d` and password `curate_d`

#### Scenario: Credential variables are set

- **WHEN** settings load with `CURATE_D_LEDGER_DATABASE_USER` and `CURATE_D_LEDGER_DATABASE_PASSWORD` set, and `CURATE_D_LEDGER_DATABASE_URL` unset
- **THEN** the ledger URL holds these credentials, with the default host, port and database

#### Scenario: A credential variable is empty

- **WHEN** settings load with `CURATE_D_LEDGER_DATABASE_PASSWORD` set to an empty string
- **THEN** settings fail to load, the error names `ledger_database_password`, and the process does not start

### Requirement: The ledger address comes from the environment

The system SHALL read the ledger host from `CURATE_D_LEDGER_DATABASE_HOST`, default `localhost`,
the ledger port from `CURATE_D_LEDGER_DATABASE_PORT`, default `5433`, and the database name from
`CURATE_D_LEDGER_DATABASE_NAME`, default `curate_d`. A host or a name that could change another part
of the URL, or a port outside 1 to 65535, MUST stop the process at settings load.

#### Scenario: Host and port variables are set

- **WHEN** settings load with `CURATE_D_LEDGER_DATABASE_HOST` set to `db.example` and `CURATE_D_LEDGER_DATABASE_PORT` set to `5432`, and `CURATE_D_LEDGER_DATABASE_URL` unset
- **THEN** the ledger URL holds the host `db.example`, the port `5432` and the database `curate_d`

#### Scenario: The database name variable is set

- **WHEN** settings load with `CURATE_D_LEDGER_DATABASE_NAME` set to `ledger_prod`, and `CURATE_D_LEDGER_DATABASE_URL` unset
- **THEN** the ledger URL holds the database `ledger_prod`, with the default host and port

#### Scenario: The database name could change the URL

- **WHEN** settings load with `CURATE_D_LEDGER_DATABASE_NAME` set to `ledger?sslmode=disable`
- **THEN** settings fail to load, the error names `ledger_database_name`, and the process does not start

#### Scenario: The host could change the URL

- **WHEN** settings load with `CURATE_D_LEDGER_DATABASE_HOST` set to `db.example/other`
- **THEN** settings fail to load, the error names `ledger_database_host`, and the process does not start

#### Scenario: The port is out of range

- **WHEN** settings load with `CURATE_D_LEDGER_DATABASE_PORT` set to `70000`
- **THEN** settings fail to load, the error names `ledger_database_port`, and the process does not start

### Requirement: Credentials keep their value in the ledger URL

The system SHALL percent-encode the user and the password when it derives the ledger URL, so that
a URL parser reads back the exact values. A credential with reserved URL characters MUST NOT
change the host, the port or the database of the URL, and MUST NOT stop settings from loading.

#### Scenario: The password holds reserved characters

- **WHEN** settings load with `CURATE_D_LEDGER_DATABASE_PASSWORD` set to `p/w:#?`
- **THEN** a URL parser reads the password `p/w:#?` and the host `localhost` from the ledger URL

#### Scenario: The user holds reserved characters

- **WHEN** settings load with `CURATE_D_LEDGER_DATABASE_USER` set to `u@x`
- **THEN** a URL parser reads the user `u@x` and the host `localhost` from the ledger URL

### Requirement: A full ledger URL takes precedence

The system SHALL use `CURATE_D_LEDGER_DATABASE_URL` as the ledger URL when it is set, and the
user, password, host, port and name settings SHALL NOT change that URL. A value that is not a valid
PostgreSQL URL MUST stop the process at settings load, and the error MUST NOT contain the value.

#### Scenario: The full URL and the part variables are all set

- **WHEN** settings load with `CURATE_D_LEDGER_DATABASE_URL`, and also the user, password, host, port and name variables, all set
- **THEN** the ledger URL is the value of `CURATE_D_LEDGER_DATABASE_URL`

#### Scenario: The full URL is not valid

- **WHEN** settings load with `CURATE_D_LEDGER_DATABASE_URL` set to a URL with a scheme that is not PostgreSQL
- **THEN** settings fail to load, the error names `ledger_database_url`, and the error does not contain the value

### Requirement: The settings module holds no credential URL

The settings source file MUST NOT contain a literal database URL with a password in it. A secret
scan of the file that reports a finding MUST fail verification.

#### Scenario: The settings file is scanned for secrets

- **WHEN** `sonar analyze secrets src/curate_d/config.py` runs
- **THEN** the command reports no secret
