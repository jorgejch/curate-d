# Tasks

Work streams. The apply orchestrator (the main session) owns group 0, group 8 and every
`openspec/` edit. Groups 1–7 are the parallel streams. Each stream is one Fable 5.1 sub-agent
(`model: "fable"`, `isolation: "worktree"`, design.md decision 5). The orchestrator starts all
seven in one message after group 0. Each stream owns only the files listed on its heading,
plus their unit tests under the matching `tests/unit/<area>/`. The streams touch disjoint
files and do not depend on each other. Group 8 waits for all seven.

Every fix task follows design.md decision 6:

1. If no test covers the branch, add a pinning test and run it green on the unchanged code.
2. Make the fix.
3. Run the module's unit tests.
4. Run `sonar analyze --file <path> -p jorgejch_curate-d --format json`. It must show no issue
   of the target rule at the site and no new issue in the file.

An issue that has no fix within design.md decision 2 is reported back with its evidence. It is
not forced. Each stream ends on a green `make check`.

## 0. Scope and baseline (orchestrator, before the streams)

- [x] 0.1 Re-list the open `dev` issues (`search_sonar_issues_in_projects`, project `jorgejch_curate-d`, statuses OPEN and CONFIRMED) and compare them with design.md's snapshot. Drop the issues that are gone, and assign each new issue to the stream that owns its file, or to a stream that design.md decision 1 names. Update the snapshot table and the stream headings below. Verify that the table's count equals the list's `paging.total`.
- [x] 0.2 On `chore/resolve-sonar-issues`, commit the change's `openspec/` artifacts. Run `make check` and record it green. Write the served `/openapi.json` (from `tests.unit.api.fakes.client()`) to the scratchpad as the baseline. Verify that the file is valid JSON and that `make check` exits 0.

## 1. API stream — `src/curate_d/api/{app,audit,configuration,connections,dry_runs,errors,jobs,review,throughput}.py`

- [ ] 1.1 Remove the flagged redundant `response_model` arguments (S8409, 21 at planning) and leave the arguments that are not flagged. Verify that the served `/openapi.json` is byte-identical to the 0.2 baseline (`cmp`) and that `uv run pytest -n auto tests/unit/api` passes.
- [ ] 1.2 Add a private `Final` constant for `"/connections"` in `api/connections.py`, and use it for the router prefix and both cookie paths (S1192). Verify with the cookie-path assertions in `tests/unit/api` and with `sonar analyze` on the file.
- [ ] 1.3 Dispatch `LedgerConnections.begin` and `complete` on `provider` with `match … case Provider.PCLOUD` and `assert_never` (S1172 ×2). Verify that `uv run mypy` passes, that the existing pCloud authorization tests pass unchanged, and that `sonar analyze` reports no S1172 in the file.
- [ ] 1.4 Make the `_answer` handler synchronous and retype `Handler` to match (S7503). Verify that the `NotFoundError`, `RejectedError` and `UnavailableError` problem-response tests in `tests/unit/api` pass unchanged.
- [ ] 1.5 Run `make check` in the stream's worktree, and run `sonar analyze` on every file the stream changed. Verify that both are clean, then hand back the commit list and any issue for design.md "Accepted issues".

## 2. Recovery and worker stream — `src/curate_d/runtime/sweep.py`, `src/curate_d/worker/main.py`, `src/curate_d/runtime/workflow_audit.py`

