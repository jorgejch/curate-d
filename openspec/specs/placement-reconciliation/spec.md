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

### Requirement: Verification gate on source release

A source SHALL NOT be released unless the governing policy is satisfied by placements at one of the two strong verification levels (in-transit or common-hash). Placements at weaker verification levels SHALL NOT count toward that requirement. Neither SHALL placements at destinations that the policy in force does not define, because no destination check verifies them any more. Every destination the policy defines counts, whether or not a rule currently selects it for that content.

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
- **AND** the decision, its reason and its audit entry name that destination among the copies that did not count

#### Scenario: Dropping a destination withdraws a release
- **GIVEN** a rule that permits release and requires two copies, and strongly verified copies at destinations `a` and `b`, so the recorded decision is release permitted
- **WHEN** `b` is removed from the policy and the content is evaluated again
- **THEN** the new decision is retain
- **AND** it names `b` as a copy that did not count

### Requirement: Retention is a policy outcome

Source retention SHALL be determined by policy evaluation, not by transfer completion. The default outcome SHALL be to retain the source. The source SHALL NOT count toward the copies a policy requires before it permits release.

#### Scenario: Default retention
- **WHEN** no policy explicitly permits releasing a source
- **THEN** the source is retained regardless of how many placements exist

#### Scenario: Required copies exist without the source
- **WHEN** a policy permits release and requires a number of copies
- **THEN** only strongly verified placements count toward that number, and the source does not
- **AND** the source is retained until that many such placements exist

#### Scenario: Fewer copies than required
- **WHEN** a policy permits release, requires two copies, and one strongly verified placement exists
- **THEN** the source is retained
- **AND** the recorded reason states that 1 of 2 required copies is strongly verified, without counting the source
