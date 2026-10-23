# Spec Delta

## ADDED Requirements

### Requirement: A forked row is reconstructed or fails by name

A workflow input or step output pickled by an older build SHALL be reconstructed on a newer one by filling every field added since with its declared default. A required field the pickle lacks SHALL fail the reconstruction with a `StaleCheckpointError` naming the model and the missing fields, so the row ends ERROR rather than failing on a later read. A field no longer declared SHALL be ignored. A field added to a pickled model SHALL carry a default that means what the older build did.

#### Scenario: A field added with a default
- **WHEN** a row pickled before a defaulted field existed is dequeued, recovered or forked onto a build that declares it
- **THEN** the field reads as its declared default and the workflow runs as the older build would have

#### Scenario: A required field added
- **WHEN** a row pickled before a required field existed reaches a build that declares it
- **THEN** reconstruction raises `StaleCheckpointError` naming the model and the field
- **AND** the row ends ERROR with that error, not an `AttributeError` from a later read

#### Scenario: A retired field
- **WHEN** a row pickled with a field the build no longer declares is reconstructed
- **THEN** the field is ignored and the model reads its declared fields
