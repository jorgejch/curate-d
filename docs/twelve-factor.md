# Twelve-Factor adoption

curate.d adopts the Twelve-Factor manifesto as a guiding principle for the contract between
the application and the platform it runs on. This page names the text adopted, states where
that text ranks against the other standards, and records the status of each of its 33 rules.

## The pinned text

| | |
| --- | --- |
| Repository | [`twelve-factor/twelve-factor`](https://github.com/twelve-factor/twelve-factor/tree/3ad5a5f36312cc8ad876abae1bd691acd790d4d1) |
| Branch | `next` |
| Commit | `3ad5a5f36312cc8ad876abae1bd691acd790d4d1` (2025-07-10) |
| Read on | 2026-11-04 |
| Licence | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) |

The manifesto is the work of its contributors and is licensed under CC BY 4.0. The short
descriptions of the rules on this page are paraphrases, not the manifesto's text. Read the
rule itself through the link under each factor.

The `next` branch is a living draft, so conformance is stated against this commit and not
against the head of the branch. The branch's unmerged work and the 2017 text of 12factor.net
are not part of the adoption.

To move the pin, make one change that does all of this:

1. Read the text at the new commit again.
2. Update the row of every rule that the new text adds, removes, renumbers or rewords.
3. Set the new read date.
4. Move the commit here and `PINNED_COMMIT` and `RULE_COUNTS` in
   [`tests/unit/infrastructure/test_engineering_standards.py`](../tests/unit/infrastructure/test_engineering_standards.py).

## Precedence

Three standards govern how curate.d is built, under the safety guarantees. On a conflict the
earlier entry wins; a later one never overrides it.

