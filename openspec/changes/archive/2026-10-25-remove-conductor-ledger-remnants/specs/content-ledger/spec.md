# Spec Delta

## MODIFIED Requirements

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
