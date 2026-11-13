# Proposal

## Why

The branch moves the runtime to DBOS 3.5.0 and refreshes the packages that move with it.
DBOS 3.5.0 adds system migrations 126 to 131 (a `suspend_key` column, a `SUSPENDED` status,
new partial indexes, a replaced `send_message` function, an `external_objects` table) and reworks the scheduler and queue
locking. Those areas feed the recovery sweep, the schedule authority, and the migrations
that read DBOS tables by raw SQL, so the bump must be verified against the full integration
suite on a fresh stack before merge. The bump also has to refresh the vendored `dbos-python`
skill in the same change, as `CLAUDE.md` requires. Pyrefly is a development tool, so it must
not ship in the production image.

## What Changes

- Pin `dbos[otel]` to 3.5.0 (from 3.4.0).
- Raise `psycopg[binary]` to `~=3.3.9` (from `~=3.3.6`). `uv.lock` fixes the exact release.
- Pin `logfire[fastapi]` to 5.6.0 (from 5.4.0).
- Pin `openai` to 3.50.0 (from 3.46.0).
- Move `pyrefly==1.4.0` from runtime dependencies to the `dev` group.
- Refresh `.agents/skills/dbos-python` and `skills-lock.json` from upstream, and verify the
  vendored folder matches upstream. The upstream skill is unchanged since skill version 2.2.0,
  so the refresh confirms the content and does not change it.
- Change the current DBOS version text in `openspec/config.yaml` and `README.md` from 3.4
  to 3.5.
- Re-read the two `Limited by DBOS` rows in `docs/twelve-factor.md` (III.2 and IX.1), as the
  table requires after a skill refresh, and record that re-read on the page's dated
  assessment line. The existing 3.4.0 record stays.
- Add a unit-tested requirement that the DBOS version named in those documents matches the
  pinned dependency, so the text cannot drift again.
- Rebuild the branch history so each commit is one logical change and the lock matches
  `pyproject.toml` at every commit.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `engineering-standards`: adds a requirement that the DBOS version named in the repository
  documents matches the `dbos` pin in `pyproject.toml`, checked by a unit test.

## Impact

- **Dependencies**: `pyproject.toml` and `uv.lock`. The `dbos` bump brings a new DBOS
  runtime and a newer system schema.
- **Production image**: `pyrefly` no longer installs under `uv sync --no-dev`.
- **Database**: the first `alembic upgrade head` or worker launch on 3.5.0 applies DBOS
  migrations 126 to 131 to the `dbos` schema of the ledger database.
- **Documentation**: `openspec/config.yaml`, `README.md`, `docs/twelve-factor.md`, and
  `.agents/skills/dbos-python` with `skills-lock.json`.
- **Runtime behavior**: the change has no code edits for the DBOS 3.5.0 changes, so their
  effect is verified by the integration suite on a fresh `make test-stack`, not assumed.
- **Non-goals**: no application code changes, no curate.d migrations, and no change to the
  `psycopg`, `logfire` or `openai` call sites. `.editorconfig` is not part of this change. It
  moves to a branch and pull request of its own.
