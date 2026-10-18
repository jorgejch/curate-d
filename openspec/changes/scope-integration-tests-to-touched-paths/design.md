# Design

## Context

`integration.yml` triggers on `pull_request` to `dev` and `main` with no path filter, and
the ruleset requires its `Integration tests` check by name. A workflow-level `paths`
filter is the wrong tool: when a filter prevents the workflow from running, GitHub leaves
the required check pending and the pull request never merges. A separate detection job
with `needs:` is wrong too: a job whose dependency failed is skipped, GitHub counts a
skipped required check as passing, and the failed detection job is not required by name,
so a broken detection would open the gate silently. The detection therefore lives inside
the required job, where its failure is the job's failure.

The job's shape is pinned by `tests/unit/infrastructure/test_integration_workflow.py`:
`set(on) == {"pull_request"}`, the check name, step order, the teardown's
`if: always()`, the budget guard's exact `if`.

What the suite depends on, found by reading the repository rather than assumed:

| Reader | Paths |
| --- | --- |
| pytest collection, `tests.stubs` imports | `tests/integration/`, `tests/stubs/`, `tests/__init__.py` |
| the code under test | `src/` |
| `docker/Dockerfile` `COPY` | `pyproject.toml`, `uv.lock`, `src/`, `migrations/`, `alembic.ini`; also `README.md`, `LICENSE`, `NOTICE` |
| `.dockerignore` | shapes the build context |
| `compose.yaml` bind mounts | `infrastructure/postgres/initdb/`, `infrastructure/rustfs/initialize.sh`, `infrastructure/rustfs/policies/` |
| `compose.yaml` itself, `Makefile` (`test-stack`) | the stack and how CI starts it |
| `test_staging_backend_checks.py` | `infrastructure/rustfs/initialize.sh`, `compose.yaml`, `infrastructure/staging-compatibility.md` |
| `test_migrations.py` | runs Alembic from the repository root (`migrations/`, `alembic.ini`) |
| `uv sync` | `pyproject.toml`, `uv.lock`, `.python-version` |
| the job | `.github/workflows/integration.yml` |

Not depended on: `infrastructure/reset/`, `infrastructure/README.md`,
`compose.lifecycle.yaml` (hand-run `make lifecycle-check` only), `tests/unit/`,
`tests/local/`, `docs/`, `openspec/`, `.agents/`, `.claude/`, `.codex/`, `.github/`
outside the workflow, `sonar-project.properties`, the Markdown files at the root.

## Goals / Non-Goals

**Goals:**
- A pull request that touches none of the paths above merges without a suite run, with
  the required check reporting success.
- The gate fails closed: a detection that errors, or answers something other than yes or
  no, fails `Integration tests`.
- The declared set cannot silently fall behind the stack: a new mount, copy or test read
  fails the unit suite.

**Non-Goals:**
- Selecting a subset of the suite by path. The suite still runs whole or not at all
  (`test_the_whole_suite_runs_with_nothing_deselected` stays).
- Path-scoping `checks.yml`: lint, type check and the unit suite are fast and the
  SonarQube analysis wants every pull request.
- A `workflow_dispatch` or label escape hatch to force a run. A maintainer who wants the
  suite on a docs-only pull request can touch a declared file; add the hatch when that
  proves annoying.
- Changing the ruleset.

## Decisions

### D1. Detect inside the required job; gate the steps, not the job

The `integration` job keeps its name and steps. After checkout and the budget guard, a
step `id: changes` computes `suite=true|false`; every later step except `Remove the
stack` gets `if: steps.changes.outputs.suite == 'true'` (combined with its existing
condition where it has one: `always() && ...` on the evidence step, `failure() && ...` on
the logs). The job always runs and always reports, as a pass with no test run or as the
suite's result. Alternatives rejected: a workflow-level `paths` filter and a separate
`changes` job with `needs:` (both let the gate open on a detection failure, see Context).

### D2. Guard the detection's output

