# Design

## Context

The branch `update-to-dbos-3.5.0` holds two unpushed commits on top of `dev`. Both have the
subject `chore(deps): update DBOS and runtime dependencies`:

- `f04108b` changes `pyproject.toml` and adds `.editorconfig`, without `uv.lock`, so the
  lock does not match `pyproject.toml` at that commit.
- `2e26129` changes only `uv.lock`.

At the branch head the lock resolves cleanly (`uv lock --check`), and `make check` passed
(lint, type check, and 2354 unit tests with coverage at 86.17%). See proposal.md for the
motivation.

Four facts shape the approach:

- **The vendored skill is already current.** `.agents/skills/dbos-python` is byte-identical
  to upstream `dbos-inc/agent-skills` at `main` (tarball commit `cc20c18`), skill version
  2.2.0. `skills-lock.json` lists only `dbos-python`.
- **DBOS 3.5.0 changes the system schema.** The upstream diff of `dbos/_migration.py`
  (3.4.0...3.5.0) adds migrations 126 to 131:
  - 126 adds the column `workflow_status.suspend_key`.
  - 127 creates the partial index `idx_workflow_status_suspended` (`status = 'SUSPENDED'`).
  - 128 creates `idx_workflow_status_deadline_v2`, and 129 drops `idx_workflow_status_deadline`.
  - 130 replaces the SQL function `send_message`.
  - 131 creates the table `external_objects` and its index
    `idx_external_objects_store_created_at`, for the new external-storage feature.
  The `workflow_status` indexes are created `CONCURRENTLY`. `migrations/env.py` applies these through the public
  `run_dbos_database_migrations` before Alembic's own migrations, outside Alembic's
  transaction.
- **curate.d does not reach the new status.** Nothing in `src/` calls `recv`, `send` or a
  suspend API, and nothing names the dropped index or `send_message`. A `SUSPENDED` row, if
  one ever appears, is handled safely: the retention prune deletes only an allow-list of
  terminal statuses (`runtime/retention.py`, `TERMINAL_STATUSES`), `api/jobs.py` counts an
  unknown status as pending, and the sweep and view 0005 select only `PENDING` and
  `ENQUEUED`.
- **The branch carries unrelated churn.** `pyproject.toml` also re-aligns inline comments and
  the ruff `select` list, which is most likely an IDE reformat. `CLAUDE.md` forbids drive-by
  reformatting.

## Goals / Non-Goals

**Goals:**

- Pin the runtime to DBOS 3.5.0 and the three packages that move with it.
- Keep the vendored skill and `skills-lock.json` in step with upstream, as `CLAUDE.md` requires.
- Keep the current DBOS version text in the documents equal to the pin, with a unit test.
- Prove the bump on a fresh `make test-stack`, not on a stack left by an earlier run.
- Leave a history of one logical change per commit, with a consistent lock at each commit.

**Non-Goals:**

- No application code change, no curate.d migration, no change to a workflow or step.
- No change to the `psycopg`, `logfire` or `openai` call sites.
- No edit to a Twelve-Factor row beyond the two re-read rows and the assessment line.
- No `.editorconfig` in this change (D8).
- No rollback test of a 3.4.0 build over the 3.5.0 schema (R3).

## Decisions

**D1. Raise `psycopg` to `~=3.3.9`, not an exact pin.** `openspec/config.yaml` states the
policy: "Target the stated minor; patch level is free". `uv.lock` already fixes the exact
release, so an exact pin in `pyproject.toml` adds nothing. Alternative rejected: `==3.3.9`,
which goes against the stated policy without a reason specific to `psycopg`.

**D2. Revert the unrelated `pyproject.toml` churn.** The re-aligned comments and the
reformatted ruff `select` list go back to their `dev` text. Only the dependency lines and
the `pyrefly` move are kept. Alternative rejected: keep the formatting. It would hide the
real dependency changes in the review.

**D3. Move `pyrefly` to `dev` with a comment.** The `dev` entry reads
`"pyrefly==1.4.0", # PyCharm uses it.` The production image installs with `--no-dev`, so the
tool no longer ships. Mypy stays the type gate (`Makefile`, `checks.yml`).

**D4. Refresh the skill through the `skills` CLI, then verify by diff.** Run
`npx skills@1.7.4 update`, as the archived design of change `absorb-package-updates-2026-10`
records. Then `diff -r` the vendored folder against upstream `main` and require no difference.
Accept a `skills-lock.json` change only if its `computedHash` field is the only change. Hand
edits to the hash are rejected because the hash algorithm cannot be reproduced by hand.

