# Spec Delta

## MODIFIED Requirements

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
- **THEN** it opens against the default branch, `dev`; the version-update groups and schedule do
  not apply to it

#### Scenario: Target branch dropped
- **WHEN** an ecosystem entry loses `target-branch: dev`, an ecosystem is removed, a
  Compose ecosystem is added, or the schedule leaves weekly Friday Pacific
- **THEN** the unit suite fails naming the entry

## ADDED Requirements

### Requirement: Dependabot pull requests are not handed to Gitar's auto-apply
The Dependabot configuration MUST NOT give any entry the `gitar-managed` label, so no Dependabot
version or security update opens with Gitar's auto-apply on. A Gitar fix commit would end the
update's eligibility for auto-approval, and its fix would be a code change that needs its own
reviewed pull request. If a Dependabot pull request gets the label by other means, the
auto-approve policy MUST still judge it by its commits alone.

#### Scenario: Version update opens
- **WHEN** Dependabot opens a version update for any of the three ecosystems
- **THEN** the pull request carries Dependabot's default labels and not `gitar-managed`

#### Scenario: Security update opens
- **WHEN** Dependabot opens a security update
- **THEN** the pull request does not carry `gitar-managed`

#### Scenario: Gitar commit on a labelled Dependabot pull request
- **WHEN** someone adds `gitar-managed` to a Dependabot pull request and Gitar pushes a fix to it
- **THEN** the auto-approve policy refuses it, because a commit is not authored by
  `dependabot[bot]`

### Requirement: The owner's pull requests open with Gitar's auto-apply on
A pull request that the repository owner opens from a branch in this repository MUST get the
`gitar-managed` label when it is opened, whether or not its commits carry a bot's
`Co-Authored-By` trailer. A pull request by any other author, or from a fork, MUST NOT get it.
If adding the label fails, the labelling run MUST fail visibly, MUST NOT block the merge, and the
pull request stays without the label until someone adds it by hand.

#### Scenario: Owner opens a pull request
- **WHEN** the repository owner opens a pull request into `dev` from a branch in this repository
- **THEN** it carries `gitar-managed` within the labelling run, and Gitar's auto-apply is on for
  it

#### Scenario: Commits co-authored by a bot
- **WHEN** the owner opens a pull request whose commits carry `Co-Authored-By: Claude`
- **THEN** it gets `gitar-managed` like any other pull request the owner opens

#### Scenario: Another author
- **WHEN** `dependabot[bot]`, `claude[bot]` or another account opens a pull request
- **THEN** the labelling run adds no label

#### Scenario: Pull request from a fork
- **WHEN** a pull request's head branch is in a fork
- **THEN** the labelling run adds no label

#### Scenario: Labelling fails
- **WHEN** the label cannot be added, for example because the token lacks permission or the
  GitHub API returns an error
- **THEN** the labelling run fails with the API's error, the required checks are unaffected, and
  the pull request has no `gitar-managed` label
