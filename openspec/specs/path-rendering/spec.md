# path-rendering Specification

## Purpose

Translates structured classification facts into a concrete destination locator using versioned templates, so that identical facts always produce identical paths and the whole library can be reorganized without reclassifying anything.

## Requirements

### Requirement: Deterministic rendering

Rendering SHALL be a pure function of the facts, the destination profile, and the template version. The same inputs SHALL always produce the same locator.

#### Scenario: Repeat render produces identical locator
- **WHEN** the same facts are rendered twice against the same destination profile and template version
- **THEN** both renders produce an identical locator

#### Scenario: Render does not consult external state
- **WHEN** a locator is rendered
- **THEN** the result does not depend on the current time, random values, or the existing contents of the destination

### Requirement: Versioned templates

Each rendered locator SHALL record the template version that produced it. The system SHALL support re-rendering existing content objects under a new template version without re-running classification.

#### Scenario: Template version recorded
- **WHEN** a locator is rendered
- **THEN** the template version used is recorded alongside the resulting placement

#### Scenario: Re-render under a new template
- **WHEN** the active template version changes
- **THEN** new locators can be derived for existing content objects from their stored facts alone
- **AND** no classification tier is invoked

### Requirement: Required facts enforcement

A template SHALL declare which facts it requires. Rendering SHALL fail explicitly when a required fact is absent rather than substituting a default or placeholder.

#### Scenario: Required fact present
- **WHEN** all facts required by the template are present
- **THEN** a locator is produced

#### Scenario: Required fact missing
- **WHEN** a fact required by the template is absent
- **THEN** rendering fails with an error identifying the missing fact
- **AND** no placement is created
- **AND** the content object is placed in the review queue

### Requirement: Destination capability handling

Rendering SHALL account for the capabilities of the target destination, including whether it supports nested hierarchy and what characters and lengths it permits in names.

#### Scenario: Hierarchical destination
- **WHEN** the destination supports nested folders
- **THEN** the rendered locator expresses the full hierarchy defined by the template

#### Scenario: Character or length limit exceeded
- **WHEN** a rendered component contains characters the destination disallows, or exceeds its length limit
- **THEN** the component is transformed into a permitted form by a documented, deterministic rule
- **AND** the transformation is recorded so the original value remains recoverable

### Requirement: Collision handling

When two distinct content objects render to the same locator, the system SHALL resolve the collision deterministically and SHALL NOT overwrite existing content.

#### Scenario: Distinct content collides
- **WHEN** a rendered locator is already occupied by a different content object
- **THEN** a deterministic disambiguating suffix is applied
- **AND** the existing content at that locator is left unmodified

#### Scenario: Identical content collides
- **WHEN** a rendered locator is already occupied by the same content object
- **THEN** the existing placement is treated as satisfying the requirement
- **AND** no transfer is performed
