# curate.d

Autonomous multi-cloud file and media organizer.

Curate.d ingests files and media from multiple cloud storage and photo providers,
classifies them by content and context, and files them into a consistent naming and
folder structure on a target cloud — running in the background without asking for
approval on every file.

> **Status: pre-release.** The organizer is implemented and verified end to end against a
> development deployment. See the [staging guide](docs/staging.md).

## The guarantee

Anything can move files between clouds. The part that matters here is not losing them.

- **Copy, verify, then register.** An original is never released until a copy has been
  independently verified to exist.
- **Verification is graded, not assumed.** Providers expose different hash algorithms and
  some content has no stable bytes to hash at all. Each placement records the strongest
  check actually achieved, and only strong levels can ever unlock source cleanup.
- **Retention is a policy outcome, not a pipeline stage.** A source may itself be one of
  the copies a redundancy rule requires, in which case deleting it would violate the rule
  the transfer existed to satisfy.
- **Uncertainty quarantines rather than guesses.** A stalled file is recoverable; a
  confidently misfiled one is not.

## Provider support

Not every provider can be read by an unattended service. This is a constraint imposed by
the providers, not a roadmap preference:

| Provider | Autonomous read | Writable | Notes |
| --- | --- | --- | --- |
| pCloud Drive / Photos | yes | yes | Strong hashes both ends. Photos is a view over the same tree. |
| Google Drive | yes | yes | Native Docs/Sheets have no stable bytes to verify. |
| OneDrive | yes | yes | |
| iCloud Drive / Photos | degraded | Drive only | Real Apple ID + 2FA; trust tokens last ~30 days. Photos is read-only. |
| Google Photos | **no** | append-only | Read scopes were removed in 2025. Bulk ingest must go via scheduled Takeout. |
| S3 / compatible | yes | yes | Strongest verification and storage-class control. |

pCloud is the first target, and so far the only one implemented, precisely because it is
the only one that is fully autonomous, writable, and strongly hashed on both ends — the
right place to prove the mechanics. The other providers are deferred to follow-on changes.

## How it works

```
  scan ──> fingerprint ──> classify ──> render ──> place ──> reconcile
   │            │              │           │         │           │
   │            │              │           │         │           └─ converges desired
   │            │              │           │         │              vs. observed state
   │            │              │           │         └─ copy, verify, register
   │            │              │           └─ facts + template -> locator
   │            │              └─ metadata -> local CPU -> LLM, stopping when confident
   │            └─ content hash + perceptual hash (cross-cloud dedup)
   └─ incremental change detection, plus periodic full rescan
```

Two ideas do most of the work:

**Classification emits facts, never paths.** A separate versioned renderer turns facts
into a destination locator. Identical facts always produce identical paths, and changing
the folder layout is a re-render from stored facts — no reclassification, no inference cost.

**Reconciliation is a control loop, not a pipeline.** It compares the placements a file
*should* have against the ones it *does* have and emits corrective actions. Tighten a
redundancy rule and the existing library repairs itself on the next pass; lose a file at a
destination and it comes back.

## Tech stack