**D5. Re-check every raw-SQL dependency on the DBOS schema.** The schema changes (Context),
so the re-check that `CLAUDE.md` requires on every DBOS upgrade is mandatory here:
migrations 0005 and 0007 to 0009, `runtime/retention.py`'s prune of `datasource_outputs`,
and the integration fixtures that read DBOS tables. `tests/integration/test_workflow_views.py`
and `tests/integration/test_migrations.py` carry the check, run on the migrated schema.

**D6. Re-read the two `Limited by DBOS` rows, and record the re-read.** Rows III.2 and IX.1
of `docs/twelve-factor.md` name the skill as their authority. The skill content is
unchanged, so both rows keep their status. The page's assessment line keeps its
2026-11-04 record against DBOS 3.4.0 and adds that III.2 and IX.1 were re-read against
DBOS 3.5.0 on 2026-11-12. The other 31 rows were not re-read, so their date stays.

**D7. Scope the version check to current statements.** The new unit test compares the
minor version named in `README.md` and `openspec/config.yaml` with the `dbos` pin. The
`docs/twelve-factor.md` assessment line is a dated record and is not compared. Alternative
rejected: compare every mention. Historical lines would then fail the build on every bump.

**D8. Split `.editorconfig` out, and rebuild the branch history first.** `.editorconfig` is
unrelated to the DBOS bump. Its `[*.py]` section turns on PyCharm alignment
(`ij_python_align_consecutive_assignments`, `ij_python_dict_alignment = 2`,
`ij_python_align_multiline_*`), which `ruff format` never produces, so a PyCharm reformat
would fail `make lint`. Ruff itself does not read `.editorconfig` (checked: a
`max_line_length = 20` there did not trigger `E501`). The file moves, unchanged, to a new
branch `chore/editorconfig` from `dev`, for its own pull request that turns the alignment
options off. This branch is then rebuilt from `origin/dev` as one dependency commit that
holds `pyproject.toml` and `uv.lock` together. The branch is unpushed, so the rewrite affects
no one. This runs before every other task because a history rewrite under parallel work
would lose that work.

## Twelve-Factor

This change departs from no rule and brings no rule into conformance. These rows are re-read
and keep their status:

