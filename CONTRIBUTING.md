# Contributing to curate.d

curate.d moves people's files between clouds and may eventually release the originals, so
the bar for a change is that it can't lose data, even on a crash, a retry or a replay. Most of
what follows serves that one guarantee.

## Before you start

- **Open an issue first** for anything beyond a small fix, so the approach can be agreed on
  before you write it.
- **Report security problems privately** through
  [GitHub's private vulnerability reporting](https://github.com/jorgejch/curate-d/security/advisories/new),
  never in a public issue. See the [security policy](SECURITY.md).
- **Follow the [Code of Conduct](CODE_OF_CONDUCT.md)** in issues, pull requests and reviews.

## Setup

Install the [system dependencies](README.md#system-dependencies) (uv, Python 3.12, Docker,
tesseract, rclone), then:

```bash
make install      # project + dev group
make test-stack   # for integration tests; destroys the local stack's data first
make help         # every target
```

## How a change is made

Every change except a simple fix (a typo, a one-line bug fix, a doc correction) goes through
[OpenSpec](openspec/): behaviour is specified before it is built.

1. **Propose**: create a change under `openspec/changes/<name>/` with its proposal, delta
   specs, design and `tasks.md`.
2. **Apply**: implement the tasks, one logical commit per task, each with a test or a
   concrete verification command.
3. **Sync**: merge the change's delta specs into `openspec/specs/`.
4. **Archive**: `openspec archive <name>`.

Spec requirements are atomic, testable GIVEN/WHEN/THEN or MUST/SHALL statements, and each one
states what happens on failure, not just on the happy path. Group `tasks.md` into work streams
that touch disjoint files so the apply can be parallelized; keep shared edits (a migration, a
settings field, `CLAUDE.md`) in one stream or one up-front task, and say so in the design when
a change can't be split.

Stay within the files the change's tasks name. No drive-by reformatting, import reordering or
cleanup of unrelated code.

## Branches, commits and pull requests

- **Branch from `dev` and open the pull request against `dev`.** `dev` is the active,
  deployed branch; `main` only receives releases, and a semver tag on `main` publishes the
  production image.
- **Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/)**:
  `feat:`, `fix:`, `refactor:`, `test:`, `docs:`, `chore:`, `spec:`, with a scope where one
  helps (`fix(recovery): ...`). Summaries are imperative and lower-case.
- **Run `make check` before pushing.** It runs the same lint, type check and unit commands
  as CI, so green locally means green there. Every push to a branch runs them and uploads
  the unit coverage to SonarQube Cloud. A pull request into `dev` or `main` must pass them
  and, when it changes a path the suite depends on, the full integration suite against the
  local stack, and be up to date with its base, before it merges; a merge runs no tests
  again, only the image build on `dev` and the SonarQube coverage refresh.
- **Gitar reviews pull requests.** [`.gitar/config/apply.md`](.gitar/config/apply.md)
  bounds which of its code-review findings it may fix by itself, on top of the organization's
  criteria. Gitar reads it from `dev`, so a pull request can't change the criteria for its own
  fixes, and the file turns nothing on: auto-apply is enabled per pull request.

## Tests

- **Isolated unit tests are the minimum for any behaviour**: no database, network,
  filesystem, rclone or model calls. Outbound connections are blocked in the unit suite.
- **The 80% coverage floor counts unit tests only.** Integration tests are extra assurance
  and don't count toward it. Coverage is a floor, not a goal: a test that pins one failure
  mode is worth more than three that repeat the success path.
- **If a function can only be tested against a live database**, split the decision from the
  I/O that carries it out.
- **A test isn't written until it has run green**, along with the whole unit suite. Lint and
  type checks passing proves nothing about behaviour. Fix whichever of the code or the test
  is wrong; never delete, loosen or skip an assertion to get a pass.
