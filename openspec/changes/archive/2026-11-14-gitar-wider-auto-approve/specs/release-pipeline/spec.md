## RENAMED Requirements

- FROM: `### Requirement: Only Python patches and minors and image patches from Dependabot are auto-approved`
- TO: `### Requirement: Only bounded Dependabot updates are auto-approved`

## MODIFIED Requirements

### Requirement: A repository auto-approve policy admits only bounded changes
`.gitar/config/approve.md` on `dev` MUST let Gitar approve only a pull request into `dev` whose
every changed file is documentation prose, a comment- or docstring-only code change, an added unit
test, or an edit to `.editorconfig` or `.gitignore`, at low or medium risk, or that is an eligible
Dependabot update, at low or medium risk. Any other change, an unavailable or high risk assessment,
or a criterion Gitar can't establish MUST leave the pull request unapproved.

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
- **THEN** the policy allows approval, provided the organization criteria allow it too

#### Scenario: Documentation change at high risk
- **WHEN** a human's documentation pull request is otherwise eligible and its risk assessment is
  high
- **THEN** it is not approved, and Gitar names the risk

#### Scenario: Editor configuration
- **WHEN** a human's pull request adds or edits `.editorconfig` or `.gitignore` and nothing outside
  the eligible categories, with no blocking finding and a low or medium risk assessment
- **THEN** the policy allows approval, provided the organization criteria allow it too

#### Scenario: Docker build context
- **WHEN** a human's pull request edits `.dockerignore`
- **THEN** it is not approved, because it changes what the image build sees

#### Scenario: Eligible Dependabot update at medium risk
- **WHEN** an eligible Dependabot update has no blocking finding and its risk assessment is medium
- **THEN** the policy allows approval, provided the organization criteria allow it too

#### Scenario: Dependabot update at high risk
- **WHEN** a Dependabot update is otherwise eligible and its risk assessment is high
- **THEN** it is not approved, and Gitar names the risk

### Requirement: Bot-written pull requests are auto-approved only as eligible Dependabot updates
The auto-approve policy MUST refuse a pull request opened by a bot or app account, or carrying a
commit authored or committed by one, except an eligible Dependabot update. A human's commit made in
GitHub's web editor, committed as `web-flow`, is the human's. When the pull request author's
identity can't be established, or, outside an eligible Dependabot update, a commit author's or
committer's, the pull request MUST NOT be approved.

#### Scenario: Pull request opened by @claude
- **WHEN** `claude[bot]` opens a documentation-only pull request into `dev`
- **THEN** it is not approved

#### Scenario: @claude pushes to a human's pull request
- **WHEN** a human's eligible pull request gains a commit authored by `claude[bot]`
- **THEN** it is not approved

#### Scenario: Edit made in GitHub's web editor
- **WHEN** a human's otherwise eligible documentation pull request holds a commit the human
  authored in GitHub's web editor, with `web-flow` as its committer
- **THEN** that commit does not refuse it

#### Scenario: Gitar fix commit
- **WHEN** a human's otherwise eligible pull request gains a fix commit from Gitar
- **THEN** it is not approved, because a fix Gitar commits waits for a human

#### Scenario: Dependabot update
- **WHEN** `dependabot[bot]` opens a pull request that is not an eligible Dependabot update
- **THEN** it is not approved

### Requirement: Only bounded Dependabot updates are auto-approved
The auto-approve policy MUST admit a Dependabot version or security update only when Dependabot
opened it, no commit shown to Gitar is another account's, and each dependency the diff changes is a
`uv` patch, a `docker` patch, a patch of an action pinned to a commit SHA, or a `uv` or pinned
action minor whose old and new major are not `0`. Any other update, any `dbos` update, or a version
Gitar can't compare MUST leave it unapproved, with the condition named.

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
- **WHEN** Dependabot's `actions-patches-and-minors` pull request moves
  `anthropics/claude-code-action` from `1.0.268` to `1.0.271`, its diff changes only that action's
  `uses:` commit SHA, the trailing comment stays `# v1`, and the review has no blocking finding and
  a low or medium risk assessment
- **THEN** the policy allows approval, provided the organization criteria allow it too, with the
  update type taken from Dependabot's title

#### Scenario: GitHub Actions update of a tag-pinned action
- **WHEN** Dependabot moves `actions/checkout@v7` to `actions/checkout@v8`
- **THEN** it is not approved, because the action is pinned to a tag and the update is a major

#### Scenario: GitHub Actions update that changes more than the ref
- **WHEN** a `github-actions` pull request changes a workflow line other than a `uses:` ref and its
  trailing version comment
- **THEN** it is not approved

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
updates: in `uv.lock` and `pyproject.toml`, in `docker/Dockerfile` `FROM` lines, or in an action's
`uses:` ref and version comment. An updated package's dependency lists in `uv.lock` MAY change when
every name they list is locked before and after. Any other change, or a lock file that adds or
removes a package, MUST leave it unapproved, with the change named.

#### Scenario: Lock file adds a package
- **WHEN** a `uv` pull request's `uv.lock` diff adds a package that was not locked before
- **THEN** it is not approved, and Gitar names the added package

#### Scenario: Updated package gains an edge to a locked package
- **WHEN** a `python-patches` pull request moves `fastapi` from `0.162.0` to `0.162.4`, and
  `fastapi`'s lock entry gains `{ name = "anyio" }` in its `dependencies`, and `anyio` is locked
  before and after
- **THEN** the edge does not refuse it

#### Scenario: Dockerfile change beyond a FROM line
- **WHEN** an `image-patches` pull request changes a `docker/Dockerfile` line other than a `FROM`
  image's tag and digest
- **THEN** it is not approved

#### Scenario: Large grouped update
- **WHEN** a grouped patch pull request changes more than 200 lines of `uv.lock`, and every changed
  line belongs to an updated dependency's version, source or hashes
- **THEN** its size does not refuse it

## ADDED Requirements

### Requirement: GitHub Actions version updates wait out a cooldown
The `github-actions` entry of `.github/dependabot.yml` MUST set a `cooldown` of at least seven
days, so a new action release is proposed, and can be auto-approved, only after it has been public
for a week. No other entry sets a `cooldown` beyond Dependabot's default. The unit suite MUST fail
when the `github-actions` cooldown is missing or shorter.

#### Scenario: New action release
- **WHEN** an action used in a workflow publishes a patch
- **THEN** Dependabot's version update for it opens no sooner than seven days after the release

#### Scenario: Cooldown removed
- **WHEN** the `github-actions` entry's `cooldown` is removed or set below seven days
- **THEN** the unit suite fails