- [ ] 2.1 Add `_SKIPPED_EVENT: Final = "recovery_sweep.skipped"` and use it at its four log calls (S1192). Verify that `uv run pytest tests/unit/runtime/test_sweep.py tests/unit/runtime/test_sweep_execution.py` passes. The tests assert the literal event name.
- [ ] 2.2 Extract the fork branch of `_recover_version` into a plain async helper, and the integrity-conflict check into a second helper, so that `_recover_version` scores 15 or less (S3776, 33). Keep design.md decision 4's seven-step order. Verify that every test in `tests/unit/runtime/test_sweep_execution.py` passes unchanged and that `sonar analyze` reports no S3776 in the file.
- [ ] 2.3 Extract the version partition and the fenced recovery of `run_recovery_sweep` into plain helpers (S3776, 18). Keep the registry read before any write, and set `sweep.fence` before `recover_fenced`. Verify with the same suite. The fence-failure and promotion-in-progress tests must pass unchanged.
- [ ] 2.4 Add tests for `_serve_forever` with no code change: `staging.supervisor_failed` after readiness, and `CancelledError` and `SystemExit` before readiness, where `start()` must raise each one. Verify that they pass on the current code in `tests/unit/worker/test_staging.py`.
- [ ] 2.5 Split `_serve_forever`'s `except BaseException` into `except (Exception, asyncio.CancelledError)` and a re-raising `except BaseException`. Both clauses use one recording helper (S5754, design.md decision 3). Verify that the 2.4 tests and `tests/unit/worker/test_staging.py` pass and that `sonar analyze` reports no S5754 in the file.
- [ ] 2.6 Move the service construction of `prepare_services`'s nested `create()` into a module-level helper (S3776, 16). Verify that `tests/unit/runtime/test_services.py` and the `prepare_services` failure test in `tests/unit/runtime/test_worker_main.py` pass.
- [ ] 2.7 Extract `run`'s shutdown `finally` into helpers that keep the order: reconciler join, DBOS destroy or drain, `staging.stop()`, then lease release only when drained or never launched (S3776, 18). Verify that `tests/unit/runtime/test_worker_main.py` passes unchanged, including the unfinished-drain and lease-lost cases.
- [ ] 2.8 Replace the nested conditional in `workflow_audit.py` with `_name(receiver)` (S3358). Verify that `uv run pytest tests/unit/runtime/test_workflow_audit.py` passes and that `make lint`'s audit still passes on `src/curate_d`.
- [ ] 2.9 Run `make check` in the stream's worktree, and run `sonar analyze` on the three files. Verify that both are clean, then hand back.

## 3. Workflow stream — `src/curate_d/workflows/{ingest,reconcile,scan}.py`

- [ ] 3.1 Extract `ingest_source`'s staging retry loop into a plain async helper that keeps the step, sleep and transaction order (S3776, 17). Verify that `make lint` (workflow audit) and `uv run mypy` pass and that `sonar analyze` reports no S3776. The integration suite covers the body in 8.3.
- [ ] 3.2 Extract `reconcile`'s per-target planning into a pure helper, and its running-attempt lookup into a plain async helper that keeps the `if attempt_ids` guard (S3776, 22). Add isolated unit tests for the pure helper, covering the stale, running and place cases, in `tests/unit/workflows/test_reconcile.py`. Verify that they pass and that `sonar analyze` is clean.
- [ ] 3.3 Extract `scan_root`'s per-folder enumeration into a plain async helper, and its action tally into a pure helper (S3776, 21). Add unit tests for the tally in `tests/unit/workflows/test_scan.py`. Verify that they pass, that `make lint` passes and that `sonar analyze` is clean.
- [ ] 3.4 Run `make check`, and write a list in the hand-back for each workflow body. The list gives the ordered step, transaction, sleep and enqueue calls before and after the change. Verify that the lists are identical.

## 4. Placement stream — `src/curate_d/workflows/placement.py`, `src/curate_d/placement/{transfer,verification,rclone,admission}.py`, `tests/integration/test_rclone.py`

