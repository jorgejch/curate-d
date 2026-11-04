# Tasks

Work streams: **1** (verify, fix or roll back) owns `pyproject.toml` and `uv.lock`; **2**
(DBOS skill and version text) owns `.agents/skills/dbos-python/**`, `skills-lock.json`,
`README.md`, `openspec/config.yaml`; **3** (SDK evidence command) owns
`src/curate_d/staging/sdk_evidence.py`, its tests, the `Makefile`,
`ops/staging-compatibility.md` and `ops/README.md`. The three streams touch disjoint files
and can start at once. Task 1.3 is the one exception: a `dbos` rollback reverts stream 2's
files, so 1.3 waits for 2.3 and 2.4 to finish before it runs, if it runs at all. Group 4 waits
for all three.

## 1. Verify the bump; fix or roll back

- [x] 1.1 Commit the working-tree `pyproject.toml` and `uv.lock` to the change branch and open the pull request against `dev`; verify `uv lock --check` passes and CI's `integration.yml` runs (the paths filter matches the lock) rather than reporting both jobs skipped.
- [x] 1.2 Reproduce on a fresh stack, as `CLAUDE.md` "Integration tests" requires: `make test-stack` then `make test-integration GROUP=1..4` (or the whole suite) and `make check`; verify every job is green or record each failing test id in this task's line. Ask the reviewer for the failing tests they observed if the run is green and they have a run to point at.
- [x] 1.3 For each failure from 1.2 or CI: pin the suspected package alone back to its previous version (design.md, decision 1), re-run the failing test to attribute it, then either fix the code and tests (fix ≤ 400 changed lines, `git diff --stat`) or keep the rollback pin with a `pyproject.toml` comment naming the incompatibility and upstream issue; verify the attributed test passes and `make check` is green. A `dbos` rollback also reverts tasks 2.1–2.4 in the same commit. Record each decision under "Outcome" in design.md. Skip this task, writing "no failure attributed" there, when 1.2 and CI are green.

## 2. Refresh the DBOS skill and the stated DBOS version

- [x] 2.1 Run `npx skills@1.7.4 update` at the repository root (fall back to `npx skills@1.7.4 add dbos-inc/agent-skills --skill dbos-python -y` if the entry is left untouched); verify `git diff --stat` shows only `.agents/skills/dbos-python/**` and `skills-lock.json`, and the lock's `computedHash` changed.
- [x] 2.2 Download upstream `dbos-inc/agent-skills` at commit `0bfe1dd` (GitHub tarball) and `diff -r` its `skills/dbos-python` against `.agents/skills/dbos-python`; verify no difference, `SKILL.md` reports `version: "2.2.0"`, and `.claude/skills/dbos-python` is still the symlink (`readlink`).
- [x] 2.3 Re-read the refreshed `references/advanced-patching.md`, `advanced-versioning.md`, `lifecycle-config.md` and `advanced-async.md` against `CLAUDE.md`'s DBOS section; verify no new departure needs recording (expected: none, since `enable_patching`, auto-upgrade and `reserve_step` are unused), or add the departure line to `CLAUDE.md` if one does.
- [x] 2.4 Update `README.md` ("DBOS 3.3" → 3.4) and `openspec/config.yaml` ("DBOS 3.3" → 3.4; the "Versions verified current as of" date → the change's date); verify `grep -rn "DBOS 3.3" README.md openspec/config.yaml docs CLAUDE.md` returns nothing and `uv run pytest -n auto tests/unit/infrastructure` passes (the README badge and ops-path tests read these files).

## 3. SDK evidence command and drift guard

- [x] 3.1 Add `src/curate_d/staging/sdk_evidence.py` with the lock reader (`tomllib`, packages `aioboto3`, `aiobotocore`, `botocore`), the matrix parser (header-table `SDK`, `RustFS server`, `RustFS endpoint` rows; the matrix table's header line gives the column order; rows 32–35 located by their `#` cell) and the `check` subcommand per design.md decision 4; verify `uv run python -m curate_d.staging.sdk_evidence check` exits 0 on the committed files and `uv run mypy` and `make lint` pass.
- [x] 3.2 Add `tests/unit/staging/test_sdk_evidence.py` for `check`: agreement, one drifted package named with both versions, a missing or partial SDK row failing and naming the file, a cell holding a backtick-quoted `|` parsed intact, a row with the wrong cell count rejected; verify `uv run pytest tests/unit/staging/test_sdk_evidence.py` passes.
- [x] 3.3 Add the `record` subcommand: runs the contract suite per selected backend with `--junitxml`, parses outcomes and the `sdk_checksum_headers` property, rewrites the `SDK` row and rows 32–35 per decision 4 (`PASS`/`FAIL` for run backends, `PENDING` with previous evidence kept for backends not run when the SDK moved, untouched otherwise), prints the review list of other lines naming the previous versions, honours `--dry-run` and `--date`, exits 2 with no write when a report is missing and 1 after writing on any failing test; verify with the unit tests of 3.4 and `uv run mypy`.
- [x] 3.4 Extend `tests/unit/staging/test_sdk_evidence.py` for `record` with a fixture matrix and fixture JUnit files (pass, one failing test, missing property, no report): cell rewrites exact, untouched rows byte-identical, dry run leaves the file unchanged, exit codes as specified, the property name pinned against `tests/integration/test_staging_contract.py`'s source; verify the module's unit coverage keeps `make test`'s 80% floor.
- [x] 3.5 Add `tests/unit/infrastructure/test_staging_sdk_evidence.py` that runs `check` on the repository's `uv.lock` and `ops/staging-compatibility.md`; verify it passes today and fails with the package named when the test temporarily points at a copy of the lock with `botocore` edited.
- [x] 3.6 Add `make staging-sdk-evidence` (`BACKENDS ?= rustfs`, `DRY_RUN` switch; listed in `.PHONY` and `help`), point the matrix's "How to refresh" SDK-upgrade row and `ops/README.md`'s tree and staging section at it; verify `make help` lists the target, `make staging-sdk-evidence DRY_RUN=1` runs against the local `make test-stack` services and reports no drift, and `uv run pytest -n auto tests/unit/infrastructure` passes.

## 4. Integration check

- [x] 4.1 With all streams merged on the branch: `make check` green, `make test-integration` green on a `make test-stack` stack, `openspec validate absorb-package-updates-2026-10` passes, and `git diff --stat dev` lists only the files the three streams own; verify and record the final counts in design.md's "Outcome".

## Workflow follow-up

- Sync the `content-staging` delta into `openspec/specs/` (`openspec-sync-specs`), then archive the change (`openspec-archive-change`) after review.
