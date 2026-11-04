# Design

## Context

See proposal.md, "Why". Observed state at planning (2026-11-03 PT, `dev` = `1b9ea4e`):

- SonarQube Cloud project `jorgejch_curate-d`, organization `jorge-haddad-oss`. Its main
  branch is `dev`, analysed at `1b9ea4e`. The open list holds 63 OPEN issues and 2 CONFIRMED
  issues. No security hotspot is TO_REVIEW.
- The local `sonar` CLI (sonarqube-cli 1.11.0) is logged in, but its code analysis returns
  `403 Forbidden` on this connection ("Vortex analysis is not available"). Only its secret
  scan runs. This was found at apply start.
- SonarQube for IDE in PyCharm 2026.2 runs in connected mode on this project, with the
  server's analyzers and quality profile. It stores its findings in an H2 database
  (`~/Library/Caches/JetBrains/PyCharm2026.2/sonarlint/storage/h2/sq-ide.mv.db`, table
  `KNOWN_FINDINGS`, one scope per open project). At apply start it held 64 of the 65 issues
  for this worktree. The missing one is `pythonbugs:S2583`, which only the server runs.
- A short-lived branch analysis in SonarQube Cloud reports only issues that the branch adds.
  It cannot show that an old `dev` issue is gone. Only the next `dev` analysis after the merge
  shows that.
- DBOS: `runtime/sweep.py` and `worker/main.py` hold no DBOS-decorated function. The sweep
  runs inside one step (`workflows/recovery.py`, `_recovery_sweep_step`) or from the operator
  CLI. `ingest_source`, `reconcile`, `scan_root` and `place_content` are `@DBOS.workflow`
  bodies. Replay and the sweep's cross-version fork depend on the order and names of their
  step, transaction, sleep and enqueue calls. Each build has its own explicit application
  version (`runtime/dbos_app.py`). The sweep forks in-flight workflows onto the new version, so
  a reordered call would break a fork.
- `checks.yml` and `claude.yml` already run `uv sync --locked --no-build`. In `uv.lock`, every
  package with an sdist also has wheels. `docker/Dockerfile` lines 38 and 46 omit the flag.
- `tests/unit/api/test_contract.py` validates the served OpenAPI document but does not compare
  it with a baseline.
- `ingest_source`, `reconcile` and `scan_root` have no unit test. Only the integration suite
  exercises them. `worker/main.py`'s `staging.supervisor_failed` path and a `CancelledError`
  out of `asyncio.run` have no test at all.

### Issue snapshot at planning (65)

| Stream | File | Rule × count, line |
| --- | --- | --- |
| 1 API | `api/app.py` | S8409 ×2 (136, 141) |
| 1 | `api/audit.py` | S8409 (147) |
| 1 | `api/configuration.py` | S8409 ×6 (181–227) |
| 1 | `api/connections.py` | S1192 `"/connections"` (107); S8409 ×3 (110, 120, 140); S1172 `provider` ×2 (208, 226) |
| 1 | `api/dry_runs.py` | S8409 ×3 (334, 350, 355) |
| 1 | `api/errors.py` | S7503 (55) |
| 1 | `api/jobs.py` | S8409 (222) |
| 1 | `api/review.py` | S8409 ×4 (157–185) |
| 1 | `api/throughput.py` | S8409 (116) |
| 2 Recovery, worker | `runtime/sweep.py` | S3776 `_recover_version` 33 (506), `run_recovery_sweep` 18 (1109); S1192 `"recovery_sweep.skipped"` (1005) |
| 2 | `worker/main.py` | S5754 (333); S3776 `prepare_services` 16 (491), `run` 18 (613) |
| 2 | `runtime/workflow_audit.py` | S3358 (101) |
| 3 Workflows | `workflows/ingest.py` | S3776 `ingest_source` 17 (245) |
| 3 | `workflows/reconcile.py` | S3776 `reconcile` 22 (309) |
| 3 | `workflows/scan.py` | S3776 `scan_root` 21 (266) |
| 4 Placement | `workflows/placement.py` | S1192 `"placement.superseded"` (313); S7503 `report` (176) |
| 4 | `placement/transfer.py` | S3776 `copy_and_collect` 26 (79) |
| 4 | `placement/verification.py` | S3776 `assess` 23 (126) |
| 4 | `placement/rclone.py` | S3776 `_object_info` 18 (96); S1192 `"operations/stat"` (100) |
| 4 | `placement/admission.py` | S2583 (58) |
| 5 Classification | `classification/deterministic.py` | S3776 21 (118); S3358 (130) |
| 5 | `classification/local.py` | S1172 `item` (282) |
| 5 | `classification/metadata.py` | S5843 regex 32 (38) |
| 5 | `rendering/render.py` | S3776 `render_with` 16 (125) |
| 5 | `content/sniff.py` | S1192 `"image/heic"` (16) |
| 5 | `observability/redaction.py` | S5869 (38) |
| 5 | `domain/base.py` | S7504 (64) |
| 6 Staging, ledger | `staging/object_store.py` | S5754 (572) |
| 6 | `staging/occupancy.py` | S5886 (613) |
| 6 | `staging/smoke.py` | S3776 `run_smoke_checks` 19 (184) |
| 6 | `staging/snapshots.py` | S7493 (263) |
| 6 | `staging/publish.py` | S3358 (93) |
| 6 | `providers/pcloud/files.py` | S1192 `"resolve folder"` (205) |
| 6 | `ledger/tables.py` | S1192 ×3: `"content_objects.id"` (113), `"size_bytes >= 0"` (141), `"'[]'::jsonb"` (595) |
| 7 Build, ops | `docker/Dockerfile` | S8541 ×2 CONFIRMED (38, 46); S6570 (60) |
| 7 | `ops/reset/reset-environment.sh` | S7679 ×2 (103, 142); S131 (220) |

