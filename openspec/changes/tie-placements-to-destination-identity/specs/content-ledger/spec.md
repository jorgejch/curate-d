# Spec Delta

## MODIFIED Requirements

### Requirement: Three-layer identity model

The system SHALL distinguish three separate identities: a source object identified by provider, account, and provider-native identifier; a content object identified by content fingerprint; and a placement identified by content object and destination. A destination SHALL be identified by its provider, account and root path, the root in one canonical form; the name a policy gives it SHALL be a label recorded on the placement, not part of any identity.

#### Scenario: Same content in two source locations
- **WHEN** two source objects in different providers or paths yield the same content fingerprint
- **THEN** both source objects reference a single content object
- **AND** classification is performed once for that content object

#### Scenario: File renamed at the source
- **WHEN** a source object's path changes but its provider-native identifier is unchanged
- **THEN** the existing source object record is updated rather than a new one created
- **AND** existing placements remain valid

#### Scenario: One destination name pointed at another account
- **WHEN** a policy points a destination name at another account or root path than the one existing placements under that name were made at
- **THEN** those placements keep the account and root path they were made at
- **AND** they are not counted as copies at the new account or root path
- **AND** locators they hold do not take a path at the new account or root path

#### Scenario: Destinations sharing a provider
- **WHEN** two destinations on one provider differ in account or root path
- **THEN** one-placement-per-destination and one-content-per-locator hold at each separately
- **AND** each can hold a copy at the same locator

#### Scenario: Destination renamed
- **WHEN** a policy gives a new name to a destination whose provider, account and root path are unchanged
- **THEN** its existing placements belong to it under the new name
- **AND** the next copy started under the new name records that name as the placement's label

#### Scenario: Two spellings of one root
- **WHEN** two destinations name one root with and without a trailing slash
- **THEN** they are one destination identity

### Requirement: Placement state tracking

The system SHALL record, for every placement, its destination identity and the destination name it was last placed under, its rendered locator, current state, and achieved verification level.

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

#### Scenario: Copy started records its destination
- **WHEN** a copy starts at a destination no placement was ever made at
- **THEN** the destination's identity is recorded in the transaction that records the copy starting
- **AND** the placement references it

## ADDED Requirements

### Requirement: Placement identity backfill

Upgrading a ledger whose placements are keyed by destination name SHALL assign each placement the destination identity it was made at, and SHALL abort without changing the ledger when any placement cannot be resolved, when two would share an identity with one content object or one locator, or when a placement is still being copied or verified.

#### Scenario: Location recorded with the copy
- **WHEN** a placement's latest recorded copy start names the destination's provider, account and root
- **THEN** the placement is assigned that identity

#### Scenario: Location resolved from the policy in force
- **WHEN** a placement's latest recorded copy start names no location
- **THEN** the placement is assigned the identity its destination name had in the policy version that start names, else in the policy version in force when it started, else when it was created
- **AND** a name missing from that version is looked up in the versions before it, latest first

#### Scenario: Placement that cannot be resolved
- **WHEN** no policy version in force by the time a placement started defines its destination name
- **THEN** the upgrade fails naming the placement
- **AND** nothing is changed

#### Scenario: Two placements resolve to one place
- **WHEN** two placements under different names resolve to one identity with one content object or one locator
- **THEN** the upgrade fails naming them
- **AND** nothing is changed

#### Scenario: Placement still in flight
- **WHEN** a placement is pending, copying or verifying when the upgrade runs
- **THEN** the upgrade fails naming it, because the copy's workflow is keyed by the name
- **AND** nothing is changed