Python application driving [rclone](https://rclone.org/) over its remote-control API, with
PostgreSQL doing considerably more than usual: ledger, work queue, durable-execution store
and audit log all in one instance.

Durable execution is [DBOS](https://docs.dbos.dev/) 3.2, chosen as a library rather than a
service so that ledger writes go through a datasource transaction and are exactly-once
across crashes and replays. An optional connection to DBOS Conductor, off by default, shows
workflows in the DBOS Console, read-only: every write from the Console is refused
([guide](docs/conductor.md)).

Designed for Kubernetes. No GPU on any node — all local
inference is CPU-only.

Full stack with pinned versions lives in [`openspec/config.yaml`](openspec/config.yaml).

## System dependencies

Install these before `make install`; they're not pulled in by `uv`.

| Dependency | Used for | Install |
| --- | --- | --- |
| [`uv`](https://docs.astral.sh/uv/) | Python env + dependency management | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| Python 3.12+ | runtime | `uv python install 3.12` (or via your OS package manager) |
| Docker + Compose | local PostgreSQL/PostGIS, RustFS and rclone stack, container image build | [docs.docker.com](https://docs.docker.com/get-docker/) |
| [`tesseract`](https://github.com/tesseract-ocr/tesseract) 5.5 | OCR path during classification | macOS: `brew install tesseract` · Debian/Ubuntu: pin `tesseract-ocr=5.5.*` from a compatible repository or build from source |
| [`rclone`](https://rclone.org/) 1.75.1 | cloud provider transfers | macOS: `brew install rclone` · Debian/Ubuntu: install pinned upstream package `rclone-v1.75.1-linux-*.deb` |

## Development

```bash
make install           # install, including the dev group
make stack-up          # PostgreSQL, RustFS, rclone, API and workers via Docker Compose
make stack-down        # stop the local stack, keeping its data
make stack-reset       # destroy the local stack and its data
make migrate           # apply the Alembic migrations to the configured database
make staging-init      # re-run RustFS staging provisioning and its access-matrix check
make lifecycle-check   # observe lifecycle expiry and abort on a time-scaled RustFS, in seconds
make format            # apply formatting and autofixable lint
make lint              # ruff check + format check + workflow database-access audit
make typecheck         # mypy, strict
make test              # isolated unit tests + 80% coverage gate
make coverage          # unit coverage data in curate-d.coverage, importable by PyCharm
make test-integration  # integration tests (needs the local stack)
make check             # lint + typecheck + test; CI also runs the staging integration checks
make image             # build the container image
```

Isolated unit tests are the minimum for any behavior — no database, network, filesystem,
rclone, or model calls. Outbound connections are blocked in the unit suite so a test that
reaches for the network fails rather than becoming intermittently slow. The coverage floor
is measured against the unit suite alone; integration coverage does not count toward it.

### Coding agents and DBOS

[`CLAUDE.md`](CLAUDE.md) (also [`AGENTS.md`](AGENTS.md)) holds the conventions agents
follow here. Before designing or changing anything that touches DBOS — a workflow, step,
queue, schedule, datasource, client or Conductor call — load the **`dbos-python` skill**
from [dbos-inc/agent-skills](https://github.com/dbos-inc/agent-skills). It carries DBOS's
own patterns, conventions and architecture guidance; `CLAUDE.md` lists where this repo
departs from it. The skill is vendored in `.agents/skills/dbos-python`, linked from
`.claude/skills/dbos-python` and pinned in `skills-lock.json`. To take a newer upstream
version, re-run the install and commit what changes:

```bash
npx skills add dbos-inc/agent-skills --skill dbos-python -a claude-code codex -y
```

## Content staging

RustFS is the local/CI backend through the application S3 adapter. It replaced MinIO,
whose upstream is archived ([staging guide](docs/staging.md#local-engine-rustfs)).
I/O workers publish immutable objects; CPU workers use a separate read-only identity and
bounded, verified local snapshots. S3 readiness fails without filesystem fallback. Seed the
readiness marker with provisioning smoke or I/O startup before starting CPU.

See the [runtime and configuration guide](docs/staging.md),
[provisioning smoke invocations](infrastructure/README.md#provisioning-smoke-check), and
[compatibility evidence](infrastructure/staging-compatibility.md). The publishing workflow
runs the shared contract suite, the backend checks and the time-scaled lifecycle check on
RustFS; OVH is verified separately against the real buckets.

## Repository layout

```
  src/curate_d/     application code
  tests/unit/       isolated tests; no I/O, coverage gate applies here
  tests/integration/  tests that need the local stack: Postgres, RustFS and the rclone sidecar
  migrations/       Alembic migrations for the ledger schema
  docker/           container image build
  docs/             staging, Conductor and reset guides, verification records, diagrams
  infrastructure/   local stack config and operator scripts; deployment manifests
                    are kept outside this repository
  .github/          image publishing and unit-test workflows
  openspec/
    config.yaml     project context and pinned tech stack
    specs/          current capability specifications
  CLAUDE.md         conventions for coding agents (AGENTS.md links to it)
  skills-lock.json  pinned agent skills (dbos-python)
```

This project uses OpenSpec: behaviour is specified before it is built, and each change
carries its own proposal, capability specs, design rationale, and task breakdown.

## License

Apache License 2.0; see [LICENSE](LICENSE) and [NOTICE](NOTICE).
