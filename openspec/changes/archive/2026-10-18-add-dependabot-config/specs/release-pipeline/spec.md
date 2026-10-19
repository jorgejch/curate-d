# Spec Delta

## ADDED Requirements

### Requirement: Dependency version updates are proposed against dev
The repository MUST hold a Dependabot configuration that checks `pyproject.toml` with
`uv.lock` (ecosystem `uv`, directory `/`), the workflow files (`github-actions`, `/`) and
`docker/Dockerfile` (`docker`, `/docker`) for version updates once a week, on Friday late
afternoon America/Los_Angeles, and opens every resulting pull request against `dev`. It MUST
NOT configure Compose images. A unit test MUST pin the ecosystems, the target branch and
the schedule.

#### Scenario: Weekly run
- **WHEN** Dependabot's scheduled run finds an outdated dependency in any of the three
  ecosystems
- **THEN** it opens a pull request whose base is `dev`, which the merge gate then checks
  like any other pull request

#### Scenario: Security update
- **WHEN** GitHub raises a Dependabot security update
- **THEN** it opens against the repository's default branch as GitHub shapes it; the
  version-update configuration's target, groups and ignores do not apply to it

#### Scenario: Target branch dropped
- **WHEN** an ecosystem entry loses `target-branch: dev`, an ecosystem is removed, a
  Compose ecosystem is added, or the schedule leaves weekly Friday Pacific
- **THEN** the unit suite fails naming the entry

### Requirement: Patch updates are grouped, minors and majors are not
Within version updates, the `uv` ecosystem MUST group patch updates into one pull request
and open a separate pull request per minor and per major update; the `github-actions`
ecosystem MUST group patch and minor updates into one pull request and open one per
major; the `docker` ecosystem MUST group patch updates into one and open one per minor and
per major. A digest-only image refresh never joins a group.
A unit test MUST pin each group's ecosystem and update types.

#### Scenario: Several Python patches
- **WHEN** three packages in `pyproject.toml` have a new patch release and one has a new
  minor
- **THEN** one pull request raises the three patches together and a second raises the
  minor alone

#### Scenario: Major update
- **WHEN** a dependency in any ecosystem has a new major release
- **THEN** it gets a pull request of its own, grouped with nothing

#### Scenario: Image digest refreshed under the same tag
- **WHEN** the registry publishes a new digest for a `FROM` image's current tag
- **THEN** either a pull request updating only that digest opens on its own, outside the
  patch group, or, where GitHub suppresses digest-only refreshes for versioned tags, no
  pull request opens

#### Scenario: Group loosened
- **WHEN** a group's `update-types` gains `major`, or the `uv` patch group gains `minor`
- **THEN** the unit suite fails naming the group

### Requirement: aioboto3 minors wait for backend re-verification
The `uv` ecosystem MUST ignore `version-update:semver-minor` for `aioboto3` and MUST NOT
ignore its patch updates, because a new minor must be re-verified against RustFS and OVH
before it is adopted. A unit test MUST pin the ignore.

#### Scenario: aioboto3 patch
- **WHEN** `aioboto3` publishes a patch within the pinned minor
- **THEN** it is proposed in the patch group like any other patch

#### Scenario: aioboto3 minor
- **WHEN** `aioboto3` publishes a new minor
- **THEN** no version-update pull request is opened for it

#### Scenario: Ignore removed or widened
- **WHEN** the `aioboto3` ignore entry is removed, or it gains `semver-patch`
- **THEN** the unit suite fails naming `aioboto3`

### Requirement: The python image stays on the targeted minor
The `docker` ecosystem MUST ignore `version-update:semver-minor` and
`version-update:semver-major` for the `python` image and MUST NOT ignore its patch
updates, so the image follows the Python minor the repository declares elsewhere
(`.python-version`, `requires-python`) rather than moving on its own. A unit test MUST
pin the ignore.

#### Scenario: Python patch release
- **WHEN** `python:3.12.<n+1>-slim-trixie` is published
- **THEN** a pull request moves both `python` stages to it

#### Scenario: Python minor release
- **WHEN** `python:3.13.<n>-slim-trixie` is current and the repository still declares 3.12
- **THEN** no version-update pull request moves the Dockerfile to it

#### Scenario: Ignore removed
- **WHEN** the `python` ignore entry loses `semver-minor` or `semver-major`
- **THEN** the unit suite fails naming `python`

### Requirement: Dockerfile base images are literal and digest-pinned
Every `FROM` line in `docker/Dockerfile` MUST name its image as a literal
`<name>:<tag>@sha256:<64 hex>`, with no `${...}` or `$VAR` interpolation in the image
reference, so Dependabot can parse and update it, and the build is reproducible down to
the image content. A unit test MUST pin the shape.

#### Scenario: Image updated by Dependabot
- **WHEN** Dependabot bumps a `FROM` image
- **THEN** the pull request rewrites the tag and the digest on that line, and the build
  needs no build argument to select the version

#### Scenario: Interpolated or unpinned FROM reintroduced
- **WHEN** a `FROM` line references `${PYTHON_VERSION}`, omits the tag, or omits the
  `@sha256:` digest
- **THEN** the unit suite fails naming the line
