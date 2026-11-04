# Design

## Context

See proposal.md, "Why". Observed state on `dev` `62068e5` with the bumped lock installed
(2026-10-31, local, Docker Desktop arm64):

- `make lint`, `make typecheck` (mypy, strict) and `uv run pytest -n auto tests/unit` pass.
- `uv run pytest tests/integration --no-cov -m integration --staging-backend=rustfs` passes:
  442 passed, 2 skipped (the two local-only lifecycle observations that need `make
  lifecycle-check`), 17 min. No failure attributable to a package was found.
  Re-run the same day on a fresh `make test-stack` (stack reset, services started,
  migrated from empty under DBOS 3.4.0): 442 passed, 2 skipped, 17 min, exit 0.
- `uv lock --check` passes; `aioboto3`/`aiobotocore`/`botocore` are unchanged in the lock.
- `.agents/skills/dbos-python` is byte-identical to upstream `dbos-inc/agent-skills`
  commit `7fd8aef` (skill 2.1.0). Upstream head `0bfe1dd` (2.2.0, 2026-10-28) differs in
  six files, 98 lines: DBOS 3.4 guidance (`enable_patching` deprecated, `DBOS.reserve_step`,
  auto-upgrade). The `skills-lock.json` entry carries `computedHash`, written by the
  `skills` CLI (`vercel-labs/skills`, `computeSkillFolderHash`); a by-hand recomputation did
  not reproduce the committed value, so the lock is refreshed through the CLI, not by hand.
- DBOS 3.4.0 adds system migrations 124 and 125 (indexes on `application_versions`,
  `CREATE INDEX CONCURRENTLY`). `migrations/env.py` already runs DBOS's migrations through
  the public `run_dbos_database_migrations`, which handled migration 122's concurrent index
  under 3.3; the integration run above covered `test_migrations.py` and
  `test_workflow_views.py`.
- `ops/staging-compatibility.md` records SDK evidence in one header row (`| SDK | ... |`)
  and in rows 32–35 of the matrix table (`| # | Behavior | Check | RustFS | MinIO
  (superseded) | OVH | Design response |`). `test_default_sdk_checksum_headers_are_accepted`
  already captures the request headers and records them as a JUnit property
  (`sdk_checksum_headers`). Rows 34 and 35 are implicit in every test of the contract suite.
- Repository precedent for tooling: `curate_d.runtime.workflow_audit` and
  `curate_d.staging.smoke` are modules under `src/` run with `python -m`, strictly typed and
  unit-tested; `ops/*.py` is outside mypy's `files`.

## Goals / Non-Goals

**Goals:**
- Land the bump with the work the repository attaches to it, in one change.
- Decide package by package: fix, or roll back under the 400-line rule, never a mix within
  one package.
- Make the SDK evidence refresh a command whose output is the evidence, and make SDK drift a
  failing test rather than a reminder in prose.

**Non-Goals:**
- Adopting DBOS 3.4 features (`reserve_step`, auto-upgrade, sharding). The recovery sweep
  keeps its own cross-version fork; auto-upgrade is an enterprise feature and would be a
  departure to record under `CLAUDE.md`'s DBOS section if ever taken.
- Changing the pin style the bump introduces, or the Dependabot grouping.
- Running the OVH leg of the contract suite in this change: `aioboto3` did not move, so no
  new evidence is owed. The command supports OVH for the next time.
- Re-flowing the matrix's prose (the "RESOLVED 2026-09-15" section cites `botocore`
  1.40.61 by name): the command lists such lines; a person edits them.

## Decisions