- [ ] 4.1 Replace the four `"placement.superseded"` log calls with one plain logging helper (S1192). Verify that `uv run pytest tests/unit/workflows/test_placement.py` passes and that `sonar analyze` is clean.
- [ ] 4.2 Make the transfer progress callback synchronous: change its type, the call site in `copy_and_collect`, `report` in `_copy_step` and `tests/integration/test_rclone.py` (S7503). Add a unit test in `tests/unit/placement/test_transfer.py` that shows that the callback receives the polled progress. Verify that it passes and that `uv run mypy` passes.
- [ ] 4.3 Extract `copy_and_collect`'s polling, failure evidence and read-back into helpers, in the same rclone call order (S3776, 26). Verify that `tests/unit/placement/test_transfer.py` passes unchanged.
- [ ] 4.4 Extract `assess`'s reported-hash, read-back and size rungs into helpers in the same precedence (S3776, 23). Verify that `tests/unit/placement/test_verification.py` passes unchanged.
- [ ] 4.5 Extract `_object_info`'s modification time and hashes into helpers (S3776, 18). Add `_STAT: Final = "operations/stat"` for its three calls (S1192). Verify that `tests/unit/placement/test_rclone_client.py` passes.
- [ ] 4.6 Pin NaN rejection in `tests/unit/placement/test_admission.py` on the current code. Then rewrite the condition per design.md decision 3 (S2583). If `sonar analyze` still flags it, restore the line and report the issue for False positive. Verify that the admission tests pass either way.
- [ ] 4.7 Run `make check` and `sonar analyze` on the five files. Verify that both are clean, then hand back with the `place_content` call-order list (as in 3.4).

## 5. Classification stream — `src/curate_d/classification/{deterministic,local,metadata,prepare}.py`, `src/curate_d/rendering/render.py`, `src/curate_d/content/sniff.py`, `src/curate_d/observability/redaction.py`, `src/curate_d/domain/base.py`, `tests/integration/test_local_ocr.py`

- [ ] 5.1 Split `classify_deterministic` into per-media-kind rule helpers, and replace the nested conditional with an if/elif helper (S3776, 21; S3358). Keep the order screenshot, then EXIF, then camera name. Verify that `uv run pytest tests/unit/classification/test_deterministic.py` passes unchanged.
- [ ] 5.2 Drop the unused `item` parameter of `classify_local`, and update `prepare.py`, `tests/unit/classification/test_local.py` and `tests/integration/test_local_ocr.py` (S1172). Verify that the unit tests and `uv run mypy` pass.
- [ ] 5.3 Pin `D:2026Z` and `D:20260301120000-0500` on the current `_PDF_DATE`. Then compose the pattern from named sub-patterns with the same seven groups (S5843). Verify that the new and the existing date tests pass and that `sonar analyze` is clean, or use the `re.VERBOSE` fallback.
- [ ] 5.4 Drop the duplicate `A-Z` from the redaction character class under `(?i)` (S5869). Verify that `uv run pytest tests/unit/observability/test_redaction.py` passes unchanged.
- [ ] 5.5 Extract `render_with`'s per-segment substitution and flat-profile join into helpers (S3776, 16). Verify that `uv run pytest tests/unit/rendering/test_render.py` passes unchanged.
- [ ] 5.6 Add a `_HEIC: Final` constant for `"image/heic"` (S1192). Verify that `uv run pytest tests/unit/content/test_sniff.py` passes.
- [ ] 5.7 Iterate over the key-set difference instead of `list(self.__dict__)` in `DomainModel.__setstate__` (S7504). Verify that `uv run pytest tests/unit/domain/test_base.py` passes unchanged, including the retired-key case.
- [ ] 5.8 Run `make check` and `sonar analyze` on the changed files. Verify that both are clean, then hand back.

## 6. Staging, providers and ledger stream — `src/curate_d/staging/{object_store,occupancy,smoke,snapshots,publish}.py`, `src/curate_d/providers/pcloud/files.py`, `src/curate_d/ledger/tables.py`

