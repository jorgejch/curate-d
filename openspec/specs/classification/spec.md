# classification Specification

## Purpose

Determines what a file is by escalating through progressively more expensive techniques, and emits structured facts describing it, so that destination paths can be derived consistently without spending inference cost on files that simpler methods already resolve.

## Requirements

### Requirement: Tiered escalation with confidence gating

The system SHALL attempt classification in ascending order of cost, and SHALL stop at the first tier whose result meets the configured confidence threshold. A more expensive tier SHALL NOT run when a cheaper tier has already produced a sufficiently confident result.

#### Scenario: Deterministic tier suffices
- **WHEN** file extension, embedded metadata, or filename patterns produce a result above the confidence threshold
- **THEN** no local inference or remote model call is made
- **AND** the recorded facts note which tier produced them

#### Scenario: Escalation to remote model
- **WHEN** neither deterministic nor local inference tiers reach the confidence threshold
- **THEN** the remote model tier is invoked
- **AND** the recorded facts note that the remote tier produced them

#### Scenario: All tiers fail to reach confidence
- **WHEN** no tier reaches the confidence threshold
- **THEN** the content object is placed in the review queue
- **AND** no destination path is derived for it
- **AND** the file is not transferred to an organized location

### Requirement: Facts-only output

Classification SHALL emit structured facts drawn from a defined schema. Classification SHALL NOT emit a destination path, filename, or folder name.

#### Scenario: Facts conform to schema
- **WHEN** any tier produces a classification result
- **THEN** the result consists only of fields defined in the facts schema
- **AND** each field value conforms to that field's declared type

#### Scenario: Model returns unusable output
- **WHEN** the remote model tier returns output that does not conform to the facts schema
- **THEN** the output is rejected
- **AND** the content object is placed in the review queue rather than being assigned invented facts

### Requirement: Closed-set taxonomy

Category and subcategory facts SHALL be drawn from a configured closed set. A value outside that set SHALL NOT be accepted.

#### Scenario: Value within taxonomy accepted
- **WHEN** a tier produces a category present in the configured taxonomy
- **THEN** the category is recorded on the content object

#### Scenario: Value outside taxonomy rejected
- **WHEN** a tier produces a category absent from the configured taxonomy
- **THEN** the value is rejected
- **AND** the content object is placed in the review queue
- **AND** the rejected value is recorded so an operator can decide whether to extend the taxonomy

### Requirement: Classification reuse

The system SHALL classify a given content fingerprint at most once, and SHALL reuse the stored result for any later source object resolving to the same fingerprint.

#### Scenario: Repeat content reuses result
- **WHEN** a source object resolves to a content fingerprint that already has recorded facts
- **THEN** the stored facts are reused
- **AND** no tier is invoked

### Requirement: Cost governance

The system SHALL enforce a configured spending cap on the remote model tier and SHALL stop invoking that tier when the cap is reached.

#### Scenario: Spend within cap
- **WHEN** remote model spending for the current period is below the configured cap
- **THEN** eligible content objects escalate to the remote tier normally

#### Scenario: Cap reached
- **WHEN** remote model spending for the current period reaches the configured cap
- **THEN** no further remote model calls are made in that period
- **AND** affected content objects are held for the next period rather than being discarded or misclassified
- **AND** the cap breach is reported through the control plane
