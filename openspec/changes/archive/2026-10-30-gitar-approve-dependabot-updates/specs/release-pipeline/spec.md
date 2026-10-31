# Spec Delta

## RENAMED Requirements

- FROM: `### Requirement: Bot-written pull requests are never auto-approved`
- TO: `### Requirement: Bot-written pull requests are auto-approved only as eligible Dependabot updates`

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
- **THEN** it opens against the default branch, `dev`, and carries the entry's labels; the
  version-update groups and schedule do not apply to it

#### Scenario: Target branch dropped
- **WHEN** an ecosystem entry loses `target-branch: dev`, an ecosystem is removed, a
  Compose ecosystem is added, or the schedule leaves weekly Friday Pacific
- **THEN** the unit suite fails naming the entry

### Requirement: A repository auto-approve policy admits only low-risk changes
`.gitar/config/approve.md` on `dev` MUST let Gitar approve only a pull request into `dev` whose
every changed file is documentation prose, a comment- or docstring-only code change, or an added
unit test, or that is an eligible Dependabot update. Any other change, an unavailable or above-low
risk assessment, or a criterion Gitar can't establish MUST leave the pull request unapproved.

#### Scenario: Documentation typo
- **WHEN** a human's pull request into `dev` corrects a typo in `docs/staging.md`, Gitar's review
  has no blocking finding and the risk assessment is low
- **THEN** the policy allows approval, provided the organization criteria allow it too

#### Scenario: One code change among docs
- **WHEN** a pull request changes `README.md` and one executable line under `src/`
- **THEN** it is not approved

#### Scenario: Risk not assessed
- **WHEN** no risk assessment is available for the pull request
- **THEN** it is not approved

### Requirement: Bot-written pull requests are auto-approved only as eligible Dependabot updates
The auto-approve policy MUST refuse a pull request opened by a bot or app account, or carrying a
commit authored or committed by one, so a bot's change merges only after a human reads it. The one
exception is an eligible Dependabot update. When an author's or committer's identity can't
be established, the pull request MUST NOT be approved.

#### Scenario: Pull request opened by @claude
- **WHEN** `claude[bot]` opens a documentation-only pull request into `dev`
- **THEN** it is not approved

#### Scenario: @claude pushes to a human's pull request
- **WHEN** a human's eligible pull request gains a commit authored by `claude[bot]`
- **THEN** it is not approved

#### Scenario: Dependabot update
- **WHEN** `dependabot[bot]` opens a pull request that is not an eligible Dependabot update
- **THEN** it is not approved

## ADDED Requirements

### Requirement: Only Python patches and minors and image patches from Dependabot are auto-approved
The auto-approve policy MUST admit a Dependabot version or security update only when Dependabot
wrote every commit and every dependency it names is a `uv` patch, a `uv` minor whose old and new
major are not `0`, or a `docker` patch, judged from the old and new versions in the diff. Any other
update, any `github-actions` or `dbos` update, or a version Gitar can't compare MUST leave it
unapproved, with the condition named.

#### Scenario: Grouped Python patches
- **WHEN** `dependabot[bot]` opens the `python-patches` group pull request into `dev`, whose commit
  metadata gives `version-update:semver-patch` for every dependency, and the review has no blocking
  finding and the risk assessment is low
- **THEN** the policy allows approval, provided the organization criteria allow it too

#### Scenario: GitHub Actions patch
- **WHEN** Dependabot's `actions-patches-and-minors` pull request moves an action from `v8.3.0` to
  `v8.3.1`, with no blocking finding and a low risk assessment
- **THEN** it is not approved, because a new action release runs in CI with the repository's
  secrets

#### Scenario: Python minor outside the declared range
- **WHEN** Dependabot opens a pull request whose metadata gives `version-update:semver-minor` for
  a `uv` dependency, its diff rewrites that dependency's specifier in `pyproject.toml` and changes
  `uv.lock`, with no blocking finding and a low risk assessment
- **THEN** the policy allows approval, provided the organization criteria allow it too, and the
  required checks are the only test of compatibility

#### Scenario: Minor of a 0.x package
- **WHEN** Dependabot opens a pull request that moves `fastapi` from `0.142.2` to `0.162.0`, typed
  `version-update:semver-minor`
- **THEN** it is not approved, and Gitar names the `0.x` version

#### Scenario: Patch of a 0.x package
- **WHEN** a `python-patches` pull request moves `fastapi` from `0.142.2` to `0.142.3`, with every
  other condition met
- **THEN** the policy allows approval, provided the organization criteria allow it too

#### Scenario: Image minor
- **WHEN** Dependabot opens a pull request whose metadata gives `version-update:semver-minor` for
  a `docker` image
- **THEN** it is not approved, and Gitar names the update type

#### Scenario: Major update
- **WHEN** any Dependabot pull request names a `version-update:semver-major` update
- **THEN** it is not approved

#### Scenario: Digest-only image refresh
- **WHEN** Dependabot changes a `FROM` image's digest under the same tag
- **THEN** it is not approved

#### Scenario: Security update within the limits
- **WHEN** Dependabot opens a security update whose metadata gives no `update-type`, and its
  `uv.lock` diff moves `pypdf` from `6.21.0` to `6.21.1`, with no blocking finding and a low risk
  assessment
- **THEN** the policy allows approval, provided the organization criteria allow it too

#### Scenario: Security update across a major
- **WHEN** a Dependabot security update moves `pillow` from `12.3.0` to `13.0.0`
- **THEN** it is not approved, and Gitar names the major

#### Scenario: DBOS update
- **WHEN** a Dependabot pull request includes a `dbos` patch or minor update
- **THEN** it is not approved, because a `dbos` bump must refresh the vendored skill in the same
  change

#### Scenario: Human commit on a Dependabot pull request
- **WHEN** an eligible Dependabot pull request gains a commit authored by any other account
- **THEN** it is not approved

### Requirement: Dependabot updates that dependabot.yml ignores are not auto-approved
The auto-approve policy MUST leave unapproved a Dependabot update, version or security, that
names a dependency update an `ignore` rule in `.github/dependabot.yml` excludes from version
updates, with the ignored update named. A security update MUST NOT get a path to auto-approval
that a version update of the same dependency does not have.

#### Scenario: Security update that dependabot.yml ignores
- **WHEN** a Dependabot security update moves `aioboto3` from `15.5.0` to `15.6.0`, a minor that
  `.github/dependabot.yml` ignores for version updates
- **THEN** it is not approved, because a new `aioboto3` minor must first be re-verified against
  RustFS and OVH

### Requirement: An auto-approved Dependabot update changes only version references
An eligible Dependabot pull request MUST change only the version references of the dependencies its
metadata names: `uv.lock` and those dependencies' specifiers in `pyproject.toml`, or the tag and
digest of `FROM` lines in `docker/Dockerfile`. Any other change, or a lock file that adds or
removes a package, MUST leave it unapproved, with the change named.

#### Scenario: Lock file adds a package
- **WHEN** a `uv` pull request's `uv.lock` diff adds a package that was not locked before
- **THEN** it is not approved, and Gitar names the added package

#### Scenario: Dockerfile change beyond a FROM line
- **WHEN** an `image-patches` pull request changes a `docker/Dockerfile` line other than a `FROM`
  image's tag and digest
- **THEN** it is not approved

#### Scenario: Large grouped update
- **WHEN** a grouped patch pull request changes more than 200 lines of `uv.lock`, and every changed
  line belongs to a named dependency's version, source or hashes
- **THEN** its size does not refuse it
