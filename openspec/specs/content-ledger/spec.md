# content-ledger Specification

## Purpose

Maintains the authoritative record of what content exists, where each copy of it lives, and how every placement decision was reached, so that repeated runs are idempotent and no file is ever lost track of.

## Requirements

### Requirement: Three-layer identity model

The system SHALL distinguish three separate identities: a source object identified by provider, account, and provider-native identifier; a content object identified by content fingerprint; and a placement identified by content object and destination.

#### Scenario: Same content in two source locations
- **WHEN** two source objects in different providers or paths yield the same content fingerprint
- **THEN** both source objects reference a single content object
- **AND** classification is performed once for that content object

#### Scenario: File renamed at the source
- **WHEN** a source object's path changes but its provider-native identifier is unchanged
- **THEN** the existing source object record is updated rather than a new one created
- **AND** existing placements remain valid

### Requirement: Content fingerprinting

The system SHALL compute a strong content fingerprint for every ingested file, and SHALL additionally compute a perceptual fingerprint for images so that visually identical images differing only in encoding can be recognized.

#### Scenario: Strong fingerprint computed
- **WHEN** a file's content is read
- **THEN** a strong content fingerprint is computed and stored on the content object

#### Scenario: Perceptual fingerprint for images
- **WHEN** an ingested file is an image
- **THEN** a perceptual fingerprint is computed in addition to the strong fingerprint

#### Scenario: Content unreadable
- **WHEN** a file's content cannot be read from the provider
- **THEN** no content object is created
- **AND** the source object is marked as failed with the reason
- **AND** the failure does not block processing of other source objects

### Requirement: Placement state tracking

The system SHALL record, for every placement, its destination, rendered locator, current state, and achieved verification level.

#### Scenario: Placement recorded after transfer
- **WHEN** a placement completes successfully
- **THEN** its destination locator and verification level are recorded

#### Scenario: Placement recorded atomically with progress
- **WHEN** a placement's recorded state is updated
- **THEN** the update and the corresponding progress checkpoint either both persist or neither persists

#### Scenario: Duplicate placement attempt
- **WHEN** work is created for a placement that already exists in a completed state
- **THEN** no duplicate transfer is performed
- **AND** no duplicate placement record is created

### Requirement: Immutable audit trail

The system SHALL record an append-only audit entry for every classification decision, placement action, verification outcome, and retention decision. Audit entries SHALL NOT be modified or deleted by normal operation. An entry type the system no longer writes SHALL stay valid in the ledger, so entries already recorded under it remain readable: `workflow_command_issued` and `workflow_command_completed`, recorded by a Conductor command path that has since been removed, are such types.

#### Scenario: Decision recorded with provenance
- **WHEN** a classification or retention decision is made
- **THEN** an audit entry records the decision, the inputs it was based on, and which tier or rule produced it

#### Scenario: Failed action recorded
- **WHEN** a placement action fails
- **THEN** an audit entry records the failure, the affected content object and destination, and the reason

#### Scenario: Entries of a retired type
- **WHEN** the ledger holds `workflow_command_issued` or `workflow_command_completed` entries recorded before the Conductor command path was removed
- **THEN** they remain in the trail unchanged, valid under the entry-type constraint and returned by audit reads
- **AND** no new entry of either type is written

### Requirement: Perceptual fingerprint within the decode budget

The system SHALL compute an image's perceptual fingerprint within the CPU worker's pixel budget, decoding at a reduced scale where the format allows. An image that would still exceed the budget, or that no decoder reads, SHALL keep its strong fingerprint and media type, SHALL have no perceptual fingerprint, and SHALL have the reason recorded.

#### Scenario: Large JPEG
- **WHEN** a JPEG larger than the budget can be decoded at a reduced scale within it
- **THEN** its perceptual fingerprint is computed from the reduced decode
- **AND** it lies within the match threshold of a full decode's fingerprint

#### Scenario: Image above the budget
- **WHEN** an image would decode to more pixels than the budget
- **THEN** the content object is recorded with its strong fingerprint and no perceptual fingerprint
- **AND** the inspection records the image's size and the budget

### Requirement: Files that can never be staged fail at once

A file larger than any bound it must fit whole — the configured per-file ceiling when set, the total staging bound, or the reading worker's local scratch bound — SHALL fail before its content is requested from the provider. A staging refusal stating that a request can never fit SHALL be treated the same way. Neither SHALL wait for capacity.

#### Scenario: File larger than staging
- **WHEN** a listed file's size exceeds a bound it must fit whole
- **THEN** no content is requested from the provider and no content object is created
- **AND** the source object is marked as failed with a reason naming the file's size and the bound
- **AND** the failure is recorded in the audit trail

#### Scenario: Staging reports that a request will never fit
- **WHEN** staging refuses a file because it exceeds the whole bound
- **THEN** the source object is marked as failed at once, without waiting for capacity

#### Scenario: Staging full for now
- **WHEN** staging refuses a file that fits its bound because the space is in use
- **THEN** the ingest waits durably and tries again, for a bounded time, as before
