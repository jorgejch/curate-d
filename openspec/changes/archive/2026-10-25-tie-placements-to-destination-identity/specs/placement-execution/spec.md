# Spec Delta

## MODIFIED Requirements

### Requirement: Idempotent execution

Executing the same placement more than once SHALL NOT produce duplicate destination content or duplicate ledger records. A placement is the same placement when its content object and its destination identity are the same; two destination identities under one name are two placements.

#### Scenario: Repeated execution after success
- **WHEN** a placement that already completed is executed again
- **THEN** no additional copy is performed
- **AND** the existing placement record is unchanged

#### Scenario: Repeated execution after failure
- **WHEN** a placement that previously failed is executed again
- **THEN** the attempt proceeds
- **AND** any partial destination content from the failed attempt is superseded rather than duplicated

#### Scenario: Same content to two accounts under one name
- **WHEN** content placed under a destination name at one account is placed under the same name after the policy pointed it at another account
- **THEN** a second placement is recorded, at the new account
- **AND** the first placement is unchanged
- **AND** the two copies run as different work and neither collapses into the other

### Requirement: Dry-run evaluation

The system SHALL support evaluating placements without performing them. In dry-run mode the system SHALL report the destination locator and the verification level it expects to achieve, and SHALL NOT write to any destination, modify any source, or record a placement as present. Locators SHALL be proposed per destination identity, as a real placement claims them.

#### Scenario: Dry run reports proposed placement
- **WHEN** a placement is evaluated in dry-run mode
- **THEN** the proposed destination locator is reported
- **AND** no content is written to the destination

#### Scenario: Dry run leaves no durable placement state
- **WHEN** a dry-run evaluation completes
- **THEN** no placement is recorded as present
- **AND** a subsequent real execution proceeds as though the dry run had not occurred

#### Scenario: Dry run surfaces rendering failure
- **WHEN** a placement cannot be rendered during dry-run evaluation
- **THEN** the failure and its reason are reported
- **AND** the evaluation continues with the remaining content objects rather than aborting

#### Scenario: Dry run against two accounts under one template
- **WHEN** a dry run evaluates two destinations on one provider in different accounts with the same root path and template
- **THEN** each content object is proposed at its plain locator at both
- **AND** a locator proposed at one does not push the other's proposal to a suffixed locator

#### Scenario: Dry run after a name moved accounts
- **WHEN** a dry run evaluates a destination name the policy pointed at another account than existing placements under that name were made at
- **THEN** those placements neither count as already present nor occupy locators at the new account
- **AND** no destination record is written for the dry run
