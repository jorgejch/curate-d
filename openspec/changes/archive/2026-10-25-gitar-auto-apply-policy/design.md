# Design

## Context

Gitar's auto-apply fixes code-review findings on a pull request when it is turned on for that
pull request. Each candidate patch is checked against the organization's criteria and, when the
repository has one, against `.gitar/config/apply.md`; a fix must satisfy both. Gitar reads the
repository file from the default branch (`dev`), and the fixer cannot run shell commands or
tests, so a criterion that requires a passing test run is never met. A risk-dependent condition
is not satisfied by an unavailable risk assessment. Criteria select code-review fixes only: CI
fixes, CI recovery, rule actions and explicit `gitar fix` requests work independently of them,
and auto-approve and auto-merge have their own criteria. (Gitar's documentation, "Auto-apply" and
"Repository customization", read 2026-10-25.)

The organization criteria allow small, unambiguous fixes and exclude authentication, secrets,
schemas and migrations, infrastructure, deployment and CI configuration, dependency versions,
public APIs and business-rule changes.

curate.d's own rules for what a correct change is live in `CLAUDE.md` (DBOS departures,
docstrings, repository boundaries), `CONTRIBUTING.md` (tests, conventions), `openspec/config.yaml`
and the specs under `openspec/specs/`.

## Goals / Non-Goals

**Goals:**
- A concise policy, specific to curate.d, that an AI fixer can apply: what is eligible, what needs
  a human, what to do when unsure, with examples.
- Eligibility that rests on evidence the fixer can read, never on a test run it cannot make.

**Non-Goals:**
- Widening the organization criteria, or enabling auto-apply, approval or merging anywhere.
- Restating `CLAUDE.md`: the policy points at it.

## Decisions

- **Judge a patch by its effect, not its directory.** A docstring fix in `runtime/sweep.py` is
  as safe as one anywhere else; a one-line change in a helper that reorders a copy and its
  registration is not. The policy lists the guarantees and the mechanisms that carry them, with
  where they live, and asks whether a patch changes them. Path lists alone would either block
  harmless fixes in half of `src/` or miss a semantic change made from outside it.
- **Organization exclusions are restated, not reinterpreted.** The policy maps them to where they
  sit in this repository (`migrations/`, `.github/`, `pyproject.toml`, `src/curate_d/api/` ...)
  so the fixer recognises them, and treats any doubt about whether an exclusion covers a patch as
  an uncertain finding. It cannot narrow them by mapping: the organization's wording governs.
- **"Verified by inspection" is defined.** The evidence a fix may rest on is the spec requirement
  the code implements, its callers, its types and the existing tests that pin the contract, read
  in the repository. A fix whose correctness needs a run (a race, a database or provider
  behaviour, a library's undocumented semantics) is uncertain by definition.
- **Uncertainty skips the patch and keeps the finding.** The skip names the concrete uncertainty
  and what evidence or decision would resolve it. Silent suppression would lose the finding;
  guessing would risk the guarantee the project exists for.
- **The policy's own mechanics are stated in it.** That it is read from `dev`, that it enables
  nothing, and its scope are in the file, so a reader of the file alone doesn't mistake a merged
  policy for an enabled feature.
- **Requirements in `release-pipeline`.** The capability already holds the repository's
  tooling contracts beyond CI proper (Dependabot, SonarQube scope). A new capability for four
  short requirements would scatter them.
- **List `.gitar/**` in the Sonar exclusions rather than special-case it in the test.** The test
  derives the expected lists from the tracked top-level directories; the properties file's own
  comment says to add a new top-level directory there.

## Risks / Trade-offs

- [The fixer misreads natural-language criteria and applies a patch the policy meant to skip] →
  The organization criteria still apply, every pushed fix lands on a pull request that needs the
  required checks and review to merge, and the policy is explicit enough to be checked against.
  Observing a live run after the merge is the follow-up.
- [The policy drifts from `CLAUDE.md` as departures are added] → It points at `CLAUDE.md` rather
  than copying it, and lists guarantees, not every departure; a change that adds a guarantee
  updates the policy in the same pull request.

## Migration Plan

Merge into `dev`; Gitar uses the file from its next review run. Rollback is reverting the commit,
after which the organization criteria apply alone.

## Parallelism

One work stream. The policy, its pointer in `CONTRIBUTING.md` and the Sonar exclusion are small,
and the Sonar edit must land with the new directory or the unit suite fails, so the change isn't
split.