A step right after the detection fails the job with `::error::` when
`steps.changes.outputs.suite` is neither `true` nor `false`. The action sets the output on
every successful run, and a failed step already fails the job; the guard turns a silent
empty output, which `== 'true'` would read as "skip", into a visible failure. The
later steps use `== 'true'` rather than `!= 'false'` because, with the guard, the two
are equivalent and `== 'true'` reads as what it is.

### D3. A skip is explained in the step summary

When `suite` is `false`, one step appends to `GITHUB_STEP_SUMMARY` that the suite was not
run because none of the declared paths changed, and lists them. The existing
"Record staging evidence" step is gated on `suite == 'true'`, so it no longer writes
"nothing was verified" for a skip.

### D4. Changed files from GitHub's pull-request file list

The detection step is `dorny/paths-filter`, pinned by commit SHA like the Docker and uv
actions, on the default shallow checkout. On a `pull_request` event it reads the files the
pull request changes through the API, which is the list GitHub shows and the one meant by
"a PR with changes to"; it is stable across base updates because the ruleset requires the
branch to be up to date before merging. Alternative rejected: `git diff` against
`github.event.pull_request.base.sha`, which needs a deeper fetch and compares against a
base SHA that can lag the branch's actual merge base. The action needs `contents: read`,
already granted, and `pull-requests: read`, added to the workflow's `permissions`,
because it lists the changed files through the REST API; it outputs only the boolean.

### D5. Patterns are directory prefixes or exact files, declared in the workflow

The filter has one key, `suite`, whose patterns are each either `dir/**` or an exact
path, nothing else, so the unit test can match them with a prefix check instead of
re-implementing glob semantics. The list lives in `integration.yml`, the file the job
reads, and the test parses it from the YAML; a copy in a Python module was rejected as a
second place to keep in sync.

### D6. Pin by deriving the dependents, not only by asserting the list

`test_integration_workflow.py` gains tests that read `compose.yaml`'s `./` bind-mount
sources, `docker/Dockerfile`'s `COPY` sources (ignoring `--from=` stage copies and the
three packaging files), and the repository paths `tests/integration/*.py` read via
`_ROOT /` or `REPOSITORY_ROOT /`, and assert each is covered by a declared pattern,
failing with the path's name. A literal expected list is asserted too, so an accidental
widening (`**`) or narrowing shows as a diff. The three packaging files are excluded
because `hatchling` reads them for metadata only; nothing the suite asserts changes with
their content.

### D7. Pin the gate's shape

A test asserts: the detection step is a step of the `integration` job, before "Install
Tesseract"; the guard step follows it; every step after the guard except "Remove the
stack" has an `if` containing `steps.changes.outputs.suite == 'true'`; "Remove the stack"
keeps `always()`. The existing budget-guard test keeps its exact `if`, so that step stays
before the detection.

### D8. Docs say when the suite runs

`integration.yml`'s header, `checks.yml`'s header line about the integration suite, the
`Makefile` header and `check` target comment, `README.md:115`, `CONTRIBUTING.md:59` and
`docs/staging.md:253` currently say every pull request runs it. Each gets one clause:
when the pull request changes a path the suite depends on.

## Risks / Trade-offs

- [A dependency the derivation does not see, such as a test reading a path through a
  helper the grep misses] → the literal list in D6 is reviewed with the change; the
  derivation covers the mechanical sources (mounts, copies, `_ROOT /` reads). Add the
  helper's pattern to the derivation when one appears.
- [`dorny/paths-filter` is a new third-party action in the gate] → pinned by commit SHA
  with the version in a comment; Dependabot already watches `github-actions`; and its
  failure fails the required check rather than opening it (D1, D2).
- [A skipped-step job costs a runner start, about a minute, on every docs-only pull
  request] → accepted; it is what keeps the check reporting and the gate fail-closed.
- [A pull request that changes only `README.md` could break the image build (`COPY` of a
  missing file after a rename)] → `docker-publish.yml` builds on the `dev` push and would
  fail there; renaming a packaging file is rare and visible.

## Migration Plan

One pull request against `dev`. Nothing to roll back beyond reverting it: the ruleset,
check name and suite are unchanged.