- [ ] 6.1 Narrow the multipart-abort handler to `except Exception` (S5754). Verify that the abort-failure tests in `tests/unit/staging/test_object_store.py` pass unchanged, including the deadline case that asserts `__cause__`.
- [ ] 6.2 Return an annotated `ReclaimReport` from `sweep`, or construct it explicitly if `sonar analyze` still flags the code (S5886). Verify that `uv run pytest tests/unit/staging/test_occupancy.py` passes.
- [ ] 6.3 Extract `run_smoke_checks`'s read-back checks and the reader write-denied check into helpers (S3776, 19). Verify that `uv run pytest tests/unit/staging/test_smoke.py` passes unchanged.
- [ ] 6.4 Open the snapshot file through `asyncio.to_thread` (S7493). Verify that `uv run pytest tests/unit/staging/test_snapshots.py` passes, including the cancellation and ENOSPC cases.
- [ ] 6.5 Replace `span()`'s nested conditional with statements (S3358). Verify that `uv run pytest tests/unit/staging/test_publish.py` passes unchanged.
- [ ] 6.6 Add a `Final` constant for `"resolve folder"` in `providers/pcloud/files.py` (S1192). Verify that `uv run pytest tests/unit/providers` passes.
- [ ] 6.7 Add private constants for `"content_objects.id"`, `"size_bytes >= 0"` and `"'[]'::jsonb"` in `ledger/tables.py` (S1192 ×3). Verify that `uv run pytest tests/unit/ledger` passes and that the emitted DDL is unchanged: compare `CreateTable` output for every table in `metadata` before and after.
- [ ] 6.8 Run `make check` and `sonar analyze` on the changed files. Verify that both are clean, then hand back.

## 7. Build and ops stream — `docker/Dockerfile`, `ops/reset/reset-environment.sh`, `tests/unit/infrastructure/test_dockerfile_pins.py`

- [ ] 7.1 Add a unit test that fails when any `uv sync` line in `docker/Dockerfile` lacks `--locked` or `--no-build`, and that names the line. Verify that it fails on the current Dockerfile with both lines named.
- [ ] 7.2 Add `--no-build` to both `uv sync` calls (S8541 ×2), with a one-line reason comment as in `checks.yml`. Verify that the 7.1 test passes and that `make image` builds.
- [ ] 7.3 Quote `"tesseract-ocr=${TESSERACT_VERSION}"` (S6570). Verify that `make image` builds and that `uv run pytest -n auto tests/unit/infrastructure` passes.
- [ ] 7.4 Add `local steps=$1` to `local_helper` and `dev_helper`, and add `*) usage ;;` as the final arm of the dispatch `case` (S7679 ×2, S131). Verify that `bash -n ops/reset/reset-environment.sh` passes, that `shellcheck` reports nothing new and that `tests/unit/infrastructure/test_ops_paths.py` passes.
- [ ] 7.5 Fix any issue that 0.1 routed to this stream and that no other task covers. Run `make check` and `sonar analyze` on the changed files. Verify that both are clean, then hand back.

## 8. Integration (orchestrator, after groups 1–7)

- [ ] 8.1 Merge the seven stream branches into `chore/resolve-sonar-issues` one at a time, and run `make check` after each merge. Verify that every merge is conflict-free, that `git diff --stat dev` lists only stream-owned files and `openspec/`, and that `make check` is green.
- [ ] 8.2 Review each workflow-body and sweep diff against design.md decision 4, with the 3.4 and 4.7 call-order lists. Run `sonar analyze --base dev -p jorgejch_curate-d`. Verify that the analysis reports no issue in the changed files except the ones in "Accepted issues", and record each new row there.
- [ ] 8.3 Run `make test-stack`, then `make test-integration` on that fresh stack. Verify that the whole suite passes, and record the counts in design.md.
- [ ] 8.4 Run `openspec validate resolve-sonar-issues`. Push the branch, and open the pull request against `dev`. Verify that validation passes and that CI's checks and integration jobs are green.
- [ ] 8.5 After the merge and the next `dev` analysis, re-list the OPEN and CONFIRMED issues. Set each "Accepted issues" row to Accepted or False positive with its reason as the comment (`change_sonar_issue_status`). Route any other survivor through design.md decision 1. Verify that a final re-list returns `paging.total` 0, or only issues that landed in code this change did not touch, listed by key.

## Workflow follow-up

- Sync the `release-pipeline` delta into `openspec/specs/` (`openspec-sync-specs`), then archive the change (`openspec-archive-change`).
