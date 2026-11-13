# Spec Delta

## ADDED Requirements

### Requirement: The documented DBOS version matches the pin
The DBOS minor version that `openspec/config.yaml` and `README.md` name as the current
version MUST equal the minor version of the `dbos` pin in `pyproject.toml`. A unit test MUST
compare each named version with the pin. A dated record, such as the assessment line in
`docs/twelve-factor.md`, is not a current statement and is not compared.

#### Scenario: The documents name the pinned version
- **WHEN** `pyproject.toml` pins `dbos[otel]` to a version and each of the two documents
  names DBOS at that same minor version
- **THEN** the unit suite passes

#### Scenario: A document names an older version
- **WHEN** `pyproject.toml` pins `dbos[otel]` to 3.5.0 and `README.md` still names DBOS 3.4
- **THEN** the unit suite fails, naming the document and the version it found

#### Scenario: The pin is missing or unreadable
- **WHEN** `pyproject.toml` has no exact `dbos[otel]` pin, or the file cannot be parsed
- **THEN** the unit suite fails, naming the file and the missing pin, and does not pass by
  skipping the comparison
