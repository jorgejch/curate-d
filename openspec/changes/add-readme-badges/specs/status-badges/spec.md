# Spec Delta

## Purpose

Shows the health of `dev`, the default and deployed branch, and the project's licence and
release status at the top of the README. Each badge comes from the service that owns the
signal and is kept consistent with the repository's own metadata.

## ADDED Requirements

### Requirement: The README shows dev's health badges
The README MUST show four health badges, in this order: Checks (`checks.yml`), CodeQL
(GitHub's default setup), SonarCloud's quality gate and SonarCloud's coverage. Each
badge's image URL MUST name the `dev` branch. A unit test MUST pin the badges, their
order and their branch.

#### Scenario: Badges complete and pinned
- **WHEN** the README holds the four health badges in order, each image URL naming `dev`
- **THEN** the unit suite passes

#### Scenario: A badge loses its branch
- **WHEN** a health badge's image URL names no branch, or a branch other than `dev`
- **THEN** the unit suite fails, naming the badge and the branch found

#### Scenario: A badge is missing or out of order
- **WHEN** one of the four health badges is absent or out of order
- **THEN** the unit suite fails, naming the badges it found in place of the expected ones

### Requirement: A health badge links to dev's view
Each health badge MUST link to the view that owns its signal, filtered to `dev`: for
Checks and CodeQL, the workflow's runs on `dev`; for the quality gate and coverage,
SonarCloud's new-code summary of `dev`.

#### Scenario: Links name dev
- **WHEN** every health badge's link names `dev`
- **THEN** the unit suite passes

#### Scenario: A link points elsewhere
- **WHEN** a health badge links to a view that names no branch, or a branch other than
  `dev`
- **THEN** the unit suite fails, naming the badge and the link

### Requirement: SonarCloud badges name the analysed project
Every SonarCloud badge image and link, and the branch-list link, MUST name the project key
that `sonar-project.properties` declares as `sonar.projectKey`.

#### Scenario: Project key matches
- **WHEN** every SonarCloud URL in the README names the `sonar.projectKey` value
- **THEN** the unit suite passes

#### Scenario: Project key drifts
- **WHEN** a SonarCloud URL names any other project, or `sonar.projectKey` is changed
  without the README
- **THEN** the unit suite fails, naming the URL and the expected key

### Requirement: Other branches are reached through SonarCloud's branch list
The README MUST link SonarCloud's branch list for the project next to the badges, as the
way to see branches other than `dev`. It MUST NOT hold a health badge for any other
branch.

#### Scenario: Branch-list link present
- **WHEN** the README links `sonarcloud.io/project/branches_list` for the project
- **THEN** the unit suite passes

#### Scenario: Branch-list link missing
- **WHEN** the branch-list link is absent
- **THEN** the unit suite fails, naming the missing link

### Requirement: The License badge matches the declared licence
The README MUST show one License badge whose value is the SPDX identifier `pyproject.toml`
declares as `project.license`, and which links to `LICENSE`.

#### Scenario: Licence matches
- **WHEN** the License badge reads the `project.license` value
- **THEN** the unit suite passes

#### Scenario: Licence drifts
- **WHEN** `project.license` changes and the badge does not, or the badge is missing
- **THEN** the unit suite fails, naming the badge's value and the declared licence

### Requirement: The Status badge matches the README's status note
The README MUST show one Status badge whose value equals the status the README's
"Status:" note states.

#### Scenario: Status agrees
- **WHEN** the note reads "Status: pre-release." and the badge reads "pre-release"
- **THEN** the unit suite passes

#### Scenario: Status drifts
- **WHEN** the note and the badge state different statuses, or either is missing
- **THEN** the unit suite fails, naming both values
