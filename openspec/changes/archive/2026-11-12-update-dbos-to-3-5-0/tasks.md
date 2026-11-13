# Tasks

Group 1 runs first, on one agent: it rewrites the branch history. Groups 2 and 3 then run in
parallel, one agent each, on disjoint files. Group 4 waits for groups 1 to 3. Apply the
groups in parallel as this file orders them. See design.md, "Parallel work streams".

## 1. Up-front: branch history and dependencies (`pyproject.toml`, `uv.lock`)

- [x] 1.1 Save `.editorconfig` unchanged on a new branch `chore/editorconfig` from
  `origin/dev`, as one commit `chore(editor): add .editorconfig`; verify
  `git show --stat chore/editorconfig` lists only `.editorconfig` (design D8)
- [x] 1.2 Rebuild `update-to-dbos-3.5.0` from `origin/dev` as one commit
  `build(deps): update DBOS to 3.5.0 and runtime dependencies` that holds only
  `pyproject.toml` and `uv.lock`: the `dev` text of `pyproject.toml` plus `dbos[otel]==3.5.0`,
  `psycopg[binary]~=3.3.9`, `logfire[fastapi]==5.6.0`, `openai==3.50.0`, and
  `"pyrefly==1.4.0", # PyCharm uses it.` moved to the `dev` group, then `uv lock`; verify
  `git diff --stat origin/dev..HEAD` lists only those two files, `git diff origin/dev --
  pyproject.toml` has no hunk outside the dependency lists, `uv lock --check` passes, and the
  untracked change folder `openspec/changes/update-dbos-to-3-5-0/` still exists (design D1,
  D2, D3, D8)
- [x] 1.3 Verify the installed versions with
  `uv run python -c "import importlib.metadata as m; print([m.version(p) for p in ('dbos', 'psycopg', 'logfire', 'openai')])"`;
  expect `3.5.0`, `3.3.9`, `5.6.0`, `3.50.0`, and verify `uv sync --no-dev --dry-run` does not
  list `pyrefly`
- [x] 1.4 Read the files of DBOS PR #934 (`gh pr view 934 -R dbos-inc/dbos-transact-py --json files`)
  and their diff, and record in this change's `design.md`, under a new "Outcome" section,
  whether the pickled encoding of workflow inputs or step outputs changes; verify the
  section names the PR, the files and the finding (design R2)

## 2. Skill (`.agents/skills/dbos-python`, `skills-lock.json`), after group 1

- [x] 2.1 Run `npx skills@1.7.4 update` and verify with `git status --short` that it changes no
  file outside `.agents/skills/dbos-python` and `skills-lock.json`
- [x] 2.2 Diff the vendored folder against upstream `dbos-inc/agent-skills` `main` (tarball
  fetched to the scratchpad) with `diff -r`; verify no difference (design D4)
- [x] 2.3 Verify `skills-lock.json` changed only in `computedHash`, or not at all, and that
  `.claude/skills/dbos-python` is still a symbolic link to `../../.agents/skills/dbos-python`
  (`tests/unit/infrastructure/test_engineering_standards.py` pins the link); commit any change
  as `chore(skills): refresh dbos-python for DBOS 3.5.0`

## 3. Documents and version test, after group 1

- [x] 3.1 Add a unit test that compares the DBOS minor version named in `README.md` and
  `openspec/config.yaml` with the `dbos[otel]` pin, fails naming the document when they
  differ, and fails naming `pyproject.toml` when the pin is missing; run it red first, then
  green, with `uv run pytest tests/unit/infrastructure/test_engineering_standards.py --no-cov -q`
- [x] 3.2 Change the DBOS version text in `openspec/config.yaml` (Tech Stack, "DBOS 3.4") and
  in `README.md` (line 85, "DBOS 3.4") to 3.5; verify the test from 3.1 passes
- [x] 3.3 Add to the assessment line of `docs/twelve-factor.md` (line 58) that rows III.2 and
  IX.1 were re-read against DBOS 3.5.0 and `dbos-python` skill 2.2.0 on 2026-11-12, keeping
  the 2026-11-04 record against DBOS 3.4.0 (design D6); verify the engineering-standards unit
  tests still pass with the same command as 3.1

## 4. Integration (after groups 1 to 3)

- [x] 4.1 Run `make check`; verify it passes (lint, type check, unit suite and coverage gate)
- [x] 4.2 Run `make test-stack` to reset the stack, then `make test-integration` against that
  stack; verify the full integration suite passes. A stack left by `make stack-up` is not
  used. Record the command and its outcome for the pull request.
- [x] 4.3 Re-check the DBOS raw-SQL dependencies on the schema at migration 131 (design D5):
  run `uv run pytest tests/integration/test_workflow_views.py tests/integration/test_migrations.py --no-cov -m integration --staging-backend=rustfs`
  on the stack from 4.2 and verify both pass, and verify with
  `psql` that `dbos.workflow_status` has the `suspend_key` column, so the run used the new
  schema
- [x] 4.4 Run `uv run pytest tests/unit/domain/test_base.py --no-cov -q` and verify the
  `__setstate__` and `StaleCheckpointError` tests pass on DBOS 3.5.0 (design R2)

## Workflow follow-up

- Sync the delta spec for `engineering-standards` into `openspec/specs/engineering-standards/spec.md`
  (`openspec-sync-specs`), then archive the change (`openspec-archive-change`).
- Open the pull request against `dev` with `.github/pull_request_template.md`. It states the
  outcome of task 4.2, the finding of task 1.4, and that rollback over the 3.5.0 schema is
  unverified (design R3).
- Open a separate pull request from `chore/editorconfig` against `dev` that sets the
  `ij_python_align_*` options and `ij_python_dict_alignment` so that PyCharm's reformat agrees
  with `ruff format`.
