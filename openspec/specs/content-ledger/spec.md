# content-ledger Specification

## Purpose

Maintains the authoritative record of what content exists, where each copy of it lives, and how every placement decision was reached, so that repeated runs are idempotent and no file is ever lost track of.

## Requirements

### Requirement: Three-layer identity model

The system SHALL distinguish three separate identities: a source object identified by provider, account, and provider-native identifier; a content object identified by content fingerprint; and a placement identified by content object and destination. A destination SHALL be identified by its provider, account and root path; the name a policy gives it SHALL be a label recorded on the placement, not part of any identity.

#### Scenario: Same content in two source locations
- **WHEN** two source objects in different providers or paths yield the same content fingerprint
- **THEN** both source objects reference a single content object
- **AND** classification is performed once for that content object

#### Scenario: File renamed at the source
- **WHEN** a source object's path changes but its provider-native identifier is unchanged
- **THEN** the existing source object record is updated rather than a new one created
- **AND** existing placements remain valid

#### Scenario: One destination name in two accounts
- **WHEN** a policy points a destination name at another account or root path than the one existing placements under that name were made at
- **THEN** those placements keep the account and root path they were made at
- **AND** they are not counted as copies at the new account or root path
- **AND** locators they hold do not take a path at the new account or root path

#### Scenario: Destinations sharing a provider
- **WHEN** two destinations on one provider differ in account or root path
- **THEN** one-placement-per-destination and one-content-per-locator hold at each separately
- **AND** each can hold a copy at the same locator

#### Scenario: Two names for one place refused
- **WHEN** a policy defines two destinations with the same provider, account and root path, or one whose root path lies inside another's in the same account
- **THEN** the policy is refused, because one file would count as a copy at both

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
