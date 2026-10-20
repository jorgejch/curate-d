# Spec Delta

## MODIFIED Requirements

### Requirement: Immutable audit trail

The system SHALL record an append-only audit entry for every classification decision, placement action, verification outcome, and retention decision. Audit entries SHALL NOT be modified or deleted by normal operation. An entry type the system no longer writes SHALL stay valid in the ledger, so entries already recorded under it remain readable: `workflow_command_issued` and `workflow_command_completed`, recorded by a Conductor command path that has since been removed, are such types. An entry SHALL carry the evidence that explains it without the workflow history, which is pruned.

#### Scenario: Decision recorded with provenance
- **WHEN** a classification or retention decision is made
- **THEN** an audit entry records the decision, the inputs it was based on, and which tier or rule produced it
- **AND** a retention decision records the version of the placement policy it applied, and the destinations whose copies counted, were too weak, or were not checked

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

#### Scenario: Entries of a retired type
- **WHEN** the ledger holds `workflow_command_issued` or `workflow_command_completed` entries recorded before the Conductor command path was removed
- **THEN** they remain in the trail unchanged, valid under the entry-type constraint and returned by audit reads
- **AND** no new entry of either type is written

## ADDED Requirements

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
