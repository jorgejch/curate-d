# Spec Delta

## ADDED Requirements

### Requirement: SonarQube does not analyse docs/ or openspec/
SonarQube Cloud MUST leave every file under `docs/` and `openspec/` out of its analysis, so it
raises no issue, security hotspot or secret there. Its analysis exclusions MUST be exactly
`docs/**` and `openspec/**`. A drift either way MUST fail the unit suite, naming the pattern
missing or added.

#### Scenario: Generated diagram page analysed
- **WHEN** a pull request changes the inline script of a page under `docs/diagrams/`
- **THEN** SonarQube Cloud's analysis of it reports no issue on that page

#### Scenario: Change artifact analysed
- **WHEN** a pull request adds a file under `openspec/changes/`
- **THEN** SonarQube Cloud's analysis of it reports no issue on that file

#### Scenario: Exclusion dropped
- **WHEN** the SonarQube analysis exclusions no longer list `docs/**` or `openspec/**`
- **THEN** the unit test pinning the analysed scope fails, naming the missing pattern

#### Scenario: Another directory excluded from analysis
- **WHEN** a pattern other than `docs/**` and `openspec/**` is added to the SonarQube analysis
  exclusions
- **THEN** the unit test pinning the analysed scope fails, naming the added pattern
