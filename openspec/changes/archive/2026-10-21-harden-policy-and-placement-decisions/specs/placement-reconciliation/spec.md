# Spec Delta

## REMOVED Requirements

### Requirement: Retention as policy outcome
**Reason**: Its scenario "Source counts toward required copies" counted the original as one of the required copies. Release has always needed the required copies without the original, so the scenario described a flag (`source_counts_as_copy`) that changed no outcome; the flag is removed (#12).
**Migration**: Replaced by "Retention is a policy outcome" below, which keeps the default-retention scenario and states that only strongly verified placements count, never the source.

## ADDED Requirements

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

## MODIFIED Requirements

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
