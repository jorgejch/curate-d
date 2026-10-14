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
make install    # project + dev group
make stack-up   # PostgreSQL, RustFS, rclone, API and workers, for integration tests
make help       # every target
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
  as CI, so green locally means green there. Every push to a branch runs the unit suite;
  pushes to `dev` and `main`, and pull requests into `main`, also run the staging
  integration checks.

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
- **Integration tests** (`make test-integration`) need the local stack. While developing, run
  the ones your change touches (`uv run pytest tests/integration/test_<area>.py --no-cov -m
  integration`); CI runs the full suite.

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
| Every ledger write inside a workflow goes through the ledger datasource (`runtime/datasource.py`), never a step or a raw session. | `python -m curate_d.runtime.workflow_audit`, in `make lint` |
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