1. **Safety guarantees**: above the standards. [`README.md`, "The guarantee"](../README.md#the-guarantee),
   and the Project paragraph of [`openspec/config.yaml`](../openspec/config.yaml).
2. **DBOS**: the vendored [`dbos-python` skill](../.agents/skills/dbos-python), as amended by
   the departures recorded in [`CLAUDE.md`, "DBOS"](../CLAUDE.md#dbos).
3. **Twelve-Factor**: the text pinned above, with the status of each rule below.
4. **Coding standards**: [`openspec/config.yaml`](../openspec/config.yaml), "Coding &
   Documentation Conventions", with its versioning and commit rules.

Each rule has one of three statuses:

- **`Conforms`**: curate.d meets the rule.
- **`Limited by DBOS`**: the skill, or a departure recorded in `CLAUDE.md`, requires the
  behaviour that keeps the rule unmet. The row names the skill reference. A choice that the
  skill only permits is curate.d's own, so it counts as conformance or as a gap.
- **`Gap`**: curate.d does not meet the rule. The row states what is unmet and on which side:
  the application, the deployment, or both. Where a safety guarantee keeps a rule unmet, the
  row is a `Gap` and names the guarantee. No row does so today.

## Status

Assessed 2026-11-04 against DBOS 3.4.0 and `dbos-python` skill 2.2.0. On that date 16 rules
conformed, 2 were limited by DBOS and 15 were gaps. Rows III.2 and IX.1 were re-read against
DBOS 3.5.0 and `dbos-python` skill 2.2.0 on 2026-11-12 and keep their status. The deployment
manifests are kept outside this repository; a row that mentions them states the finding in
general terms.

### I. Codebase

[Read the factor](https://github.com/twelve-factor/twelve-factor/blob/3ad5a5f36312cc8ad876abae1bd691acd790d4d1/content/codebase.md).

| Rule | Status | Evidence, or what is unmet | Authority or tracking |
| --- | --- | --- | --- |
| I.1 | Conforms | *In version control.* One git repository holds the whole application. | - |
| I.2 | Conforms | *One codebase, one app.* One image serves the API and both worker profiles (`docker/Dockerfile`). | - |
| I.3 | Conforms | *Many deploys.* The local, dev and prod deploys run the same codebase. | - |

### II. Dependencies

[Read the factor](https://github.com/twelve-factor/twelve-factor/blob/3ad5a5f36312cc8ad876abae1bd691acd790d4d1/content/dependencies.md).

| Rule | Status | Evidence, or what is unmet | Authority or tracking |
| --- | --- | --- | --- |
| II.1 | Gap | *Deterministic build and run.* `uv sync --locked --no-build` and digest-pinned base images hold, but `TESSERACT_VERSION=5.5.*` floats in `docker/Dockerfile`, and the deployment manifests take a minor-version tag of the rclone image where `compose.yaml` pins the patch. | both |
| II.2 | Conforms | *No implicit system packages.* `pyproject.toml` and `uv.lock`, installed into `.venv`; the image and a development machine resolve the same set. | - |
| II.3 | Gap | *No implicit system tools.* A run outside the image needs the `tesseract` binary on `PATH` (`README.md`, "System dependencies"). | application |

### III. Config

[Read the factor](https://github.com/twelve-factor/twelve-factor/blob/3ad5a5f36312cc8ad876abae1bd691acd790d4d1/content/config.md).

| Rule | Status | Evidence, or what is unmet | Authority or tracking |
| --- | --- | --- | --- |
| III.1 | Conforms | *Config apart from code.* `Settings` (`src/curate_d/config.py`) reads `CURATE_D_*` variables, and the repository holds no deploy credentials: its only passwords are the local stack's defaults (`ledger_database_password`, `compose.yaml`). The ledger connection comes from one URL setting or from part settings. Policies, templates and the taxonomy in the database are versioned application data, not deploy config. | - |
| III.2 | Limited by DBOS | *Config in environment variables.* Config is read from the environment, except queue limits: DBOS keeps them in its system database and changes them at runtime (`src/curate_d/runtime/queues.py`, `never_update`). | [`references/queue-management.md`](../.agents/skills/dbos-python/references/queue-management.md), [`references/queue-concurrency.md`](../.agents/skills/dbos-python/references/queue-concurrency.md) |
| III.3 | Gap | *Granular controls, not environment groups.* `CURATE_D_ENVIRONMENT` switches the alerting test routes (`src/curate_d/config.py`) and names the DBOS application (`src/curate_d/runtime/dbos_app.py`). A fix keeps the application name stable: the skill treats it as the ownership key. | application |

### IV. Backing services

[Read the factor](https://github.com/twelve-factor/twelve-factor/blob/3ad5a5f36312cc8ad876abae1bd691acd790d4d1/content/backing-services.md).

| Rule | Status | Evidence, or what is unmet | Authority or tracking |
| --- | --- | --- | --- |
| IV.1 | Conforms | *Relies on backing services.* PostgreSQL, S3 staging, the rclone daemon, OpenRouter and Logfire are reached over the network. | - |
| IV.2 | Conforms | *Attachable resources.* Each is a distinct resource named in config; the staging backend changes from RustFS to a hosted S3 by config alone. | - |
| IV.3 | Gap | *Connection strings in config.* The OpenRouter base URL (`src/curate_d/classification/remote.py`) and the pCloud authorize URL (`src/curate_d/providers/pcloud/oauth.py`) are constants in code. | application |

### V. Build, release, run

[Read the factor](https://github.com/twelve-factor/twelve-factor/blob/3ad5a5f36312cc8ad876abae1bd691acd790d4d1/content/build-release-run.md).

| Rule | Status | Evidence, or what is unmet | Authority or tracking |
| --- | --- | --- | --- |
| V.1 | Conforms | *Distinct stages.* Build is the image (`.github/workflows/docker-publish.yml`); release is the image tag plus config, applied through GitOps; run is the container command. | - |
| V.2 | Gap | *Immutable releases.* A change of config or of a secret restarts no process and gets no new release identifier. | deployment |
| V.3 | Gap | *Complexity in the build, minimal run.* Schema migration runs at process start: the deployment manifests run `alembic upgrade head` when the API starts, and the workers leave `run_migrations` at its default for DBOS and for the datasource (`src/curate_d/runtime/dbos_app.py`, `src/curate_d/runtime/datasource.py`). The skill supports the fix (`references/lifecycle-config.md`, "Least-Privilege Deployments"). | both; [#7](https://github.com/jorgejch/curate-d/issues/7) |

### VI. Processes

[Read the factor](https://github.com/twelve-factor/twelve-factor/blob/3ad5a5f36312cc8ad876abae1bd691acd790d4d1/content/processes.md).

| Rule | Status | Evidence, or what is unmet | Authority or tracking |
| --- | --- | --- | --- |
| VI.1 | Conforms | *One or more processes.* Three process types: API, I/O worker, CPU worker. | - |
| VI.2 | Conforms | *Stateless, share nothing.* No process relies on what it cached in memory or on disk: scratch space is disposable (`staging_local_scratch_root`), and rclone remotes are rebuilt from the ledger (`src/curate_d/placement/remotes.py`). The stable executor identity (`src/curate_d/runtime/identity.py`) and the executor lease (`src/curate_d/runtime/lease.py`) are curate.d's own choices, and do not limit this rule. | - |

### VII. Port binding

[Read the factor](https://github.com/twelve-factor/twelve-factor/blob/3ad5a5f36312cc8ad876abae1bd691acd790d4d1/content/port-binding.md).

| Rule | Status | Evidence, or what is unmet | Authority or tracking |
| --- | --- | --- | --- |
| VII.1 | Conforms | *Self-contained, binds a port.* The API binds its own port with uvicorn (`src/curate_d/api/__main__.py`). | - |
| VII.2 | Conforms | *Services over a port.* The rclone daemon is consumed by URL, like any other service. | - |

### VIII. Concurrency

[Read the factor](https://github.com/twelve-factor/twelve-factor/blob/3ad5a5f36312cc8ad876abae1bd691acd790d4d1/content/concurrency.md).

| Rule | Status | Evidence, or what is unmet | Authority or tracking |
| --- | --- | --- | --- |
| VIII.1 | Conforms | *Programs run as processes.* Work is split by process type, each started by an explicit command. | - |
| VIII.2 | Conforms | *Processes are first-class.* No process daemonizes or writes a PID file. | - |
| VIII.3 | Gap | *Scale out by process type.* `provider_requests_per_minute` feeds an in-memory pacer in each process (`src/curate_d/providers/pacing.py`), so more I/O workers raise the request rate at the provider. DBOS queue limiters are global but count started workflows, not requests. | application |
| VIII.4 | Conforms | *The platform manages processes.* The platform starts, restarts and stops the processes. | - |

### IX. Disposability

[Read the factor](https://github.com/twelve-factor/twelve-factor/blob/3ad5a5f36312cc8ad876abae1bd691acd790d4d1/content/disposability.md).

| Rule | Status | Evidence, or what is unmet | Authority or tracking |
| --- | --- | --- | --- |
| IX.1 | Limited by DBOS | *Disposable processes.* The workers of an application version stay until that version's workflows drain. | [`references/advanced-versioning.md`](../.agents/skills/dbos-python/references/advanced-versioning.md); [#74](https://github.com/jorgejch/curate-d/issues/74) |
| IX.2 | Gap | *Fast startup.* Startup time is not measured for the API or the workers, and the executor lease wait can add to it. | both |
| IX.3 | Gap | *Graceful shutdown.* The worker handles SIGTERM and drains (`src/curate_d/worker/main.py`, `DRAIN_SECONDS`), but the rclone sidecar gets the signal at the same moment, and the drain is shorter than the platform's grace period. | both; [#74](https://github.com/jorgejch/curate-d/issues/74) |
| IX.4 | Conforms | *Robust against sudden termination.* Durable workflows, deterministic workflow IDs, the recovery sweep, and copy, verify, register. | - |

### X. Dev/prod parity

[Read the factor](https://github.com/twelve-factor/twelve-factor/blob/3ad5a5f36312cc8ad876abae1bd691acd790d4d1/content/dev-prod-parity.md).

| Rule | Status | Evidence, or what is unmet | Authority or tracking |
| --- | --- | --- | --- |
| X.1 | Gap | *Small gaps between environments.* No production deploy exists yet, and `main` trails `dev`. | release process |
| X.2 | Gap | *Same backing services everywhere.* RustFS locally and in CI, a hosted S3 when deployed; no connection pooler locally or in CI; different PostgreSQL images. | both; [#48](https://github.com/jorgejch/curate-d/issues/48) |

### XI. Logs

[Read the factor](https://github.com/twelve-factor/twelve-factor/blob/3ad5a5f36312cc8ad876abae1bd691acd790d4d1/content/logs.md).

| Rule | Status | Evidence, or what is unmet | Authority or tracking |
| --- | --- | --- | --- |
| XI.1 | Gap | *A stream of time-ordered events.* The event stream goes to `stderr` (`src/curate_d/observability/logging.py`); the rule names `stdout`. | application |
| XI.2 | Gap | *No routing or storage of the stream.* Each process also ships its log records to Logfire. Who routes logs is a decision, and a fix touches the `telemetry-export` capability. | both |

### XII. Admin processes

[Read the factor](https://github.com/twelve-factor/twelve-factor/blob/3ad5a5f36312cc8ad876abae1bd691acd790d4d1/content/admin-processes.md).

| Rule | Status | Evidence, or what is unmet | Authority or tracking |
| --- | --- | --- | --- |
| XII.1 | Gap | *Admin tasks as one-off processes.* The deployment manifests run the migrations on every API start, with no lock (`migrations/env.py`), and the workers neither wait for them nor check the ledger schema. | both; [#7](https://github.com/jorgejch/curate-d/issues/7) |
| XII.2 | Gap | *Admin processes in the same environment.* `ops/reset/reset.py` is not in the image (`.dockerignore` excludes `ops/`). The other admin commands ship in the package. | application |

## Keeping this page true

The design of a change names each Twelve-Factor rule the change departs from or brings into
conformance, and the change updates that rule's row here, in the same pull request. A
departure names the higher standard that requires it. `openspec/config.yaml` carries this as
a design rule.

A rule leaves `Gap` only in the change that brings it into conformance or records the
standard that limits it. No test can tell that a status is true, so review keeps the rows
honest. `tests/unit/infrastructure/test_engineering_standards.py` pins what a test can: the
order of precedence, the pinned commit, one row and one status for each rule, a skill
reference for each `Limited by DBOS` row, and a statement for each `Gap` row.

A change that refreshes the `dbos-python` skill reads the two `Limited by DBOS` rows again.
