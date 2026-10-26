# Spec Delta

## MODIFIED Requirements

### Requirement: Collision handling

When two distinct content objects render to the same locator at the same destination identity, the system SHALL resolve the collision deterministically and SHALL NOT overwrite existing content. A locator occupied at another destination identity, under the same name or another, SHALL NOT count as occupied.

#### Scenario: Distinct content collides
- **WHEN** a rendered locator is already occupied by a different content object
- **THEN** a deterministic disambiguating suffix is applied
- **AND** the existing content at that locator is left unmodified

#### Scenario: Identical content collides
- **WHEN** a rendered locator is already occupied by the same content object
- **THEN** the existing placement is treated as satisfying the requirement
- **AND** no transfer is performed

#### Scenario: Locator occupied at another destination
- **WHEN** a rendered locator is occupied by a different content object at a destination with another provider, account or root path
- **THEN** no suffix is applied
- **AND** the content is placed at its plain locator

#### Scenario: Locator occupied at a name's previous account
- **WHEN** a rendered locator is occupied by a placement made under the same destination name before the policy pointed the name at another account
- **THEN** no suffix is applied at the new account
- **AND** the earlier placement is left unmodified
