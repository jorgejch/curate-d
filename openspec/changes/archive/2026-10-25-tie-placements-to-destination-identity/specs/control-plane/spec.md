# Spec Delta

## ADDED Requirements

### Requirement: Destinations sharing a place refused

A placement policy, and the named destinations of a dry run, SHALL be refused with 422 when two destinations have the same provider, account and canonical root path, when one destination's root lies inside another's in the same account, or when a root path is not absolute or has an empty, `.` or `..` segment. The error SHALL name the destinations concerned.

#### Scenario: Two names for one place
- **WHEN** a user submits a policy with two destinations whose provider, account and root path are the same, however the roots are spelled
- **THEN** the change is rejected with 422 and an error naming both destinations
- **AND** the version in force is unchanged

#### Scenario: Nested roots in one account
- **WHEN** a user submits a policy with one destination's root inside another's in the same account
- **THEN** the change is rejected with 422 and an error naming both destinations, because one file would count as a copy at both

#### Scenario: Root that does not normalize
- **WHEN** a user submits a policy with a destination root that is not absolute or has an empty, `.` or `..` segment
- **THEN** the change is rejected with 422 and an error naming the destination

#### Scenario: Same roots in two accounts accepted
- **WHEN** a user submits a policy with two destinations on one provider in different accounts with the same root path
- **THEN** the policy is accepted

#### Scenario: Name pointed at another account accepted
- **WHEN** a user submits a policy that keeps a destination's name and changes its account or root path
- **THEN** the policy is accepted
- **AND** the destination is a new one for placement, checks and retention

#### Scenario: Named dry-run destinations sharing a place
- **WHEN** a user starts a dry run naming two destinations with the same provider, account and root path
- **THEN** the request is refused with 422 and no run is started

### Requirement: Destination identity in views

The history of a content object, the failed-units view and a dry-run export SHALL identify each placement's or item's destination by its identity and location beside the policy's name for it.

#### Scenario: History after a name moved accounts
- **WHEN** a user inspects content placed under one destination name at two accounts
- **THEN** the history shows two placements, each with its destination identifier and its provider, account and root path

#### Scenario: Failed placement located
- **WHEN** a failed placement is listed among failed units
- **THEN** its location names the destination's name, provider, account and root path, and its locator

#### Scenario: Dry-run item identifies its destination
- **WHEN** a user exports a dry run
- **THEN** each item that reached rendering carries its destination's identifier
- **AND** an item that did not reach rendering carries none

## MODIFIED Requirements

### Requirement: Audit inspection

The control plane SHALL allow a user to inspect the recorded history for a given content object, including how it was classified, where it was placed, and what verification each placement achieved. Each placement SHALL be shown with its destination identity and location, and the name it was last placed under.

#### Scenario: History retrieved
- **WHEN** a user inspects a content object
- **THEN** its classification decisions, placements, verification levels, and retention decisions are shown in order

#### Scenario: Unknown content object
- **WHEN** a user requests history for an identifier the ledger does not contain
- **THEN** the control plane reports that no such content object exists

#### Scenario: Placement shown with its location
- **WHEN** a user inspects a content object with a placement
- **THEN** the placement shows its destination identifier, provider, account and root path, and the destination name it was last placed under
