# placement-reconciliation Specification

## Purpose

Continuously compares the placements a content object should have against the placements it actually has, and issues corrective actions, so that policy changes and destination losses are repaired automatically rather than requiring migration scripts.

## Requirements

### Requirement: Convergence loop

The system SHALL periodically evaluate, for every content object, the difference between its desired placements and its observed placements, and SHALL issue actions that reduce that difference. Evaluation SHALL be repeatable and SHALL NOT depend on having observed a prior evaluation.

#### Scenario: Missing placement created
- **WHEN** a content object's desired placements include one that does not exist
- **THEN** work is created to establish that placement

#### Scenario: Satisfied state produces no action
- **WHEN** a content object's observed placements already satisfy its desired placements
- **THEN** no work is created

#### Scenario: Policy changed after placement
- **WHEN** the policy governing a content object changes to require additional copies
- **THEN** the next evaluation creates work for the additional placements
- **AND** existing placements are left intact

#### Scenario: Overlapping evaluations
- **WHEN** two evaluations run concurrently over the same content object
- **THEN** duplicate work is not performed
- **AND** the ledger records a single set of placements

### Requirement: Destination loss repair

The system SHALL detect when a previously registered placement is no longer present at its destination and SHALL re-establish it.

#### Scenario: Placement disappeared
- **WHEN** a registered placement is found absent at its destination
- **THEN** the placement is marked absent
- **AND** work is created to re-establish it

#### Scenario: Destination unreachable
- **WHEN** a destination cannot be reached during evaluation
- **THEN** its placements are not marked absent
- **AND** the evaluation records that the destination could not be checked

### Requirement: Retention as policy outcome

Source retention SHALL be determined by policy evaluation, not by transfer completion. The default outcome SHALL be to retain the source.

#### Scenario: Default retention
- **WHEN** no policy explicitly permits releasing a source
- **THEN** the source is retained regardless of how many placements exist

#### Scenario: Source counts toward required copies
- **WHEN** a policy requires a number of copies and the source location is itself an eligible copy
- **THEN** the source is counted toward that requirement
- **AND** the source is not released while it is needed to satisfy the requirement

### Requirement: Verification gate on source release

A source SHALL NOT be released unless the governing policy is satisfied by placements at one of the two strong verification levels (in-transit or common-hash). Placements at weaker verification levels SHALL NOT count toward that requirement, and neither SHALL placements at destinations the policy in force does not define, which nothing checks any more.

#### Scenario: Requirement met by strongly verified copies
- **WHEN** the policy permits release and enough strongly verified placements exist
- **THEN** the source may be released

#### Scenario: Requirement met only by weakly verified copies
- **WHEN** the policy permits release but the existing placements are only weakly verified
- **THEN** the source is retained
- **AND** the reason for retention is recorded

#### Scenario: Verification level downgraded
- **WHEN** a placement's verification level is later found to be weaker than recorded
- **THEN** the source release decision is re-evaluated against the corrected levels

#### Scenario: Copy at a destination no longer in the policy
- **WHEN** a present placement is at a destination the policy in force no longer defines, so no destination check verifies it
- **THEN** it does not count toward the requirement, whatever its verification level
- **AND** the decision names that destination among the copies that did not count