- **Integration tests** (`make test-integration`) need `make test-stack` first. While
  developing, run the ones your change touches (`uv run pytest
  tests/integration/test_<area>.py --no-cov -m integration --staging-backend=rustfs`; without
  the flag, staging tests also need the OVH fixture's credentials); a pull request that
  changes a path the suite depends on runs the full suite.
- **The pull request runs the suite in shards**, four by default (the repository variable
  `INTEGRATION_SHARDS` changes the count without a commit), each on its own runner against
  its own stack. To rerun one shard as CI ran it, run
  `make test-stack && make test-integration GROUP=<n>`; `SPLITS` defaults to CI's four. Run
  `make test-durations` against a `make test-stack` stack and commit `.test_durations` when
  you add or remove a slow test, or when the shard times in the `Integration tests` summary
  drift apart.
- **Don't run them against `make stack-up`.** Its workers apply the persisted schedules, and
  the suite's own DBOS process then fires them: a recovery or refresh tick runs against the
  test rows. `make test-stack` runs `make stack-reset` first, which destroys the
  local stack's containers and data (including schedules an earlier `stack-up` wrote), so
  the suite starts from a clean database as in CI.

## Code conventions

The full list is in [`openspec/config.yaml`](openspec/config.yaml) and
[`CLAUDE.md`](CLAUDE.md). In short:

- **Comments say why, not what.** Express intent through names and strict types first.
- **Contract-first typing.** `mypy --strict` must pass; no untyped `dict`s or `Any` standing
  in for a schema.
- **Errors carry context**: the operation, the resource and the cause. Never swallow one.
- **Every transfer, rename or state transition is safe to retry.** Copy, verify, then
  register, before anything touches a source.
- **When a value is uncertain, quarantine instead of guessing.**
- **Docstrings are Google style with an imperative one-line summary** ("Return the...", not
  "Returns the..."); see [`CLAUDE.md`](CLAUDE.md#docstrings) for the full rules, including
  migrations and tests.

### Rules the build enforces

`make check` fails on these:

| Rule | Enforced by |
| --- | --- |
| All ledger mutations executed within DBOS workflows MUST use a DBOS datasource transaction defined through `runtime/datasource.py`. Never perform ledger mutations through `@DBOS.step()`, unmanaged SQLAlchemy sessions, or direct database access within workflow functions. Ledger mutations and their DBOS execution checkpoints must commit atomically. | `python -m curate_d.runtime.workflow_audit`, in `make lint` |
| No private library internals (an underscore module or attribute, or a copy of private logic or SQL) without the maintainer's approval. | `tests/unit/test_no_library_internals.py`, ruff `TID251` |
| No source file, test, migration or Make target reads another repository on the machine. | `tests/unit/test_isolation.py` |

### DBOS

Durable execution is DBOS 3.x, embedded as a library. Before designing or changing a
workflow, step, queue, schedule, datasource or `DBOSClient` call, read the
[`dbos-python` skill](.agents/skills/dbos-python), then the [DBOS section of
`CLAUDE.md`](CLAUDE.md#dbos), which lists where this repository departs from it and why. A
new departure is recorded there in the same pull request. In particular:

- Construct DBOS only through `runtime/dbos_app.py`, and register queues through
  `runtime/queues.py`.
- Keep workflow IDs deterministic (`runtime/workflow_ids.py`), so duplicate enqueues
  collapse.
- Declare periodic work as a persisted schedule in `schedules_for`
  (`workflows/registry.py`).
- A new ERROR event that signals drift needs a periodic repair and an entry in
  [`docs/recovery.md`](docs/recovery.md)'s alert list. Detection alone isn't a guarantee.

### Migrations

Alembic, in `migrations/versions/`, numbered in sequence (`0021_<what>.py`). The module
docstring states what the migration does; `upgrade()` and `downgrade()` each get a one-line
docstring naming what they act on. A migration that reads DBOS's tables is a schema
dependency: cover it in `tests/integration/test_migrations.py`.

## Coding agents

Agents follow [`CLAUDE.md`](CLAUDE.md) (also [`AGENTS.md`](AGENTS.md)). The same rules apply
to agent-written changes, and the person opening the pull request answers for them.

## License

curate.d is licensed under the [Apache License 2.0](LICENSE). By contributing, you agree that
your contributions are licensed under the same terms.
