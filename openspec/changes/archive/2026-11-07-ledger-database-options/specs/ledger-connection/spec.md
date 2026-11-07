# Spec Delta

## ADDED Requirements

### Requirement: Ledger URL options come from the environment

The system SHALL read connection parameters from `CURATE_D_LEDGER_DATABASE_OPTIONS`, as a URL query
string, and SHALL add them to the URL that the part settings build. The default SHALL be no
parameter. A malformed value, a repeated key, or a key that names a part of the connection MUST
stop the process at settings load, and the error MUST NOT contain the value.

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

- **WHEN** settings load with `CURATE_D_LEDGER_DATABASE_OPTIONS` set to `password=other` or `host=other`
- **THEN** settings fail to load, the error names `ledger_database_options`, and the error does not contain `other`

#### Scenario: The options are malformed

- **WHEN** settings load with `CURATE_D_LEDGER_DATABASE_OPTIONS` set to `sslmode`, `a=1&&b=2` or `=x`
- **THEN** settings fail to load, and the error names `ledger_database_options`

#### Scenario: An option key repeats

- **WHEN** settings load with `CURATE_D_LEDGER_DATABASE_OPTIONS` set to `sslmode=disable&sslmode=require`
- **THEN** settings fail to load, and the error names `ledger_database_options`

### Requirement: Direct connections keep the URL parameters

A process that opens a libpq connection from the ledger URL, such as a lease session, promotion or
environment reset, SHALL pass every URL parameter to libpq with its exact value. A URL that repeats
a parameter key MUST stop that connection before it opens, with an error that names the key.

#### Scenario: A parameter value holds a space

- **WHEN** the ledger URL holds the parameter `options` with the value `-c statement_timeout=5000`
- **THEN** the connection information that the process gives to libpq holds `options` with the value `-c statement_timeout=5000`

#### Scenario: The credentials hold reserved characters

- **WHEN** the ledger URL holds the user `u@x` and the password `p/w`
- **THEN** the connection information that the process gives to libpq holds the user `u@x` and the password `p/w`

#### Scenario: A full URL repeats a parameter key

- **WHEN** `CURATE_D_LEDGER_DATABASE_URL` holds `sslmode=disable&sslmode=require`
- **THEN** the process does not open the connection, and the error names `sslmode`

## MODIFIED Requirements

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
