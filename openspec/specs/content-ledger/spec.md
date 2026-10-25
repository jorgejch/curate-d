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

The system SHALL record an append-only audit entry for every classification decision, placement action, verification outcome and retention decision. Normal operation SHALL NOT modify or delete an entry. An entry SHALL carry the evidence that explains it without the workflow history, which is pruned.

#### Scenario: Decision recorded with provenance
- **WHEN** a classification or retention decision is made
- **THEN** an audit entry records the decision, the inputs it was based on, and which tier or rule produced it
- **AND** a retention decision records the version of the placement policy it applied, and the destinations whose copies counted, were too weak, or were not checked

#### Scenario: Policy version change records the retention decision again
- **GIVEN** a recorded retention decision applied under one placement policy version
- **WHEN** reconciliation evaluates the content under a new policy version and reaches the same outcome and reason
- **THEN** a new retention audit entry records the decision under the new policy version, so the latest entry names the version that governs
- **AND** evaluating again under the same version, outcome and reason records nothing

#### Scenario: Placement entries carry their evidence
- **WHEN** a copy starts, or its verification concludes
- **THEN** the entry records the attempt and the source's version marker at copy time
- **AND** a started copy's entry also records the destination's provider, account and root in force when it started, and the policy version it was requested under, so the entry explains the decision after the destination or policy is redefined and after workflow history is pruned

#### Scenario: Evidence unknown at the time
- **WHEN** a value an entry would carry is unknown, such as the policy version of a placement requested before it was recorded or a source with no version marker
- **THEN** the entry records that value as null rather than omitting the entry or guessing the value

#### Scenario: Failed action recorded
- **WHEN** a placement action fails
- **THEN** an audit entry records the failure, the affected content object and destination, and the reason

### Requirement: Audit committed with the decision

Every audited decision that changes ledger state SHALL be committed in the same ledger datasource transaction as its audit entry. A transaction that fails SHALL commit neither. The trail covers decisions about files only. An operation on workflow rows, such as a rewind, a cancellation or the recovery sweep, SHALL write no audit entry of its own.

#### Scenario: Decision and entry commit together
- **WHEN** a workflow records a decision that changes the ledger
- **THEN** the ledger change and its audit entry are committed in one datasource transaction
- **AND** a failure of either rolls back both, so the trail never explains a change that did not happen nor misses one that did

#### Scenario: Replay does not repeat an entry
- **WHEN** a workflow is recovered after a crash, or a datasource transaction is retried
- **THEN** a transaction that already committed is not executed again
- **AND** its audit entry is not appended a second time

#### Scenario: Rewound step re-executes
- **WHEN** a workflow is rewound and a step that wrote a decision runs again
- **THEN** the re-executed step appends a new audit entry for the decision it reaches, beside the original one
- **AND** entries are never deduplicated by workflow or step, so the newest entry explains the ledger's current value

#### Scenario: Rewound parent with collapsed children
- **WHEN** a parent workflow is rewound and asks again for a child under the child's existing deterministic ID
- **THEN** the existing child is not re-run, and it keeps acting on what the parent decided before the rewind
- **AND** rewinding that child after the parent makes it act on the new decision

#### Scenario: The rewind itself
- **WHEN** an operator rewinds a workflow
- **THEN** the rewind writes no audit entry
- **AND** who ran it, the workflow ID, from which step and why are recorded in the issue tracking the incident
