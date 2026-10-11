# control-plane Specification

## Purpose

Provides the HTTP API through which a person configures connections, taxonomy, and rules, observes what the system is doing, reviews proposed organization before committing to it, and resolves the items the automated pipeline could not decide on its own.

## Requirements

### Requirement: Connection management

The control plane SHALL allow a user to initiate provider authorization, view connection health, and re-authorize a connection whose credentials have expired.

#### Scenario: New connection authorized
- **WHEN** a user completes the authorization flow for a provider
- **THEN** the connection appears with healthy status and is available to scans

#### Scenario: Expired connection surfaced
- **WHEN** a connection requires re-authentication
- **THEN** the control plane shows it as requiring attention
- **AND** offers the action needed to restore it

### Requirement: Configuration management

The control plane SHALL allow a user to view and edit the taxonomy, path templates, and placement policies. Invalid configuration SHALL be rejected before it takes effect.

#### Scenario: Valid configuration accepted
- **WHEN** a user submits a configuration change that satisfies validation
- **THEN** the change is persisted and applied to subsequent work

#### Scenario: Invalid configuration rejected
- **WHEN** a user submits a configuration change that fails validation
- **THEN** the change is rejected with an error identifying the problem
- **AND** the previously active configuration remains in effect

#### Scenario: Template change does not disturb existing placements
- **WHEN** a user changes the active path template version
- **THEN** existing placements remain valid and untouched until reconciliation acts on them

### Requirement: Job observability

The control plane SHALL report the state of scans, classification, and placements, including counts of pending, in-progress, completed, and failed work, and the reason for each failure.

#### Scenario: Progress reported
- **WHEN** work is in progress
- **THEN** the control plane reports current counts by state

#### Scenario: Failure detail available
- **WHEN** a unit of work has failed
- **THEN** the control plane reports which content object or placement failed and why

### Requirement: Review queue

The control plane SHALL present items the pipeline could not classify or render, and SHALL allow a user to resolve them by assigning facts, extending the taxonomy, or excluding the item.

#### Scenario: Item resolved by assigning facts
- **WHEN** a user assigns valid facts to a queued item
- **THEN** the item leaves the review queue
- **AND** it becomes eligible for rendering and placement

#### Scenario: Item resolved by extending taxonomy
- **WHEN** a user promotes a previously rejected value into the taxonomy
- **THEN** the value becomes valid for future classification
- **AND** the queued item can be resolved using it

#### Scenario: Invalid resolution rejected
- **WHEN** a user submits a resolution whose facts fail schema or taxonomy validation
- **THEN** the resolution is rejected with an error
- **AND** the item remains in the review queue

### Requirement: Audit inspection

The control plane SHALL allow a user to inspect the recorded history for a given content object, including how it was classified, where it was placed, and what verification each placement achieved.

#### Scenario: History retrieved
- **WHEN** a user inspects a content object
- **THEN** its classification decisions, placements, verification levels, and retention decisions are shown in order

#### Scenario: Unknown content object
- **WHEN** a user requests history for an identifier the ledger does not contain
- **THEN** the control plane reports that no such content object exists

### Requirement: Dry-run result export

The control plane SHALL expose the results of a dry run in a form suitable for reviewing proposed organization in bulk, including the facts derived for each content object, the proposed locator, and any items that failed classification or rendering.

#### Scenario: Dry-run results exported
- **WHEN** a user requests the results of a completed dry run
- **THEN** each evaluated content object is reported with its derived facts and proposed locator

#### Scenario: Unresolved items included
- **WHEN** a dry run produced items that could not be classified or rendered
- **THEN** those items appear in the export with the reason they could not be resolved
- **AND** the values rejected for falling outside the taxonomy are included so they can inform taxonomy refinement

#### Scenario: Export requested for an unknown run
- **WHEN** a user requests results for a dry run identifier that does not exist
- **THEN** the control plane reports that no such run exists

### Requirement: Runtime throughput adjustment

The control plane SHALL allow a user to view and adjust the concurrency and rate limits governing each stage of work while the system is running, without redeployment. Adjusted values SHALL persist across worker restarts and SHALL NOT be overwritten by worker startup.

#### Scenario: Concurrency adjusted at runtime
- **WHEN** a user lowers the concurrency limit for a stage
- **THEN** workers apply the new limit without being restarted
- **AND** work already in progress is allowed to finish rather than being cancelled

#### Scenario: Adjustment survives a worker restart
- **WHEN** a worker restarts after a limit has been adjusted at runtime
- **THEN** the adjusted value remains in effect
- **AND** the value configured at startup does not replace it

#### Scenario: Invalid limit rejected
- **WHEN** a user submits a limit that is negative or otherwise outside the permitted range
- **THEN** the adjustment is rejected with an error identifying the permitted range
- **AND** the previously effective limit remains in force