All `src/` paths are under `src/curate_d/`.

Task 0.1 got the list again at apply start (2026-11-03 21:30 PT). It held the same 65 keys from
the same analysis, and `dev` was still `1b9ea4e`, so the table did not change.

## Goals / Non-Goals

**Goals:**
- Leave no OPEN or CONFIRMED issue on `dev` after the merge. The exceptions are issues that
  carry an Accepted or False-positive status with a written reason.
- Keep observable behavior, with the one exception that the `release-pipeline` delta specifies.
- Let 7 sub-agents work at once without one agent's half-finished edit turning another
  agent's suite red.

**Non-Goals:**
- Docstring or style clean-up beyond the flagged lines. Existing "The …" docstrings stay as
  they are. Only new helpers follow `CLAUDE.md`'s imperative summary.
- `S3StagingObjectStore._download` writes each part synchronously through the snapshot
  handle. That is a real event-loop cost, but SonarQube does not flag it. A separate change
  owns it.
- Changing SonarQube's quality profile, or adding `# NOSONAR` comments. A suppression in code
  hides the reason from the issue history, so the Accepted status with a comment replaces it.
- Moving the sweep's DBOS calls out of its step (a known follow-up in `CLAUDE.md`).

## Decisions

### 1. Scope is the live list at apply start

Task 0.1 reads the list again (`search_sonar_issues_in_projects`, project
`jorgejch_curate-d`, no branch, which means `dev`, statuses OPEN and CONFIRMED). Then it
compares the list with the snapshot above:

- An issue that is gone drops out of scope.
- A new issue in a file that a stream owns goes to that stream.
- A new issue in a file that no stream owns goes to stream 7, or to a new stream when it
  needs `src/` work. The orchestrator writes it into this table first.

Alternative: fix exactly the 65 issues. Rejected, because the user asked for every open
issue, and other merges land on `dev` while this change waits.

### 2. Fix in code, else Accept with a reason

A code fix must keep these unchanged:

- public and Protocol signatures, and keyword names that callers use
- raised exception types and messages
- log event names and their fields
- the order and `name` labels of DBOS step, transaction, sleep and enqueue calls
- workflow IDs
- the served OpenAPI document, byte for byte
- the emitted DDL

When a stream finds no fix that keeps all of them, it does not weaken the code. It records
the issue key, the guarantee and the evidence in its hand-back. The orchestrator adds the issue
to "Accepted issues" below. After the merge, the orchestrator sets the issue to Accepted (the
code is deliberate) or False positive (the analyzer is wrong), with the same reason as the
issue comment. A sub-agent never changes a SonarQube status.

Alternative: a code change that clears the rule at the cost of a guarantee. Rejected by the
user at planning.

### 3. Decided fixes for the non-mechanical issues

- **`worker/main.py:333` S5754.** If the code narrows the clause to `Exception`, a
  `CancelledError` from `stop()` escapes unrecorded, and `start()` then logs `staging.started`
  on a dead loop. Split the clause instead:
  - `except (Exception, asyncio.CancelledError)` records the failure and logs as today.
  - A second clause, `except BaseException`, records and logs the same way, then `raise`s.
  - Extract one recording helper, so that both clauses share it.

  The thread's excepthook ignores `SystemExit`. Only a `KeyboardInterrupt` raised inside the
  staging thread prints a traceback, and that is the only visible change. Add tests before the
  edit: `staging.supervisor_failed` after readiness, `CancelledError` before readiness, and
  `SystemExit` before readiness (`start()` must raise each one).
