# Proposal

## Why

Gitar reviews every pull request, and its auto-apply can push fixes for the findings it raises.
Its organization-wide criteria allow small, unambiguous fixes but say nothing about curate.d's
contracts or failure modes, so a fixer has no way to tell an ordinary local defect from a patch
that touches copy, verification and registration, ledger consistency, durable execution or
recovery. Useful findings beyond cosmetic cleanup stay unfixed, and nothing written down keeps a
fix from weakening the zero-data-loss guarantee (issue #62).

## What Changes

- Add `.gitar/config/apply.md`, a repository auto-apply policy written for curate.d. It
  separates eligible fixes, cases that need human review, and uncertain findings, with a compact
  allow/skip list of examples. It defines what "verified by inspection" means here (the spec
  requirement, the callers, the tests), since the fixer cannot run commands or tests, and states
  what happens when a fix's correctness or compliance is uncertain: no patch, the finding stays
  open with the concrete uncertainty named.
- The policy keeps the organization criteria binding: a fix must satisfy both, and the file
  widens nothing the organization excludes. It states that Gitar reads it from `dev`, so a pull
  request editing it cannot authorize its own fixes, that saving criteria enables neither
  auto-apply, approval nor merging, and that CI fixes, rule actions and explicit `gitar fix`
  requests are outside its scope.
- `CONTRIBUTING.md` points at the file where it describes pull requests.
- `sonar-project.properties` lists `.gitar/**` among the coverage and duplication exclusions:
  `tests/unit/infrastructure/test_sonar_properties.py` requires every tracked top-level
  directory but the measured ones to be listed there.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `release-pipeline`: adds requirements that a repository auto-apply policy, read from `dev`,
  bounds Gitar's code-review fixes on evidence the fixer can read; that a fix touching a
  protected guarantee, or one whose correctness can't be established, is not applied and the
  finding stays open; that the policy widens nothing the organization excludes; and that it
  states its own reach (read from `dev`, enables nothing, code-review findings only).

## Non-goals

- Widening eligibility into anything the organization criteria exclude (authentication, secrets,
  schemas and migrations, infrastructure, deployment and CI configuration, dependency versions,
  public APIs, business rules). That is a separate, explicit organization-policy decision.
- Turning auto-apply on for any pull request, or adding auto-approve, auto-merge or risk criteria
  (`approve.md`, `merge.md`, `risk.md`).
- Observing a live auto-apply run against the merged policy. It can only happen after the merge
  and stays open on the issue (`tasks.md`, Workflow follow-up).

## Impact

- New: `.gitar/config/apply.md`. Gitar applies it once it is on `dev`.
- `CONTRIBUTING.md`: one paragraph under "Branches, commits and pull requests".
- `sonar-project.properties`: `.gitar/**` added to `sonar.coverage.exclusions` and
  `sonar.cpd.exclusions`; without it `test_coverage_counts_src_only` and
  `test_duplication_counts_src_and_infrastructure_only` fail on the new directory. No code is
  measured differently.
- No code, test, migration or workflow changes. The Docker image copies named paths only, so the
  new directory stays out of it; the integration gate's declared paths don't cover it, so a pull
  request changing only the policy skips the integration suite, as for `docs/`.