1. **Verification stream is conditional and bounded.** The gate already passes locally, so
   the stream re-runs it on the branch (CI runs `integration.yml` because `pyproject.toml`
   and `uv.lock` change) and acts only on a failure. The rule: attribute the failure to one
   package by pinning that package alone back to its previous version and re-running the
   failing test; if the fix for the attributed package would exceed 400 changed lines
   (`git diff --stat` over the fix's files, tests included), keep the rollback pin with a
   `pyproject.toml` comment naming the incompatibility and the upstream issue, and record
   the decision in this change's design.md under "Outcome". Previous versions: `dbos` 3.3.0,
   `logfire` 5.3.0, `httpx2` 2.13.1, `openai` 3.40.0, `pypdf` 6.21.3, `pyrefly` 1.3.2,
   `pydantic` 2.14.1 with `pydantic-core` 2.50.1, `pydantic-settings` 2.16.1. A rollback of
   `dbos` also reverts the skill refresh and the docs' version text, because the skill must
   match the DBOS in use. The first local run used a stack migrated by earlier DBOS 3.3 runs, not a
   fresh `make test-stack`; `CLAUDE.md`'s "Integration tests" rule (added by this change)
   makes the fresh stack the default, so stream 1 re-runs on one. Alternative rejected:
   committing the bump on the first run alone.

2. **Skill refresh through the `skills` CLI, verified against upstream by diff.** Run
   `npx skills@1.7.4 update` (or `npx skills@1.7.4 add dbos-inc/agent-skills --skill
   dbos-python -y` if `update` leaves the entry untouched), then `diff -r` the vendored
   folder against the `skills/dbos-python` folder of upstream commit `0bfe1dd` (tarball from
   the GitHub API) and require no difference; confirm `.claude/skills/dbos-python` is still
   the symlink and the lock's `computedHash` changed. Alternative rejected: copying the
   tarball and editing the hash by hand, because the hash algorithm could not be reproduced
   and a wrong hash makes the CLI's own drift check lie.

3. **The evidence command lives in `src/curate_d/staging/sdk_evidence.py`.** It follows
   `smoke.py` and `workflow_audit.py`: `python -m`, `argparse`, strict mypy, unit tests
   with a fixture matrix. Alternative: a script under `ops/` next to the matrix, rejected
   because `ops/*.py` is outside the type check and the unit suite's import path, and
   `reset.py` only lives there because it runs inside the image.

4. **Two subcommands, one parser.**
   - Both read `uv.lock` and `ops/staging-compatibility.md` relative to the working
     directory (the repository root, where the Make target runs). The command line takes no
     path, so nothing passed to it can point the matrix write at another file; the unit
     tests hand `main` their fixture paths as keyword arguments, as they hand it the runner.
   - `check`: reads the lock with
     `tomllib` (`package` entries named `aioboto3`, `aiobotocore`, `botocore`), parses the
     matrix's `| SDK |` row with a regex over the three backtick-quoted names, prints each
     drift as `<package>: matrix <a>, lock <b>`, exits 1 on drift or on an unparsable row, 0
     otherwise. `tests/unit/infrastructure/test_staging_sdk_evidence.py` calls it on the
     committed files, so drift fails `make test`.
   - `record --backend {rustfs,ovh} [--backend ...] [--date YYYY-MM-DD] [--dry-run]`: for
     each selected backend runs
     `uv run pytest tests/integration/test_staging_contract.py -m integration --no-cov
     --staging-backend=<b> --junitxml=<tmp>/<b>.xml -p no:cacheprovider`, then parses the
     JUnit XML (`xml.etree`): per-test outcome, and the `sdk_checksum_headers` property of
     `test_default_sdk_checksum_headers_are_accepted`. It rewrites:
       - the `| SDK |` row from the lock;
       - rows 32 and 33's cell for each run backend: `PASS — <date>, <identity>:
         test_default_sdk_checksum_headers_are_accepted under botocore <v>; headers
         <sorted header names and values, authorization excluded>` or `FAIL — <date>,
         <identity>: <failing test names>`;
       - rows 34 and 35's cell for each run backend from the whole suite's outcome
         (`PASS — <date>, <identity>: <n> tests signed s3v4 / region <r>` or `FAIL`);
       - rows 32–35's cell for each backend not selected: `PENDING — <date>: SDK moved to
         botocore <v>; previous evidence: <old cell>`; when the SDK did not move, the cell
         is left as is.
     The RustFS identity is read from the matrix header (`| RustFS server |` digest and
     `| RustFS endpoint |`); the OVH identity from `CURATE_D_TEST_OVH_ENDPOINT` and
     `CURATE_D_TEST_OVH_BUCKET` at run time (the names the contract fixtures already read).
     After rewriting it prints every other line that still names a previous SDK version.
     No report file (pytest did not run) exits 2 without writing. Any failing test exits 1
     after writing the `FAIL` cells, so a red run is recorded, as the matrix requires.
     These also exit 2 without writing, as runs that are not evidence:
       - a report in which no test passed: a stack that is down, or OVH settings the
         fixture refuses, errors every test, and says nothing about the SDK;
       - OVH selected without `CURATE_D_TEST_OVH_BUCKET` and `_ENDPOINT`, checked before
         the run;
       - an installed `aioboto3`, `aiobotocore` or `botocore` that is not the locked
         version (`importlib.metadata`), checked before any run: the cells name the lock's
         versions, so the suite must run on them. `make staging-sdk-evidence` runs through
         `uv run`, which syncs the environment first.
     The suite runs without the caller's `PYTEST_ADDOPTS`: a `-k` there would deselect
     tests, and a deselected test leaves no entry in the report to detect.
   The column order is taken from the table header line, not assumed, and a row whose cell
   count differs from the header is an error.

5. **`make staging-sdk-evidence`** runs `record` with `BACKENDS ?= rustfs` and a
   `DRY_RUN` switch, and the "How to refresh" row for SDK upgrades in the matrix and the
   `ops/README.md` tree point at it. `make lint` does not gain the `check`: the unit test
   already fails the gate and says what to run.

6. **The drift test fails the next Dependabot patch that moves `botocore`.** That is the
   matrix's stated policy ("Re-verify after any SDK upgrade"), now enforced; the cost is
   that the `python-patches` group PR stays red until someone runs `make
   staging-sdk-evidence` on RustFS and pushes the rewritten matrix, and the OVH cells read
   `PENDING` until the OVH leg runs. Alternative considered: a warning-only check, rejected
   because the matrix says a skipped check is never a pass, and a warning is a skipped
   check. If the owner prefers the warning, task 3.5 is the one to drop.

## Risks / Trade-offs

- [The reviewer saw integration failures this change could not reproduce] → The proposal
  asks for the test names or the run; stream 1 runs the gate on the branch and on a fresh
  `make test-stack`, where a DBOS 3.4 system-schema or pool-disposal change would surface.
- [`npx skills update` rewrites more than the one entry, or node is unavailable] → The
  lock has one entry; verify by `git diff skills-lock.json`. Node is installed through nvm.
- [Matrix table edits by regex corrupt a cell with a literal `|`] → The parser splits on
  ` | ` with the header's column count and refuses rows that do not match; unit tests cover
  a cell containing a backtick-quoted `|`.
- [JUnit property missing because the test was skipped or renamed] → `record` treats a
  missing property as a failure to record (exit 2, no write), and the unit test for the
  parser pins the property name against the test module's source.
- [Rolling back `dbos` after the skill refresh leaves guidance ahead of the library] →
  Decision 1 ties the two: a `dbos` rollback reverts the skill and the docs in the same task.

## Migration Plan

No schema or deployment step. DBOS 3.4 applies its own system migrations (124, 125) on the
first launch against a database, as 3.3 did; `migrations/env.py` applies them before
Alembic's. Rollback of the whole change is a revert of the lock and pins; DBOS's added
indexes are harmless under 3.3.

## Open Questions

None that change the specs or tasks. The failing tests the reviewer observed, if any, are an
input to stream 1, not to the plan's shape.

## Outcome

Local run, 2026-11-03 PT, after `dev` moved to `8746082` (PR #135):

- Integration (task 1.2): `make test-integration-parallel`, CI's four shards, each on a
  fresh stack of its own: 442 passed, 2 skipped, 0 failed (shards 110+1 skipped, 111, 111,
  110+1 skipped). No failing test to attribute to a package so far; CI on the pull request
  is still to run (tasks 1.1, 1.3).
- `make check`: lint, format, workflow audit and mypy (397 files) clean; 2180 unit tests
  passed, coverage 85.45%. One earlier run failed
  `test_parallel_integration.py::test_terminate_stops_a_running_command_and_refuses_new_ones`
  (exit code -1, expected -15) under xdist load; it passed 10 of 10 alone and in the next two
  full runs. It comes with PR #135, not this change.
- `make staging-sdk-evidence DRY_RUN=1` on a fresh `make test-stack`: 20 of 20 contract
  tests passed on RustFS, the checksum headers read from the JUnit report, nothing written;
  `check` reports the SDK row matches `uv.lock` (botocore 1.40.61). pytest warns that
  `record_property` is incompatible with `junit_family` xunit2, but still writes the property.

CI on PR #137 (first push, `c9650eb`): `Checks`, `Integration tests` (five shards and the
application boot), CodeQL, Gitar and Greptile green. Task 1.3: no failure attributed, so no
package is rolled back and no code change was needed for the updates.

SonarCloud's quality gate failed on the new code's security rating: five S8707 findings
(`--lock`/`--matrix` reach a file read and the matrix write unchecked), plus S3776
(`_sdk_sensitive_rows`' cognitive complexity) and S6019 (a lazy quantifier). The
`refactor(staging)` commit cleared S3776 and S6019. Confining the two paths to the working
directory did not clear S8707: Sonar does not take `Path.is_relative_to` as a sanitizer. The
options had no use outside the unit tests, so a second `fix(staging)` commit removes them
(decision 4): the command line takes no path at all.

Final diff against `dev` (task 4.1): 24 files, 2221 insertions, 174 deletions, all in the
streams' files: the vendored skill and `skills-lock.json`, `pyproject.toml` and `uv.lock`,
`sdk_evidence.py` and its two unit test modules, `Makefile`, `ops/README.md`,
`ops/staging-compatibility.md`, `README.md`, `openspec/config.yaml`, `CLAUDE.md`, the
synced `openspec/specs/content-staging/spec.md` and this change directory. `openspec validate
absorb-package-updates-2026-10 --strict` passed before the archive; `openspec validate
--specs` passes after it.

Review round on PR #137 (Gitar, Greptile, Codex), fixed in `fix(staging): record SDK evidence
only from a whole run on the locked SDK`: a report in which no test passed, OVH without its
settings, and an installed SDK other than the locked one now exit 2 without writing, and the
suite runs without the caller's `PYTEST_ADDOPTS`. A dry run on a fresh `make test-stack` with
`PYTEST_ADDOPTS=-k test_default_sdk_checksum_headers_are_accepted` still ran all 20 contract
tests. The delta is synced into `openspec/specs/content-staging/spec.md` and the change
archived in this PR.