- **III.2** (`Limited by DBOS`, queue limits in DBOS's store). The skill is unchanged, so the
  limit stands.
- **IX.1** (`Limited by DBOS`, workers stay until a version drains). The skill is unchanged,
  so the limit stands.
- **V.3** (`Gap`, schema migration at process start). DBOS 3.5.0's migrations 126 to 131 run
  through the same paths the row already names. The gap is unchanged, so the row is not edited.

## Risks / Trade-offs

- **[R1] A DBOS 3.5.0 behavior change breaks recovery or scheduling.** The scheduler and
  queue locking were reworked. → Mitigation: run the full integration suite on a fresh
  `make test-stack` (CLAUDE.md, "Integration tests"), and do not accept a run on a stack left
  by `make stack-up`. A red run blocks the change.
- **[R2] A serialization change alters how pickled inputs read back.** The upstream diff of
  `dbos/_serialization.py` changes only the portable-JSON coercion of variadic arguments, not
  pickling. The release item "Fast Path for Binary Serialization" (DBOS PR #934) may touch
  other files, and is not yet checked. → Mitigation: task 1.4 reads the files of PR #934 and
  records whether the pickled encoding of inputs or outputs changes. If it does, the deploy
  follows the drain rule in `docs/recovery.md`, "Before deploying".
- **[R3] A rollback to 3.4.0 meets the 3.5.0 schema.** Reverting the pin does not undo
  migrations 126 to 131. The added column has a NULL default and the dropped index served
  performance only, so a 3.4.0 build is expected to run, but this is not verified. →
  Mitigation: the pull request and deploy notes state that rollback over the 3.5.0 schema is
  unverified.
- **[R4] The skill refresh rewrites files it should not.** → Mitigation: D4's diff gate.
- **[R5] The history rewrite loses work.** → Mitigation: D8 runs first, the untracked
  `openspec/changes/update-dbos-to-3-5-0/` folder survives a soft reset, and task 1.1 saves
  `.editorconfig` on its own branch before the reset.

## Migration Plan

1. Merge through a pull request into `dev`, never a direct push.
2. Deploy with the existing gate: run `python -m curate_d.configuration.verify` from the new
   image (`docs/recovery.md`, "Before deploying"). The first `alembic upgrade head` applies
   DBOS migrations 126 to 131. Drain first if task 1.4 finds a pickling change.
3. Rollback: revert the pin commit and redeploy the previous image. The DBOS schema stays at
   migration 131 (R3).

## Parallel work streams

- **Group 1, up-front (one agent).** Rebuilds the branch, saves `.editorconfig` on its own
  branch, and owns every edit to `pyproject.toml` and `uv.lock`. Groups 2 and 3 wait for it,
  because it rewrites the history they would commit onto.
- **Group 2, skill.** Edits `.agents/skills/dbos-python/**` and `skills-lock.json`.
- **Group 3, documents and test.** Edits `openspec/config.yaml`, `README.md`,
  `docs/twelve-factor.md` and `tests/unit/infrastructure/test_engineering_standards.py`. It
  reads `pyproject.toml` but never writes it.

Groups 2 and 3 touch disjoint files and run in parallel. Group 4, integration, waits for
groups 1 to 3. Then the specs are synced and the change is archived.

## Open Questions

None.

## Outcome

### Task 1.4: DBOS PR #934 and pickled state

- **The PR.** DBOS PR #934, "Fast Path for Binary Serialization", merged 2026-11-02 as
  `bfb5741`. It changes four files: `dbos/_client.py`, `dbos/_dbos.py` (docstrings of
  `store_external_object` only: "A bytes value is stored as is, without serialization"),
  `dbos/_workflow_commands.py` (the background-job hold is timed from lock acquisition), and
  `tests/test_dbos.py`. No change touches `_serialization.py`, `_sys_db.py` or `_core.py`.
- **The encoding path.** The fast path applies only to `DBOS.store_external_object`, a
  feature new in 3.5.0 (PR #928, `_external_storage.py`, `_enterprise.py`). In the 3.4.0...3.5.0
  compare, `_serialization.py` has one hunk, in `coerce_portable_args_to_hints`, which runs
  only for portable-JSON rows (`_core.py:1338-1345`). `DefaultSerializer` (`py_pickle`,
  `pickle.dumps` then base64) has no change. The `_sys_db.py` diff has no change to how
  `inputs`, `output` or `serialization` are written or read.
- **3.4.0 rows read by 3.5.0.** No change: the same `DefaultSerializer.deserialize` reads them.
- **3.5.0 rows read by 3.4.0.** No change for curate.d: rows keep `py_pickle` and the same
  bytes. A pickled DBOS exception class new in 3.5.0 (`DBOSWorkflowSuspendedError`, the four
  `DBOSExternal*` errors) does not unpickle on 3.4.0, but only suspend mode and external
  storage raise them, and curate.d uses neither.
- **curate.d's setting.** `build_dbos_config` (`runtime/dbos_app.py`) sets no `serializer`,
  and `src/` passes no `serialization_type`, so every row uses the default `py_pickle`.
- **Conclusion.** The pickled encoding of inputs and outputs does not change. The drain rule
  in `docs/recovery.md`, "Before deploying", does not apply to this deploy.
- **Side finding.** 3.5.0 also adds migration 131, the table `external_objects`, which the
  Context listed as 126 to 130 (now corrected). curate.d does not use it.

### Group 4: integration on a fresh stack (2026-11-12)

- `make test-stack`, then `make test-integration`, ran from 21:38 to 21:51 PT on `2d6cc6d`;
  `e903c1b` (packaging only) landed during the run. `make test-integration` passes no
  `--staging-backend`, so it also collected the OVH staging backend, and exited 2:
  443 passed, 2 skipped, 35 errors, 2 failed. All 37 are OVH-backend tests in
  `test_staging_backend_checks.py` and `test_staging_contract.py`, failing at setup because no
  OVH test bucket is configured on this machine. A re-run of the 37 gave the same result.
- CI runs `--staging-backend=rustfs`, which collects 445 tests: all 443 passed, and the 2
  skips are rows 30 and 31 of `staging-compatibility.md`, observed by `make lifecycle-check`.
  No failure relates to DBOS 3.5.0.
- On that stack, `test_workflow_views.py` and `test_migrations.py` passed (28), and
  `dbos.dbos_migrations` reads 131, with `workflow_status.suspend_key` and
  `dbos.external_objects` present and `idx_workflow_status_deadline` gone.
- `make check` passed on `e903c1b` (2369 tests, coverage 86.17%), and
  `tests/unit/domain/test_base.py` passed (6).
