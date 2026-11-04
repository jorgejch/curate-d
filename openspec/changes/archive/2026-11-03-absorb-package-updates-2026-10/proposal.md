# Proposal

## Why

The working tree bumps nine Python packages at once (`dbos` 3.3.0 → 3.4.0, `logfire`
5.3.0 → 5.4.0, `httpx2` 2.13.1 → 2.16.0, `openai` 3.40.0 → 3.42.0, `pydantic` 2.14.1 →
2.14.2, `pydantic-settings` 2.16.1 → 2.16.2, `pypdf` 6.21.3 → 6.21.4, `pyrefly` 1.3.2 →
1.4.0, plus `pydantic-core` 2.50.1 → 2.50.2) and tightens several pins from `~=` to `==`.
The repository's own rules attach work to such a bump that nothing has done yet: the `dbos`
bump must refresh the vendored `dbos-python` skill and its `skills-lock.json` hash in the
same change (`CLAUDE.md`), and any SDK move must re-earn the staging evidence matrix
(`ops/staging-compatibility.md`, "How to refresh"). The matrix refresh is a hand procedure
today, so the next SDK bump will drift it again unless a command owns it.

## What Changes

- **Verify the bump against the whole gate and fix or roll back.** On `dev` at `62068e5`
  with the bumped lock installed: `make lint` passes, `make typecheck` (mypy) passes, the
  unit suite passes (2079 tests), and the integration suite against RustFS passes (442
  passed, 2 skipped, 17 min, 2026-10-31). **No integration failure reproduced locally.**
  The change therefore carries the fix as a conditional stream: run the gate again on the
  branch (CI's `integration.yml` runs on a `pyproject.toml`/`uv.lock` change), and when a
  test fails because of a package, fix it; when the fix for one package would exceed 400
  changed lines, pin that package back to its previous version (listed above) with a comment
  naming the incompatibility, and leave the rest of the bump in place. The reviewer is asked
  to name the failing tests they observed, or the run that showed them, so the stream has a
  target; without one it closes on a green gate.
- **Refresh the vendored DBOS skill to upstream `0bfe1dd` (skill 2.2.0, "Python 3.4").**
  The vendored copy is byte-identical to upstream `7fd8aef` (2.1.0, "Python 3.3"). The
  2.2.0 delta documents DBOS 3.4: `enable_patching` deprecated, `DBOS.reserve_step` for
  concurrent step sequences, and auto-upgrade (an enterprise feature curate.d does not use).
  Re-check the raw-SQL dependencies `CLAUDE.md` names: DBOS 3.4 adds system migrations 124
  and 125 (two indexes on `application_versions`, created `CONCURRENTLY`) and changes no
  table the views in migrations 0005/0007 or the retention prune read; the integration run
  above exercised `test_workflow_views.py` and `test_migrations.py` green.
- **Update the DBOS version the docs state**: `README.md` and `openspec/config.yaml` say
  "DBOS 3.3"; the config's "verified current as of" date moves to this change's date.
- **Add a repeatable SDK-evidence command for `ops/staging-compatibility.md`.**
  `aioboto3` did **not** move in this bump (`aioboto3` 15.5.0 / `aiobotocore` 2.25.1 /
  `botocore` 1.40.61 before and after), so the matrix needs no new evidence now. The
  command, `python -m curate_d.staging.sdk_evidence`, does two things: `check` compares the
  SDK versions in `uv.lock` with the matrix's `SDK` row and exits non-zero on drift;
  `record` runs the SDK-sensitive contract tests (rows 32–35, then the whole
  `test_staging_contract.py`) on the selected backends with a JUnit report, and rewrites the
  `SDK` row and the backend cells of rows 32–35 from the report: the captured checksum
  headers, the result, the date and the backend identity. A backend not run gets its rows
  32–35 cells marked `PENDING` with the previous evidence kept in the cell, because
  evidence earned under one SDK is not evidence about another. A unit test runs `check`
  against the committed lock and matrix, so the next SDK bump fails the unit suite until
  someone records new evidence. A `make staging-sdk-evidence` target wraps `record`.
- No change to the pin style the bump introduces (`==` for several packages): Dependabot's
  `versioning-strategy: increase` raises exact pins like ranges.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `content-staging`: adds two requirements under the compatibility section. The SDK
  versions the evidence matrix records MUST match the locked dependency set, checked by the
  unit suite; and re-recording the SDK-sensitive rows MUST be a single repeatable command
  that records only what it ran and marks what it did not run as pending.

## Impact

- `pyproject.toml`, `uv.lock`: already modified in the working tree; this change commits
  them, with any rollback pin the verification stream decides on.
- `.agents/skills/dbos-python/**`, `skills-lock.json`: refreshed through the `skills` CLI
  that wrote the lock (its `computedHash` is the CLI's own digest of the skill folder).
- `README.md`, `openspec/config.yaml`: DBOS version text.
- New `src/curate_d/staging/sdk_evidence.py`, `tests/unit/staging/test_sdk_evidence.py`,
  `tests/unit/infrastructure/test_staging_sdk_evidence.py`; `Makefile` target;
  `ops/staging-compatibility.md` ("How to refresh" row for SDK upgrades) and `ops/README.md`
  (tree and command).
- Policy consequence: a future Dependabot patch that moves `botocore` (any `aioboto3` or
  `aiobotocore` patch) fails the unit suite until `record` runs on RustFS, and the OVH cells
  read `PENDING` until it runs there too. That is the matrix's stated policy made
  mechanical; the alternative (a warning only) is recorded in design.md.
