## RENAMED Requirements

- FROM: `### Requirement: A repository auto-approve policy admits only low-risk changes`
- TO: `### Requirement: A repository auto-approve policy admits only bounded changes`

## MODIFIED Requirements

### Requirement: A repository auto-approve policy admits only bounded changes
`.gitar/config/approve.md` on `dev` MUST let Gitar approve only a pull request into `dev` whose
every changed file is documentation prose, a comment- or docstring-only code change, or an added
unit test, at low risk, or that is an eligible Dependabot update, at low or medium risk. Any other
change, an unavailable or higher risk assessment, or a criterion Gitar can't establish MUST leave
the pull request unapproved.

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

#### Scenario: Documentation change at medium risk
- **WHEN** a human's documentation pull request is otherwise eligible and its risk assessment is
  medium
- **THEN** it is not approved, and Gitar names the risk

#### Scenario: Eligible Dependabot update at medium risk
- **WHEN** an eligible Dependabot update has no blocking finding and its risk assessment is medium
- **THEN** the policy allows approval, provided the organization criteria allow it too

#### Scenario: Dependabot update at high risk
- **WHEN** a Dependabot update is otherwise eligible and its risk assessment is high
- **THEN** it is not approved, and Gitar names the risk

### Requirement: Bot-written pull requests are auto-approved only as eligible Dependabot updates
The auto-approve policy MUST refuse a pull request opened by a bot or app account, or carrying a
commit authored or committed by one, so a bot's change merges only after a human reads it. The one
exception is an eligible Dependabot update. When the pull request author's identity can't be
established, or, outside an eligible Dependabot update, a commit author's or committer's, the pull
request MUST NOT be approved.

#### Scenario: Pull request opened by @claude
- **WHEN** `claude[bot]` opens a documentation-only pull request into `dev`
- **THEN** it is not approved

#### Scenario: @claude pushes to a human's pull request
- **WHEN** a human's eligible pull request gains a commit authored by `claude[bot]`
- **THEN** it is not approved

#### Scenario: Dependabot update
- **WHEN** `dependabot[bot]` opens a pull request that is not an eligible Dependabot update
- **THEN** it is not approved

### Requirement: Only Python patches and minors and image patches from Dependabot are auto-approved
The auto-approve policy MUST admit a Dependabot version or security update only when Dependabot
opened it, no commit shown to Gitar is another account's, and each dependency whose version the
diff changes is a `uv` patch, a `uv` minor whose old and new major are not `0`, or a `docker` patch,
judged from the diff's versions. Any other update, any `github-actions` or `dbos` update, or a
version Gitar can't compare MUST leave it unapproved, with the condition named.

#### Scenario: Grouped Python patches
- **WHEN** `dependabot[bot]` opens the `python-patches` group pull request into `dev`, whose diff
  moves every dependency by a patch, and the review has no blocking finding and the risk assessment
  is low or medium
- **THEN** the policy allows approval, provided the organization criteria allow it too

#### Scenario: Image patch at medium risk
- **WHEN** `dependabot[bot]` moves the `astral-sh/uv` image from `0.13.6` to `0.13.7`, the diff
  changes one `FROM` line's tag and digest, the review has no blocking finding and the risk
  assessment is medium
- **THEN** the policy allows approval, provided the organization criteria allow it too

#### Scenario: Commit data not shown
- **WHEN** an otherwise eligible Dependabot update's commit authors, committers and messages are
  not shown in Gitar's review
- **THEN** their absence does not refuse it

#### Scenario: GitHub Actions patch
- **WHEN** Dependabot's `actions-patches-and-minors` pull request moves an action from `v8.3.0` to
  `v8.3.1`, with no blocking finding and a low risk assessment
- **THEN** it is not approved, because a new action release runs in CI with the repository's
  secrets

#### Scenario: Python minor outside the declared range
- **WHEN** Dependabot opens a pull request whose diff moves a `uv` dependency by a minor from a
  major of `1` or more, rewrites that dependency's specifier in `pyproject.toml` and changes
  `uv.lock`, with no blocking finding and a low or medium risk assessment
- **THEN** the policy allows approval, provided the organization criteria allow it too, and the
  required checks are the only test of compatibility

#### Scenario: Minor of a 0.x package
- **WHEN** Dependabot opens a pull request that moves `fastapi` from `0.142.2` to `0.162.0`
- **THEN** it is not approved, and Gitar names the `0.x` version

#### Scenario: Patch of a 0.x package
- **WHEN** a `python-patches` pull request moves `fastapi` from `0.142.2` to `0.142.3`, with every
  other condition met
- **THEN** the policy allows approval, provided the organization criteria allow it too

#### Scenario: Image minor
- **WHEN** Dependabot opens a pull request whose diff moves a `docker` image's tag by a minor
- **THEN** it is not approved, and Gitar names the update type

#### Scenario: Major update
- **WHEN** any Dependabot pull request's diff moves a dependency by a major
- **THEN** it is not approved

#### Scenario: Digest-only image refresh
- **WHEN** Dependabot changes a `FROM` image's digest under the same tag
- **THEN** it is not approved

#### Scenario: Security update within the limits
- **WHEN** Dependabot opens a security update whose `uv.lock` diff moves `pypdf` from `6.21.0` to
  `6.21.1`, with no blocking finding and a low or medium risk assessment
- **THEN** the policy allows approval, provided the organization criteria allow it too

#### Scenario: Security update across a major
- **WHEN** a Dependabot security update moves `pillow` from `12.3.0` to `13.0.0`
- **THEN** it is not approved, and Gitar names the major

#### Scenario: DBOS update
- **WHEN** a Dependabot pull request includes a `dbos` patch or minor update
- **THEN** it is not approved, because a `dbos` bump must refresh the vendored skill in the same
  change

#### Scenario: Human commit on a Dependabot pull request
- **WHEN** Gitar's review shows that an eligible Dependabot pull request gained a commit authored
  by any other account
- **THEN** it is not approved

#### Scenario: Description disagrees with the diff
- **WHEN** a Dependabot pull request's title or description names a dependency or a version that
  its diff contradicts
- **THEN** it is not approved, and Gitar names the disagreement

### Requirement: An auto-approved Dependabot update changes only version references
An eligible Dependabot pull request MUST change only the version references of the dependencies it
updates: `uv.lock` and those dependencies' specifiers in `pyproject.toml`, or the tag and digest of
`FROM` lines in `docker/Dockerfile`. Any other change, or a lock file that adds or removes a
package, MUST leave it unapproved, with the change named.

#### Scenario: Lock file adds a package
- **WHEN** a `uv` pull request's `uv.lock` diff adds a package that was not locked before
- **THEN** it is not approved, and Gitar names the added package

#### Scenario: Dockerfile change beyond a FROM line
- **WHEN** an `image-patches` pull request changes a `docker/Dockerfile` line other than a `FROM`
  image's tag and digest
- **THEN** it is not approved

#### Scenario: Large grouped update
- **WHEN** a grouped patch pull request changes more than 200 lines of `uv.lock`, and every changed
  line belongs to an updated dependency's version, source or hashes
- **THEN** its size does not refuse it