- **`staging/object_store.py:572` S5754.** Narrow to `except Exception`. Every
  `Exception` keeps its wrapping. `KeyboardInterrupt`, `SystemExit` and `GeneratorExit` then
  propagate unwrapped. That is correct, because wrapping `GeneratorExit` inside a coroutine is
  itself a defect.
- **`api/connections.py` S1172 `provider`.** `LedgerConnections` implements the
  `ConnectionsBackend` protocol, and `Provider` has one member. Dispatch with
  `match provider: case Provider.PCLOUD: …` and close it with `assert_never`. The parameter is
  then used, and a second provider becomes a mypy error at this point, not a silent pCloud
  authorization.
- **`classification/local.py:282` S1172 `item`.** No Protocol or dispatch table shares
  the signature. Drop the parameter, then update `classification/prepare.py:107`, the three
  unit-test calls and `tests/integration/test_local_ocr.py:42`.
- **`placement/admission.py:58` S2583.** This is a false positive: the tests reach both
  outcomes. A split into `small_share <= 0 or small_share >= 1` would accept NaN, which the
  code rejects today. First pin NaN rejection with a test. Then try
  `if not (budget_bytes > 0 and 0 < small_share < 1):`. The IDE does not run this rule.
  If the `dev` analysis after the merge still flags the line, keep the rewrite (it keeps
  behavior, NaN included) and mark the issue False positive.
- **`workflows/placement.py:176` S7503.** Make the progress callback synchronous: type it
  `Callable[[int, int], None]` in `placement/transfer.py`, call it without `await`, make
  `report` a plain `def`, and change `tests/integration/test_rclone.py:107` to match. The
  callback is local to the step, so DBOS pickles nothing new.
- **`api/errors.py:55` S7503.** Make `handle` a plain `def` and retype `Handler` as
  returning `JSONResponse`. Starlette runs a synchronous exception handler in its thread pool.
  The response is the same.
- **`domain/base.py:64` S7504.** The `list()` is a deliberate snapshot: a delete during
  direct iteration raises `RuntimeError`. Iterate over
  `self.__dict__.keys() - type(self).model_fields.keys()`. The set difference is built before
  the loop starts, and the delete order has no effect. This code unpickles DBOS checkpoints,
  so the existing `tests/unit/domain/test_base.py` cases must pass unchanged.
- **`staging/occupancy.py:613` S5886.** mypy already accepts `replace`. Bind an annotated
  local first. If the IDE check still flags it, construct `ReclaimReport(...)` explicitly.
- **`staging/snapshots.py:263` S7493.** Open with `await asyncio.to_thread(path.open, "xb")`,
  then use the handle in `with`. `_local_failures` still receives the `OSError`, because
  `to_thread` re-raises it in the caller.
- **`classification/metadata.py:38` S5843.** Compose `_PDF_DATE` from named sub-patterns
  that keep the seven capturing groups in their order. If the rule still fires, use
  `re.VERBOSE` with one commented line per group. Add pins for `D:2026Z` and
  `D:20260301120000-0500`.
- **`observability/redaction.py:38` S5869.** Under `(?i)`, `A-Z` duplicates `a-z`. Drop it.
- **`runtime/workflow_audit.py:101` S3358.** Replace the nested conditional with the
  module's own `_name(receiver)`. The behavior is the same.
- **S8409.** Remove only the flagged `response_model` arguments. Leave the 4 arguments that
  SonarQube does not flag. Decision 6 is the check.
- **S1192.** Use a module-private `Final` constant next to the module's other constants.
  Where every use is the same log call (`placement.superseded`), use a small logging helper
  instead. In `ledger/tables.py`, the constants hold identical values, so the DDL is
  unchanged.
- **Dockerfile and shell.** Add `--no-build` to both `uv sync` calls and quote
  `"tesseract-ocr=${TESSERACT_VERSION}"`. In `reset-environment.sh`, add `local steps=$1` in
  `local_helper` and `dev_helper`, and add `*) usage ;;` as the final `case` arm. Line 53
  already rejects every other value, so the new arm cannot run today.

### 4. Cognitive-complexity refactors extract plain helpers only

Each function gets helpers until it scores 15 or less. The planning analysis named the
blocks to extract. A stream may choose other cuts if it keeps these rules:

- A helper is never DBOS-decorated, and it opens no raw connection (`make lint`'s
  workflow audit).
- A workflow body calls the same steps, transactions, sleeps and enqueues, in the same
  order, with the same `name` labels. For example, `_reconcile_now_step` is still evaluated
  before the `record_reconcile` transaction, and the `if attempt_ids` guard still decides
  whether `list_workflows_async` runs.
