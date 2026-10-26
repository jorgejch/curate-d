# Spec Delta

## ADDED Requirements

### Requirement: The Claude workflow grants tools by an explicit allow-list
The Claude workflow MUST name every tool Claude may use beyond the action's defaults, and Claude
MUST be refused any tool it does not name. The list MUST NOT contain unrestricted shell access
(`Bash`, `Bash(*)`), `gh api`, or `gh pr merge`.

#### Scenario: Tool not on the list
- **WHEN** Claude, running from the workflow, attempts a shell command no allow-list entry
  matches
- **THEN** the command is refused and Claude reports that it could not run it

#### Scenario: A blanket entry is added
- **WHEN** the workflow's allow-list gains unrestricted `Bash`, a `gh api` entry or a
  `gh pr merge` entry
- **THEN** the unit suite fails naming the entry

### Requirement: The Claude workflow can run the repository's checks
The Claude workflow MUST install the locked dependencies as the checks job does and allow
`make lint`, `make typecheck`, `make test` and `make format`, so Claude can run the checks a
pull request must pass before it pushes. When a check fails or can't run, Claude MUST say so
rather than report the change as passing.

#### Scenario: Claude prepares a commit
- **WHEN** `@claude` is asked to change code and push it
- **THEN** it can run lint, the type check and the unit suite in the job, and reports their
  outcome

#### Scenario: Dependency install changes
- **WHEN** the workflow's dependency install no longer runs `uv sync --locked --no-build`
- **THEN** the unit suite fails

### Requirement: The Claude workflow's MCP servers are read-only
Every MCP server the Claude workflow configures MUST be read-only: SonarQube's server MUST run
in its read-only mode from an image pinned by digest, and only read tools of each server MAY be
allowed. A server whose secret is unset MUST be unavailable for that run without failing it.

#### Scenario: SonarQube write tool
- **WHEN** Claude attempts to change a SonarQube issue's or hotspot's status
- **THEN** the tool is not allowed, and the server refuses writes in read-only mode

#### Scenario: Logfire secret missing
- **WHEN** the Logfire secret is not set and `@claude` is mentioned
- **THEN** the run proceeds without the Logfire tools

#### Scenario: Read-only mode dropped
- **WHEN** the SonarQube server's read-only setting is removed, its image loses the digest, or a
  non-read tool of either server is allowed
- **THEN** the unit suite fails

### Requirement: A repository auto-approve policy admits only low-risk changes
`.gitar/config/approve.md` on `dev` MUST let Gitar approve only a pull request into `dev` whose
every changed file is documentation prose, a comment- or docstring-only code change, or an added
unit test. Any other change, an unavailable or above-low risk assessment, or a criterion Gitar
can't establish MUST leave the pull request unapproved.

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

### Requirement: Bot-written pull requests are never auto-approved
The auto-approve policy MUST refuse a pull request opened by a bot or app account, or carrying a
commit authored or committed by one, so a bot's change merges only after a human reads it. When
an author's or committer's identity can't be established, the pull request MUST NOT be approved.

#### Scenario: Pull request opened by @claude
- **WHEN** `claude[bot]` opens a documentation-only pull request into `dev`
- **THEN** it is not approved

#### Scenario: @claude pushes to a human's pull request
- **WHEN** a human's eligible pull request gains a commit authored by `claude[bot]`
- **THEN** it is not approved

#### Scenario: Dependabot update
- **WHEN** `dependabot[bot]` opens a pull request
- **THEN** it is not approved

### Requirement: The auto-approve policy cannot authorize itself
The policy MUST refuse any pull request that changes `.gitar/`, and MUST state that it enables
neither approval nor merging, that it narrows the organization criteria and never widens them,
and that approval leaves every required check in place.

#### Scenario: Pull request edits the criteria
- **WHEN** a pull request changes `.gitar/config/approve.md`
- **THEN** it is not approved, whichever copy of the criteria Gitar read

#### Scenario: Policy merged
- **WHEN** the policy merges into `dev` and auto-approve is off in Gitar's settings
- **THEN** Gitar approves no pull request
