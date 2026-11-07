# ledger-connection Specification

## Purpose

Defines how a process resolves the address and the credentials of the ledger database from its
settings, and how it fails when those settings are not valid.

## Requirements

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
user, password, host, port, name and options settings SHALL NOT change that URL. A value that is
not a valid PostgreSQL URL MUST stop the process at settings load, and the error MUST NOT contain
the value.

#### Scenario: The full URL and the part variables are all set

- **WHEN** settings load with `CURATE_D_LEDGER_DATABASE_URL`, and also the user, password, host, port, name and options variables, all set
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

### Requirement: Ledger URL options come from the environment

The system SHALL read connection parameters from `CURATE_D_LEDGER_DATABASE_OPTIONS`, as a URL query
string, and SHALL add them to the URL that the part settings build. The default SHALL be no
parameter. A malformed value, a repeated key, a key that names a part of the connection, or a key
that carries a secret MUST stop the process at settings load, and the error MUST NOT contain the
value.

#### Scenario: No options are set

- **WHEN** settings load with `CURATE_D_LEDGER_DATABASE_OPTIONS` and `CURATE_D_LEDGER_DATABASE_URL` unset
- **THEN** the ledger URL has no query parameter

#### Scenario: Options are set

- **WHEN** settings load with `CURATE_D_LEDGER_DATABASE_OPTIONS` set to `sslmode=disable&connect_timeout=10`, and `CURATE_D_LEDGER_DATABASE_URL` unset
- **THEN** a URL parser reads the parameters `sslmode=disable` and `connect_timeout=10` from the ledger URL, with the host, port and database of the part settings

#### Scenario: An option value holds spaces and reserved characters

- **WHEN** settings load with `CURATE_D_LEDGER_DATABASE_OPTIONS` set to `options=-c statement_timeout=5000`
- **THEN** a URL parser reads the parameter `options` with the value `-c statement_timeout=5000`

#### Scenario: An option names a part of the connection

- **WHEN** settings load with `CURATE_D_LEDGER_DATABASE_OPTIONS` set to `password=other`, `password=` or `host=other`
- **THEN** settings fail to load, the error names `ledger_database_options`, and the error does not contain `other`

#### Scenario: An option carries a secret

- **WHEN** settings load with `CURATE_D_LEDGER_DATABASE_OPTIONS` set to `sslkey=/k&sslpassword=other`
- **THEN** settings fail to load, the error names `ledger_database_options`, and the error does not contain `other`

#### Scenario: The options are malformed

- **WHEN** settings load with `CURATE_D_LEDGER_DATABASE_OPTIONS` set to `sslmode`, `a=1&&b=2`, `=x` or `=`
- **THEN** settings fail to load, and the error names `ledger_database_options`

#### Scenario: An option key repeats

- **WHEN** settings load with `CURATE_D_LEDGER_DATABASE_OPTIONS` set to `sslmode=disable&sslmode=require` or `sslmode=disable&sslmode=`
- **THEN** settings fail to load, and the error names `ledger_database_options`

### Requirement: Direct connections keep the URL parameters

A process that opens a libpq connection from the ledger URL, such as a lease session, promotion or
environment reset, SHALL pass every URL parameter to libpq with its exact value. A URL that repeats
a parameter key MUST stop that connection before it opens, with an error that names the key. A
query parameter SHALL take precedence over the same part of the address, as when libpq reads the URL.

#### Scenario: A parameter value holds a space

- **WHEN** the ledger URL holds the parameter `options` with the value `-c statement_timeout=5000`
- **THEN** the connection information that the process gives to libpq holds `options` with the value `-c statement_timeout=5000`

#### Scenario: The credentials hold reserved characters

- **WHEN** the ledger URL holds the user `u@x` and the password `p/w`
- **THEN** the connection information that the process gives to libpq holds the user `u@x` and the password `p/w`

#### Scenario: A query parameter sets a part of the address

- **WHEN** `CURATE_D_LEDGER_DATABASE_URL` holds the host `db.example` and the parameter `host` with the value `/var/run/postgresql`
- **THEN** the connection information that the process gives to libpq holds the host `/var/run/postgresql`

#### Scenario: A full URL repeats a parameter key

- **WHEN** `CURATE_D_LEDGER_DATABASE_URL` holds `sslmode=disable&sslmode=require`
- **THEN** the process does not open the connection, and the error names `sslmode`