- `runtime/sweep.py` keeps the order that its docstrings and `CLAUDE.md` state:
  1. confirm the successor
  2. re-read the original's status
  3. call `lost()` immediately before the fork
  4. verify after the fork
  5. call `lost()` before the cancel
  6. run the cancel even when the lock is lost
  7. verify that the status reads CANCELLED

  `sweep.fence` is still set before `recover_fenced`, and a fence failure still logs
  `recovery_sweep.fence_failed`.
- `worker/main.py` `run`'s shutdown order stays: join the reconciler, destroy or drain DBOS,
  call `staging.stop()`, then call `lease.release()` only when drained or never launched.
- A pure helper (for example, `reconcile`'s per-target planning or `scan_root`'s tally) gets
  isolated unit tests. A helper that calls steps is covered by the integration suite.

Alternative: suppress or accept the complexity issues on DBOS code. Rejected: every function
has cuts that keep call order, and the unit suite in `runtime/` covers the sweep's failure
branches well.

### 5. One Fable 5.1 sub-agent per stream, each in its own worktree

The apply orchestrator (the main session) starts one `Agent` per stream with
`model: "fable"` and `isolation: "worktree"`, all in one message. Each agent branches from the
change branch at the commit of task 0.2. It edits only the files that its stream owns in
tasks.md and commits one commit per task. It does not push, and it does not edit
`openspec/`. If it needs a file outside its ownership, it stops and reports. It does not edit
that file. The orchestrator then merges the stream branches into
`chore/resolve-sonar-issues` one at a time and runs `make check` after each merge. The files
are disjoint, so the merges are conflict-free.

Alternative: all agents in the shared worktree. Rejected, because `make test` in one agent
would run against another agent's half-finished edit.

### 6. Evidence per issue, per stream and for the whole change

- **Per issue:** add a pinning test first where the branch has none, and run it green on
  the unchanged code. Then make the fix, and run the module's unit tests. Then confirm, against
  the rule's description, that the flagged construct is gone. For S3776, the stream counts the
  cognitive complexity by SonarQube's rules and reports the count. No local tool runs the
  SonarQube rules inside a stream's worktree, so this is the stream's own check.
- **Per stream:** `make check`, which runs lint with the workflow audit, strict mypy and the
  unit suite with its 80% floor. Stream 1 also writes `/openapi.json` from
  `tests.unit.api.fakes.client()` and compares it with the baseline from task 0.2. The two
  documents must be byte-identical.
- **Whole change:**
  1. The IDE check (task 8.2): after the streams merge into this worktree, the user runs
     "Analyze All Project Files" from the SonarQube for IDE Report tab. "Analyze VCS Changed
     Files" is not enough: it reads only uncommitted files, and the merged fixes are
     committed. The plugin keeps the report in memory and does not write it to
     `KNOWN_FINDINGS` at once, so the user gives the orchestrator the report's findings. No
     finding of a planned rule and no new finding may remain in the changed files. This
     uses the server's rules, except `pythonbugs:S2583`.
  2. `make test-stack`, then the whole integration suite (`make test-integration`, fresh
     stack, per `CLAUDE.md`)
  3. `make image`
  4. CI on the pull request
  5. after the merge, the `dev` analysis

## Risks / Trade-offs

- [The IDE analysis and the cloud analysis can disagree, and the IDE does not run
  `pythonbugs:S2583`] → The cloud `dev` analysis
  after the merge is the final check (task 8.5). An issue that survives goes through Decision 2,
  and the orchestrator does not ship a second code change blind.
- [A refactor silently changes a workflow's call order, and in-flight workflows forked onto
  the new version then diverge] → Decision 4's rules, a review of each workflow-body diff
  against them (task 8.2), and the whole integration suite on a fresh stack.
- [A refactor changes a branch that no test covers] → Pin first (Decision 6). The three
  `worker/main.py` tests and the NaN test in Decision 3 are known gaps.
- [`--no-build` makes a future sdist-only dependency fail the image build] → This is the
  intended outcome (see the spec delta). The fix names the package with
  `--no-binary-package`, as the SonarQube rule describes, after a person reviews its build
  script.
- [7 worktrees each need their own `uv sync`] → This costs disk and a few minutes. It is
  accepted for isolation.
- [New issues land on `dev` between task 0.1 and the merge] → Task 8.5 re-lists them and
  routes each one through Decision 1 again. A new issue in code that this change did not touch
  is out of scope and stays open for the next run.

## Migration Plan

No data, schema or configuration migration. A dev image from the merge is a new DBOS
application version. The sweep forks in-flight workflows onto it, which Decision 4 keeps safe.
No drain is needed. Rollback: revert the merge commit. Accepted statuses in SonarQube can stay,
because they describe code that the revert restores.

## Accepted issues

None at planning. The orchestrator adds one row per issue that a stream cannot fix
(issue key, file and line, rule, guarantee kept, status Accepted or False positive).
