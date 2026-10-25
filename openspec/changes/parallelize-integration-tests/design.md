# Design

## Context

See proposal.md for why. What shapes the how:

- The ruleset requires the check `Integration tests` by name (confirmed against the
  repository's `default` ruleset). A matrix job's checks are named `<job name> (<group>)`,
  so the required name must sit on a job that is not the matrix.
- GitHub counts a skipped required check as passing. Today's gate therefore keeps the
  change detection inside the required job (archived change
  `scope-integration-tests-to-touched-paths`). A sharded run needs more than one job, so
  the required job becomes an aggregate that runs on every outcome and decides from the
  other jobs' results. Fail-closed moves from "detection is a step of the required job" to
  "the required job fails on anything but an explicit success or an expected skip".
- The suite needs the whole `make test-stack` stack (PostgreSQL with PostGIS, RustFS with
  its provisioning, rclone), not a bare `services:` PostgreSQL. Every runner already gets
  its own Docker daemon, so the published ports (5433, 9100, 9101, 5572) are local to the
  runner VM and cannot collide across jobs. The issue's "no fixed host ports" concern does
  not arise between runners; inside one runner there is one stack.
- pytest 9.1 has no native `--shard`. `pytest-xdist` is in the dev group for the unit
  suite only. `pytest-split` 0.11 (pytest 5 to 9) splits a collection into `--splits N
  --group G` by recorded durations; both its algorithms are deterministic given an
  identical collection order on every shard, which holds here (same commit, same flags).
  `least_duration` sorts by duration, bin-packs greedily, then restores collection order
  inside each group; `duration_based_chunks` cuts contiguous chunks. Without a durations
  file it falls back to equal counts, which skews this suite: a few tests dominate it.
- `tests/integration` has no module- or class-scoped fixture; the only wider ones are the
  package-scoped version isolation and the session-scoped `Settings`, both cheap. The
  migration tests create a throwaway database per test. Nothing found depends on another
  module having run first, which the local shard runs in the tasks confirm empirically.
- `test_staging_contract.py` records `sdk_checksum_headers` as a JUnit property, and the
  "Record staging evidence" step counts the contract and backend-check modules from
  `integration.xml`. With a split, those modules may land on any shard, so the evidence
  must be built from every shard's report.
- The boot check exists to prove the image built from this commit boots on a migrated
  database and applies its schedules. It ran after the suite on the same database only
  because a booted worker's persisted schedules would tick inside the test process on
  the same database. On its own runner there is no test process to disturb.
- `tests/unit/infrastructure/test_integration_workflow.py` pins the workflow's shape in
  detail (job name, step order, conditions, teardown, the declared paths, the Makefile
  recipe). It is rewritten against the topology below.
- `jobs.<id>.strategy` may read `needs` and `vars` but not `env`, and expressions have
  no range function, so a job must emit the group list for the matrix.

## Goals / Non-Goals

**Goals:**
- Every integration test runs exactly once per selected run, on a shard with its own
  stack, and the gate fails if that cannot be shown.
- The required check keeps its name and keeps reporting on every pull request, with the
  same skip behaviour for pull requests that touch none of the suite's inputs.
- Shards are balanced by measured duration, deterministic across reruns, and reproducible
  locally by group number.
- The shard count changes without a commit, with a default of four in the workflow.

**Non-Goals:**
- Parallelism inside a shard (`-n`): the one-database, one-serial-process invariant
  stays.
- Splitting the unit suite or `checks.yml`.
- Sharing the built image or the installed dependencies between runners beyond the
  caches already in use (uv cache, GitHub Actions build cache).
- Refreshing `.test_durations` automatically from CI (no commits from workflows).

## Decisions

### Topology: `plan`, `shard`, `boot`, `integration`

| Job id | Name | Needs | Runs when | Does |
| --- | --- | --- | --- | --- |
| `plan` | `Plan the integration run` | | always on the PR | budget guard; checkout; `dorny/paths-filter` detection (`id: changes`), the undecided-detection guard; the shard-count guard; when selected: install uv, `uv sync --locked --no-build`, collect the suite and count it. Outputs `suite`, `splits`, `groups`, `collected`. |
| `shard` | `Integration shard ${{ matrix.group }} of ${{ needs.plan.outputs.splits }}` | `plan` | `needs.plan.outputs.suite == 'true'` | `strategy: fail-fast: false; matrix: group: ${{ fromJSON(needs.plan.outputs.groups) }}`. Checkout, Tesseract, uv, sync, `make test-stack` (same env flags as today), the suite for its group, upload `integration-report-<group>` (`if: always()`), stack logs on failure, `docker compose down --volumes` always. No image build: the suite's services are image-only. |
| `boot` | `Application boot` | `plan` | `needs.plan.outputs.suite == 'true'` | Checkout, uv, sync, Buildx, build and load `curate-d:dev` (same cache), `make test-stack`, start `api worker-io worker-cpu`, wait for `worker.ready`, `schedule_check`, stack logs on failure, teardown always. |
| `integration` | `Integration tests` | `plan`, `shard`, `boot` | `always()` | The gate: a first step that reads the three results and fails closed; when selected, download `integration-report-*`, check the shards ran the whole suite, write the merged staging evidence; when not selected, write the "Suite not selected" summary. |

Why an aggregate rather than requiring each shard: the ruleset lists check names, and the
set would change with the shard count. Why `plan` is separate from the gate: the matrix
needs its group list before it starts, and the gate must run after the shards.

**Gate rule** (first step of `integration`, a shell script with `::error::` and `exit 1`):

- `needs.plan.result != 'success'` → fail, naming the plan (covers a failed or cancelled
  detection or shard-count guard; a cancelled run fails, which the superseding run
  replaces).
- `needs.plan.outputs.suite == 'false'` → `shard` and `boot` must both be `skipped`,
  else fail; then the skip summary, and pass.
- `needs.plan.outputs.suite == 'true'` → `shard` and `boot` must both be `success`,
  else fail naming the job and its result. A matrix job's `result` is `failure` when any
  group failed, so one red shard fails the gate.
- Anything else (`suite` unset) → fail.

**Whole-suite check** (`integration`, selected only): exactly `splits` report files are
present, named `integration-<group>.xml` for groups 1..splits, and the sum of their
`<testsuite tests="...">` counts equals `collected`. A missing report (a shard that never
reached pytest) or a count mismatch (a shard that collected differently) fails the gate
even when every shard job succeeded. `collected` comes from the plan's
`pytest --collect-only -q tests/integration -m integration --staging-backend=rustfs --no-cov`,
counting the node-id lines (those containing `::`); JUnit `tests` counts skipped tests, as
collection does, so the two agree.

**Staging evidence**: the existing Python script, now over every `integration-*.xml`,
plus one line per shard (tests, failures, errors, skipped, `time`) so an imbalance is
visible from the summary. "The suite did not produce a result" stays for zero reports.

### Splitting: `pytest-split`, `least_duration`, committed durations

The shard command:

```
uv run --locked --no-build pytest tests/integration
  --no-cov -m integration --staging-backend=rustfs
  --splits ${{ needs.plan.outputs.splits }} --group ${{ matrix.group }}
  --splitting-algorithm least_duration
  --junitxml=integration-${{ matrix.group }}.xml -o junit_family=legacy
```

- `--splits` reads the plan's output, not `strategy.job-total`: a second matrix dimension
  added later would double `job-total` and silently drop half the tests, whereas `splits`
  and `groups` come from one plan step and cannot disagree.
- `least_duration` over `duration_based_chunks`: better balance, and it keeps collection
  order within a shard, which is the order the suite runs in today. The contiguous
  alternative would keep whole modules together, which nothing here needs.
- `.test_durations` at the repository root (pytest-split's default path), committed,
  generated with `--staging-backend=rustfs` so the parametrised node ids (`[rustfs]`)
  match CI's. `make test-durations` runs the whole suite with `--store-durations
  --clean-durations` against a `make test-stack` stack and rewrites the file. Drift only
  affects balance: a test without a recorded duration gets pytest-split's estimate, and a
  recorded test that no longer exists is ignored. Regenerate it when the summary shows
  shards drifting apart or when a slow test is added or removed.
- Alternative considered: generating durations in CI and committing them back. Rejected:
  no workflow in this repository commits, and balance is not worth a write token.

### Shard count: repository variable with a workflow default

The plan job reads `${{ vars.INTEGRATION_SHARDS || 4 }}` into a step's environment,
refuses anything that is not an integer from 1 to 32 (an unbounded count would start that
many runners), and emits `splits=N` and `groups=[1,...,N]` to `GITHUB_OUTPUT`. Changing
the variable in the repository settings changes the next run; the default stays
reviewable in the workflow. Alternatives: a `workflow_dispatch` input (the workflow runs
on `pull_request` only), an `env` constant (needs a commit to change, and `strategy`
cannot read `env` anyway).

Locally, `make test-integration GROUP=2` runs shard 2 of `SPLITS` (default 4) with the
same algorithm and durations, and with `--staging-backend=rustfs`: the split is a function
of the collection, and CI's collection is the RustFS-only one (the OVH parametrisation
would otherwise add node ids CI never sees and shift every group). Without `GROUP` the
target runs the whole suite as today, both backends. A unit test pins the Makefile's
`SPLITS ?= 4` and the workflow's `|| 4` to each other, so the two defaults cannot drift
apart.

### Declared paths

`.test_durations` joins the declared set: a change to it changes which tests each shard
runs, so the run that validates it must happen. `EXPECTED_DECLARED_PATHS` and the skip
summary's path list gain it. Nothing else the suite reads changes.

### Timeouts and concurrency

`plan` 10 minutes, `shard` 15 (a quarter of a nine-minute suite plus two to three minutes
of setup, times three), `boot` 15, `integration` 5. The workflow-level `concurrency`
group `integration-${{ github.ref }}` with `cancel-in-progress` is unchanged: it cancels
the whole run, every job included.

### Actions

`actions/upload-artifact@v7` and `actions/download-artifact@v8` by major tag, as
`actions/checkout@v7` is (first-party; Dependabot's `github-actions` ecosystem tracks
them). `dorny/paths-filter` stays pinned by SHA. The download uses
`pattern: integration-report-*` with `merge-multiple: true` so the reports land in one
directory.

### Pin tests

`test_integration_workflow.py` gains job accessors (`_plan()`, `_shard()`, `_boot()`,
`_gate()`) and asserts, per the spec's scenarios: the required name on `integration`
only; the trigger unchanged; `plan` holds the detection after the budget guard, the
guard right after it, and every later `plan` step conditioned on the detection; the
shard-count guard's bounds and default; `shard` and `boot` gated at the job level on
`needs.plan.outputs.suite == 'true'`, with `needs: plan`; `fail-fast: false` and the
`fromJSON(needs.plan.outputs.groups)` matrix; the shard command (whole suite, nothing
deselected, `--no-cov`, RustFS only, `--splits ${{ needs.plan.outputs.splits }}`,
`--group ${{ matrix.group }}`, `least_duration`); `make test-stack` with CI's flags in
both stack jobs; no image build and no application service in `shard`; migrations before
the application start, the worker wait and the schedule check in `boot`; teardown
`always()` in both; the upload `if: always()`; the gate `if: always()`, `needs` all three,
its first step naming all three results and exiting non-zero, the download and the
whole-suite check after it; `persist-credentials: false` on every checkout; the
durations file present, a non-empty JSON object whose keys start with
`tests/integration/` and whose values are positive numbers; `SPLITS ?= 4` equal to the
workflow's default. The declared-path pins keep working unchanged apart from the new
entry.

## Risks / Trade-offs

- [A test depends on another module having run earlier in the process] → the shard runs
  in the tasks (each group against a fresh stack) must all pass before the workflow
  lands; a failure there is a test to fix, not a reason to pin modules together.
- [Collection differs between the plan and a shard] → both run the same commit, flags and
  lockfile; the whole-suite check catches a mismatch, and the failing shard's log shows
  pytest-split's `Running group G/N (M tests)` line.
- [Per-runner setup dominates] → the shards skip the image build; Tesseract, uv sync and
  image pulls remain per runner (about two minutes). Wall time is bounded below by that,
  not by the shard count. Fewer, larger shards are one variable away if the runner
  minutes matter more than the wall time.
- [Stale durations] → balance degrades, correctness does not. The per-shard summary line
  makes it visible; `make test-durations` fixes it in one local run.
- [A shard's report is missing because the job died before pytest] → the gate's
  whole-suite check fails on the count of report files, independently of the job result.
- [Boot no longer sees a database the suite exercised] → it never relied on that: the
  check is that the image boots on a migrated schema and registers its own version as
  latest, which a fresh database gives more cleanly (no test versions promoted and
  restored first).
- [Cost: seven jobs per selected run] → about the same runner minutes as today's single
  job plus the per-runner setup repeated; accepted for the wall-time gain, and the shard
  count can be lowered.
