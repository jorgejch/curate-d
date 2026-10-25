# curate.d auto-apply criteria

Repository criteria for Gitar's auto-apply of code-review findings. curate.d moves people's
files between clouds and may release the originals, so a fix must never risk losing data, even
on a crash, a retry or a replay. Project rules: `CLAUDE.md`, `CONTRIBUTING.md`,
`openspec/config.yaml`, and the contracts in `openspec/specs/`.

## Reach of this file

- **Both policies bind.** A fix is applied only if it satisfies the organization criteria and
  this file. This file clarifies what is eligible here and adds safeguards; it never overrides
  an organization exclusion. Wider eligibility needs a separate, explicit change to the
  organization policy; nothing here implies one.
- **Read from `dev`.** Gitar uses the copy on the default branch. A pull request that edits this
  file cannot authorize its own fixes.
- **It enables nothing.** Saving or merging criteria turns on neither auto-apply, approval nor
  merging. Auto-apply is enabled per pull request; auto-approve and auto-merge have their own
  criteria.
- **Code-review findings only.** CI fixes, CI recovery, rule actions and explicit `gitar fix`
  requests have separate controls and are not governed here.

## Organization exclusions, as they appear here

Never eligible, whatever the rest of this file says: authentication, secrets, schemas and
migrations, infrastructure, deployment and CI configuration, dependency versions, public APIs,
business rules. In this repository that includes, at least:

- `migrations/`, and table or constraint definitions in `src/curate_d/ledger/tables.py`.
- `.github/`, `docker/`, `infrastructure/`, `compose.yaml`, `compose.lifecycle.yaml`, `Makefile`,
  `sonar-project.properties`, `alembic.ini`.
- Dependency pins in `pyproject.toml` and `uv.lock`; the vendored `.agents/skills/dbos-python`
  and its `skills-lock.json` pin, which only a `dbos` upgrade refreshes.
- Provider authorization and credential handling (`src/curate_d/providers/authorization.py`,
  `src/curate_d/providers/crypto.py`), `.env*`, and log redaction
  (`src/curate_d/observability/redaction.py`).
- HTTP routes and their request and response models in `src/curate_d/api/`.
- Business rules: placement and retention policy (`src/curate_d/domain/policy.py`,
  `src/curate_d/reconciliation/retention.py`), classification tiers and taxonomy.

If it is unclear whether an exclusion covers a patch, treat the finding as uncertain (below).

## What "verified by inspection" means here

The fixer runs no commands or tests, so eligibility never depends on `make check`, the
integration suite or a fresh passing run. The correct behaviour must be established by reading:

1. The requirement and scenario the code implements, in `openspec/specs/<capability>/spec.md`.
2. Every caller of the changed code, and what they do with its result or exception.
3. The types (`mypy --strict` is the contract) and the docstring.
4. The existing tests that pin the behaviour, under `tests/unit/` or `tests/integration/`.

