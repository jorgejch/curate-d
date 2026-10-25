# Spec Delta

## ADDED Requirements

### Requirement: A repository auto-apply policy bounds Gitar's code-review fixes
`.gitar/config/apply.md` on `dev` MUST state which code-review findings Gitar's auto-apply may
fix in this repository and which need human review. Eligibility MUST rest on evidence the fixer
can read (the spec requirement, the callers, the tests), never on a test run. A fix that changes
a protected data-integrity or durability guarantee MUST NOT be applied, and its finding MUST stay
open for human review.

#### Scenario: Local defect verified by inspection
- **WHEN** a finding reports a missing edge-case guard, and the spec requirement, the callers and
  the existing tests establish the correct result
- **THEN** the policy allows a focused fix with a regression test, provided the organization
  criteria allow it too

#### Scenario: Patch changes a protected guarantee
- **WHEN** a candidate patch moves a ledger write out of a `runtime/datasource.py` transaction,
  reorders copy, verification and registration, or replaces a deterministic workflow ID
- **THEN** the patch is not applied and the finding stays open for human review

### Requirement: An uncertain fix leaves its finding open
When a fix's validity, intended behaviour, scope or compliance with either the organization's or
the repository's criteria cannot be established, the policy MUST direct that no patch is applied
and the finding stays open, naming the concrete uncertainty and the evidence or decision that
would resolve it. It MUST NOT be guessed at or suppressed.

#### Scenario: Correctness cannot be established
- **WHEN** a finding's correctness depends on behaviour no code, test or spec in the repository
  establishes, such as a race against a database or provider
- **THEN** no patch is applied and the finding stays open, naming the uncertainty and what would
  resolve it

#### Scenario: Risk assessment unavailable
- **WHEN** a condition of the policy depends on a pull request's risk and no risk assessment is
  available
- **THEN** the condition is not met

### Requirement: The repository policy widens nothing the organization excludes
A fix MUST satisfy both the organization's auto-apply criteria and the repository policy. The
policy MUST NOT make eligible anything the organization criteria exclude; a wider eligibility
MUST be a separate organization-policy decision, never implied by the file.

#### Scenario: Organization exclusion
- **WHEN** a fix would change a migration, a CI workflow or a dependency version
- **THEN** it is not applied, whatever the repository policy says

### Requirement: The policy states its own reach
The policy MUST state that Gitar reads it from `dev`, that saving or merging it enables neither
auto-apply, approval nor merging, and that CI fixes, rule actions and explicit `gitar fix`
requests are outside its scope.

#### Scenario: Policy changed in a pull request
- **WHEN** a pull request edits `.gitar/config/apply.md`
- **THEN** that pull request's fixes are judged by the policy on `dev`, not by its own edit

#### Scenario: Policy merged
- **WHEN** the policy merges into `dev` and no pull request has auto-apply turned on
- **THEN** Gitar applies no fix on any pull request