The patch description cites that evidence. If the evidence conflicts or is missing, the finding
is uncertain. A fix whose correctness needs an execution to show it (a race, a lock, database,
provider, rclone or S3 behaviour, a library's undocumented semantics) is uncertain.

## Eligible fixes

Focused corrections to existing intended behaviour, each satisfying the organization criteria:

- **Local defects**: edge cases, wrong guards and comparisons, collection handling (empty,
  duplicate, ordering the caller relies on), error propagation, where the correct result is
  established as above.
- **Typing and validation** that preserve the established input and output contract. Never add
  `Any`, `cast`, `# type: ignore`, a broad `except`, or a permissive default just to silence a
  finding.
- **Resource cleanup and diagnostics** that preserve ownership, cancellation
  (`asyncio.CancelledError` propagates), error types and confidentiality: no credential, token
  or signed URL reaches a log or message. Errors carry the operation, the resource and the cause.
- **Small performance fixes** only when equivalence is clear and ordering, concurrency, retries,
  caching and persistence are unchanged.
- **Tests**: a regression test that fails on the defect and passes with the fix, isolated as
  `CONTRIBUTING.md#tests` requires (no database, network, filesystem, rclone or model calls in
  unit tests); a correction to demonstrably wrong setup or expectations against an established
  contract. Never change, loosen, skip or delete an assertion to match broken behaviour.
- **Documentation and docstrings** that make them accurate, following `CLAUDE.md#docstrings`;
  mechanical lint fixes in eligible code.

Keep each patch to the finding: no unrelated cleanup, reformatting, import reordering or broad
refactoring. Severity alone neither authorizes a risky fix nor excludes a safe, verified one.

## curate.d safeguards: human review required

Skip the patch and leave the finding open when it changes any of these, wherever the code sits:

- **Copy, verify, register**: a placement is registered only after its copy is verified, and
  source content is never modified, moved or deleted (`src/curate_d/placement/`,
  `src/curate_d/workflows/placement.py`).
- **Verification evidence** and the verification ladder (`src/curate_d/placement/verification.py`,
  `src/curate_d/placement/transfer.py`).
- **Idempotency** and deterministic workflow IDs (`src/curate_d/runtime/workflow_ids.py`).
- **Atomic ledger writes**: every in-workflow ledger mutation goes through a datasource
  transaction from `src/curate_d/runtime/datasource.py`, with its audit entry in the same
  transaction; never through a step, an unmanaged session or a direct connection.
- **Audit history**: append-only, never deduplicated by workflow or step.
- **Explicit failure and quarantine** when a value is uncertain: a patch must not replace a raised
  error or a quarantine with a guess, a default or a skip.
- **DBOS replay and checkpoints**: what is a workflow or a step, step order and count, workflow
  inputs and step outputs (they are pickled; see `CLAUDE.md#dbos`).
- **Transaction boundaries, leases and fencing** (`src/curate_d/runtime/lease.py`,
  `src/curate_d/runtime/version_lease.py`, `src/curate_d/runtime/schedule_authority.py`,
  `src/curate_d/staging/leases.py`).
- **Schedules** (`src/curate_d/workflows/registry.py`, `src/curate_d/worker/schedules.py`).
- **Recovery and retention** (`src/curate_d/runtime/sweep.py`, `src/curate_d/runtime/retention.py`,
  `src/curate_d/workflows/recovery.py`, `src/curate_d/workflows/retention.py`, staging cleanup and
  eviction).
- **Destructive provider or storage operations**: delete, move, overwrite, bucket operations.
- **Repository restrictions**: private library internals (an underscore module or attribute, or
  a copy of private logic or SQL) and any reference to another repository on the machine.

Judge the patch's effect, not its directory: a corrected docstring or log message in
`src/curate_d/runtime/sweep.py` can be eligible; a one-line change elsewhere that alters one of
these guarantees is not.

## When uncertain

When validity, intended behaviour, scope, or compliance with either policy is uncertain: apply
no patch, leave the finding open, and state the concrete uncertainty and the evidence or decision
that would resolve it. Never guess, silently suppress the finding, or weaken a safeguard.

- A fix that needs a product decision, a non-trivial OpenSpec change (anything beyond the simple
  fixes `CLAUDE.md#changes` allows: a typo, a one-line bug fix, a doc correction), or an
  alteration of a protected guarantee is deferred to a normal reviewed change.
- A condition here that depends on a pull request's risk is not met when no risk assessment is
  available.

## Reporting

- Name the evidence the fix rests on: spec requirement, callers, tests.
- Say plainly that no test or check was run by the fixer; CI and review decide merge readiness.
  Never claim an unrun check passed.
- Never weaken tests, coverage, type checking, lint or security checks, or bypass required CI.

## Examples

Allow:

- A helper indexes the first element of a list its callers can pass empty, and the spec scenario
  names the result for no input: add the guard, and a unit test of the empty case.
- An exception is re-raised without the resource it concerns, against `CONTRIBUTING.md`'s error
  rule: wrap it with the operation, resource and cause, keeping the exception type callers catch.
- A unit test asserts a value that contradicts the spec scenario it cites, while the code follows
  the scenario: correct the expectation.
- A missing regression test for a defect fixed in the same pull request: add one that fails
  without the fix, using fakes at the boundary.
- A docstring in `Returns the...` form, or one naming a parameter that no longer exists: rewrite
  it per `CLAUDE.md#docstrings`. `docs/recovery.md` naming a function that was renamed: fix
  the reference.

Skip, leave open:

- Moving a ledger write into an `@DBOS.step`, splitting an audit entry from its decision's
  transaction, swapping a deterministic workflow ID for a random one, reordering a placement's
  copy and registration, or retrying a failed verification: protected guarantees.
- Catching an exception in a workflow to continue past an uncertain value: quarantine is the
  contract.
- Any edit under `migrations/` or `.github/workflows/`, or a version bump in `pyproject.toml`:
  organization exclusions.
- Changing a test's expectation to match the code when the spec says otherwise.
- A reported race between two lease holders, whose correctness rests on PostgreSQL advisory-lock
  or DBOS behaviour nothing in the repository pins: uncertain. Say which behaviour needs
  establishing and which test or spec would establish it.
- Caching a provider listing or batching ledger writes for speed: changes caching or persistence.
