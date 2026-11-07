# Design

## Context

- The proposal gives the motivation and the scope. This file settles how each edit is made.
- The manifesto text is read from `twelve-factor/twelve-factor`, branch `next`, at commit
  `3ad5a5f36312cc8ad876abae1bd691acd790d4d1` (2025-07-10), read on 2026-11-04. It is licensed
  CC BY 4.0 and holds 12 factors with 33 numbered rules: three each for I to V, two each for
  VI and VII, four each for VIII and IX, two each for X to XII. The text is a living draft;
  its unmerged branches (`identity`, `dynamic-config`) are not part of the adoption.
- The audit ran on `dev` at `854774f`, against DBOS 3.4.0 (`uv.lock`) and the vendored
  `dbos-python` skill 2.2.0 (`.agents/skills/dbos-python`, pinned in `skills-lock.json`).
  It found 16 rules met, 2 limited by DBOS and 15 unmet.
- `openspec/config.yaml` is injected into every OpenSpec artifact prompt (8,586 bytes of a
  51,200-byte limit). `## Standards` holds two bullets today, and `rules:` has no `design`
  list. `sonar-project.properties` cites "openspec/config.yaml, §8", so the numbering of the
  conventions can't move.
- `CLAUDE.md` is loaded into every agent session; `AGENTS.md` is a tracked link to it. The
  anchors `#changes`, `#docstrings` and `#dbos` are referenced from `CONTRIBUTING.md`,
  `docs/reset.md` and `.gitar/config/`.
- Git tracks `.claude/skills/dbos-python` as a link to `../../.agents/skills/dbos-python`.
  The working tree holds a directory in its place, today byte-identical to the vendored copy,
  and `git status` reports the link as deleted.
- `tests/unit/infrastructure/` already pins repository configuration by parsing it from disk
  (`test_readme_badges.py`, `test_sonar_properties.py`). No test reads `CLAUDE.md`,
  `openspec/config.yaml` or the skill paths today.
- The repository is public. The deployment manifests are kept outside it.

## Goals / Non-Goals

**Goals:**
- One stated order between the rule sets, written the same way in three files and pinned by
  a test.
- A record of where curate.d stands on each of the 33 rules, which a later change can update
  one row at a time.
- DBOS guidance stays above Twelve-Factor, and a rule may claim "Limited by DBOS" only by
  naming the skill reference that limits it.
- One copy of the DBOS authority in the repository.

**Non-Goals:**
- Closing any gap (D14).
- Following the head of `next`. Conformance is stated against the pinned commit (D1).
- Checking in CI that the pinned text still exists upstream or that a status is true. The
  unit suite blocks the network, and a status is a judgement that review keeps honest.
- Copying the manifesto into the repository. The record paraphrases a rule in a few words
  and links to the pinned text.
- Any check on the deployment manifests. Those checks live with the manifests
  (`CLAUDE.md`, "Repository boundaries").

## Decisions

**D1. Pin a commit of `next`, and hold the same commit in the test.** The record names the
repository, the branch, the full SHA, the commit date, the read date and the licence. Every
link to the manifesto goes through the SHA, never through `next`. The test holds the SHA as
a constant beside the rule counts of each factor, and fails if the record names another
commit. Moving the pin is then one change that re-reads the text, reviews the rule counts
and every row the new text touches, sets the new read date, and moves the constant and the
record together. Alternatives: follow the branch (the rules a status refers to could change
with no diff here); adopt the 2017 text of 12factor.net (the owner asked for the current
draft, and its numbered rules are what make a per-rule record possible).

**D2. Precedence, and where each authority lives.**

| Rank | Standard | Authority in the repository |
| --- | --- | --- |
| 1 | Safety guarantees (above the umbrella) | `README.md`, "The guarantee"; the Project paragraph of `openspec/config.yaml` |
| 2 | DBOS | `.agents/skills/dbos-python`, as amended by the departures recorded in `CLAUDE.md`, "DBOS" |
| 3 | Twelve-Factor | the pinned text; `docs/twelve-factor.md` records the pin and the status of each rule |
| 4 | Coding standards | `openspec/config.yaml`, "Coding & Documentation Conventions" and the two bullets on versioning and commits |

An earlier rank wins a conflict, and a later one never overrides it. Inside rank 2 a recorded
departure wins over the skill, since it is a deliberate decision against it. The other
sections of `CLAUDE.md` (Changes, Integration tests, Docstrings, Repository boundaries) are
process rules and detail of rank 4; none of them conflicts with a factor rule today.
This change adds no new authority for ranks 1, 2 or 4. It only names where they already are.

**D3. Three statuses, and a narrow meaning for `Limited by DBOS`.** A row is `Conforms`,
`Limited by DBOS` or `Gap`.

- `Limited by DBOS` applies only where the skill, or a departure recorded in `CLAUDE.md`,
  requires the behaviour that keeps the rule unmet. The row names the reference file.
- A choice that the skill only permits is curate.d's own. The stable executor identity
  (`runtime/identity.py`) and the executor lease (`runtime/lease.py`) are such choices: the
  skill asks only for a unique `executor_id`. They don't limit VI.2, which conforms.
- A rule that a safety guarantee keeps unmet is a `Gap` that names the guarantee. No such
  row exists today, so a fourth status is not worth its cost.
- A `Gap` row states what is unmet and on which side: application, deployment, or both.

**D4. One record, `docs/twelve-factor.md`, with this outline.**

1. `# Twelve-Factor adoption`: two or three sentences on what the page is.
2. `## The pinned text`: repository `twelve-factor/twelve-factor`, branch `next`, the SHA as
   a link, commit date, read date, "CC BY 4.0" with attribution to the manifesto's
   contributors and a note that the summaries here are paraphrases. It states what is not
   adopted (unmerged branches, the 2017 text) and how the pin moves (D1).
3. `## Precedence`: the four-entry list in the form of D7, then the meaning of each status
   from D3.
4. `## Status`: one line, "Assessed 2026-11-04 against DBOS 3.4.0 and `dbos-python` skill
   2.2.0", then twelve `### <numeral>. <factor name>` headings, each with a link to that
   factor at the pinned commit and a table of its rules.
5. `## Keeping this page true`: the design rule of D8, and that a change which closes a gap
   updates the row in the same pull request.

Every rule table has the same four columns, and one row for each rule:

```markdown
| Rule | Status | Evidence, or what is unmet | Authority or tracking |
| --- | --- | --- | --- |
| III.2 | Limited by DBOS | Config is read from the environment, except queue limits … | [`queue-management`](../.agents/skills/dbos-python/references/queue-management.md) |
```

The first cell is the bare rule identifier, so the test finds a row with one regex. The
fourth cell holds a skill reference path for a `Limited by DBOS` row, the side and any
GitHub issue for a `Gap` row, and `-` for a `Conforms` row. `skills-lock.json` stays the pin
of the skill; the "Assessed" line only says which versions the statuses were read against.
Alternative: one 33-row table (simpler to parse, but the factor names and their links then
need a second list); a `docs/standards.md` beside it (a fourth copy of the order).

**D5. The classification.** The record takes these rows. Paths are under `src/curate_d/`
unless they say otherwise; the apply step words each cell for the page and resolves the
factor links from the pinned tree.

| Rule | Status | Evidence, or what is unmet | Authority or tracking |
| --- | --- | --- | --- |
| I.1 | Conforms | One git repository holds the whole application. | - |
| I.2 | Conforms | One image serves the API and both worker profiles (`docker/Dockerfile`). | - |
| I.3 | Conforms | The local, dev and prod deploys run the same codebase. | - |
| II.1 | Gap | `uv sync --locked --no-build` and digest-pinned base images hold, but `TESSERACT_VERSION=5.5.*` floats in `docker/Dockerfile`, and the deployment manifests take a minor-version tag of the rclone image where `compose.yaml` pins the patch. | both |
| II.2 | Conforms | `pyproject.toml` and `uv.lock`, installed into `.venv`; the image and a development machine resolve the same set. | - |
| II.3 | Gap | A run outside the image needs the `tesseract` binary on `PATH` (`README.md`, "System dependencies"). | application |
| III.1 | Conforms | `Settings` (`config.py`) reads `CURATE_D_*` variables; the repository holds no credentials. Policies, templates and the taxonomy in the database are versioned application data, not deploy config. | - |
| III.2 | Limited by DBOS | Config is read from the environment, except queue limits: DBOS keeps them in its system database and changes them at runtime (`runtime/queues.py`, `never_update`). | `references/queue-management.md`, `references/queue-concurrency.md` |
| III.3 | Gap | `CURATE_D_ENVIRONMENT` switches the alerting test routes (`config.py`) and names the DBOS application (`runtime/dbos_app.py`). A fix keeps the application name stable: the skill treats it as the ownership key. | application |
| IV.1 | Conforms | PostgreSQL, S3 staging, the rclone daemon, OpenRouter and Logfire are reached over the network. | - |
| IV.2 | Conforms | Each is a distinct resource named in config; the staging backend changes from RustFS to a hosted S3 by config alone. | - |
| IV.3 | Gap | The OpenRouter base URL (`classification/remote.py`) and the pCloud authorize URL (`providers/pcloud/oauth.py`) are constants in code. | application |
| V.1 | Conforms | Build is the image (`.github/workflows/docker-publish.yml`); release is the image tag plus config, applied through GitOps; run is the container command. | - |
| V.2 | Gap | A change of config or of a secret restarts no process and gets no new release identifier. | deployment |
| V.3 | Gap | Schema migration runs at process start: the API runs `alembic upgrade head`, and the workers leave `run_migrations` at its default for DBOS and for the datasource (`runtime/dbos_app.py`, `runtime/datasource.py`). The skill supports the fix (`references/lifecycle-config.md`, "Least-Privilege Deployments"). | both; #7 |
| VI.1 | Conforms | Three process types: API, I/O worker, CPU worker. | - |
| VI.2 | Conforms | No process relies on what it cached in memory or on disk: scratch space is disposable (`staging_local_scratch_root`) and rclone remotes are rebuilt from the ledger (`placement/remotes.py`). | - |
| VII.1 | Conforms | The API binds its own port with uvicorn (`api/__main__.py`). | - |
| VII.2 | Conforms | The rclone daemon is consumed by URL, like any other service. | - |
| VIII.1 | Conforms | Work is split by process type, each started by an explicit command. | - |
| VIII.2 | Conforms | No process daemonizes or writes a PID file. | - |
| VIII.3 | Gap | `provider_requests_per_minute` feeds an in-memory pacer in each process (`providers/pacing.py`), so more I/O workers raise the request rate at the provider. DBOS queue limiters are global but count started workflows, not requests. | application |
| VIII.4 | Conforms | The platform starts, restarts and stops the processes. | - |
| IX.1 | Limited by DBOS | The workers of an application version stay until that version's workflows drain. | `references/advanced-versioning.md`; #74 |
| IX.2 | Gap | Startup time is not measured for the API or the workers, and the executor lease wait can add to it. | both |
| IX.3 | Gap | The worker handles SIGTERM and drains (`worker/main.py`, `DRAIN_SECONDS`), but the rclone sidecar gets the signal at the same moment, and the drain is shorter than the platform's grace period. | both; #74 |
| IX.4 | Conforms | Durable workflows, deterministic workflow IDs, the recovery sweep, and copy, verify, register. | - |
| X.1 | Gap | No production deploy exists yet, and `main` trails `dev`. | release process |
| X.2 | Gap | RustFS locally and in CI, a hosted S3 when deployed; no connection pooler locally or in CI; different PostgreSQL images. | both; #48 |
| XI.1 | Gap | The event stream goes to `stderr` (`observability/logging.py`); the rule names `stdout`. | application |
| XI.2 | Gap | Each process also ships its log records to Logfire. Who routes logs is a decision, and a fix touches the `telemetry-export` capability. | both |
| XII.1 | Gap | Migrations run on every API start, with no lock (`migrations/env.py`), and the workers neither wait for them nor check the ledger schema. | both; #7 |
| XII.2 | Gap | `ops/reset/reset.py` is not in the image (`.dockerignore` excludes `ops/`). The other admin commands ship in the package. | application |

Two rows came out differently from a first reading and are worth the reason. VI.2 conforms,
because nothing the skill requires makes a process keep state (D3). XI.2 is a gap and not
`Limited by DBOS`, because the skill lists the OTLP settings without requiring log export.

**D6. Public wording for the deployment side.** A gap on the deployment side is stated in
general terms: "the deployment manifests, kept outside this repository". No artifact of this
change names another repository, a hostname or an account, and none carries a Notion URL.
What the audit found about the deployment's own configuration goes to Notion only (D13).
The fix for such a gap lands with the manifests, and the row here is updated by a small
change once it does.

**D7. The order is written as one numbered list, in the same form, in three files.** Under
`## Standards` in the `context` of `openspec/config.yaml`, under `## Standards` in
`CLAUDE.md`, and under `## Precedence` in `docs/twelve-factor.md`, each entry starts a line
with its rank and its bold name:

```markdown
1. **Safety guarantees** …
2. **DBOS** …
3. **Twelve-Factor** …
4. **Coding standards** …
```

The text after the name is free and differs per file. The test reads the section, collects
the bold names of its numbered entries, and compares them with the expected order, so a
failure names the file and the order found, or that the file states none. `README.md` names
the standards and links to the record, but states no order: a fourth copy with no pin would
drift. Alternative: state the order once and link to it from the other two (an agent session
reads `CLAUDE.md` and an artifact prompt reads `config.yaml`; neither follows a link).

**D8. `openspec/config.yaml`: `## Standards` becomes the umbrella, and `rules` gains
`design`.** The section reads:

```markdown
## Standards

Three standards govern how curate.d is built, under the safety guarantees. On a conflict the
earlier entry wins; a later one never overrides it.

1. **Safety guarantees** - above the standards: the Project paragraph above, and README, "The guarantee".
2. **DBOS** - the vendored `dbos-python` skill (`.agents/skills/dbos-python`, pinned in `skills-lock.json`), as amended by the departures in `CLAUDE.md`, "DBOS".
3. **Twelve-Factor** - the manifesto at the commit pinned in `docs/twelve-factor.md`, which records the status of each of its 33 rules. Where DBOS requires otherwise, DBOS wins and the rule's row says `Limited by DBOS`, naming the skill reference.
4. **Coding standards** - "Coding & Documentation Conventions" below, and:

- **Versioning**: Semantic Versioning (SemVer: `vMAJOR.MINOR.PATCH`)
- **Git Commits**: Conventional Commits (`feat:`, `fix:`, `chore:`, `refactor:`, `docs:`)
```

The two existing bullets keep their text. `### 1` to `### 9` are not touched. Under `rules:`:

```yaml
  design:
    - "Name each Twelve-Factor rule (I.1 to XII.2) the change departs from or brings into conformance, and update that rule's row in docs/twelve-factor.md in the same change. A departure names the higher standard that requires it."
```

The context grows by about 1 KB, far below the limit. The Tech Stack section is not edited.

**D9. `CLAUDE.md`: a new `## Standards` section between the opening note and `## Changes`.**
An agent then reads the order before any rule it ranks. No heading is renamed or removed, so
every anchor in use stays valid, and `AGENTS.md` needs no edit. The section reads:

```markdown
## Standards

Three standards govern this repository, under the safety guarantees. On a conflict the
earlier entry wins; a later one never overrides it.

1. **Safety guarantees**: above the standards. `README.md`, "The guarantee", and the
   Project paragraph of `openspec/config.yaml`.
2. **DBOS**: the `dbos-python` skill, as amended by the departures under [DBOS](#dbos).
3. **Twelve-Factor**: the manifesto at the commit pinned in
   [`docs/twelve-factor.md`](docs/twelve-factor.md), which records the status of each rule.
4. **Coding standards**: `openspec/config.yaml`, "Coding & Documentation Conventions".

A design names each Twelve-Factor rule its change departs from or brings into conformance,
and the change updates that rule's row in `docs/twelve-factor.md`. A row says
`Limited by DBOS` only where the skill or a recorded departure requires the behaviour, and
it names the skill reference; a choice the skill only permits is ours, and counts as
conformance or as a gap.
```

**D10. `README.md` and `CONTRIBUTING.md`: the least that makes the standard findable.**
`README.md` gets a `## Standards` section between "Tech stack" and "System dependencies":

```markdown
## Standards

curate.d is built to three standards, all under [the guarantee](#the-guarantee): DBOS's own
guidance, the Twelve-Factor manifesto, and the coding standards in
[`openspec/config.yaml`](openspec/config.yaml). [`docs/twelve-factor.md`](docs/twelve-factor.md)
gives their precedence, the pinned manifesto text, and where curate.d conforms, is limited
by DBOS, or has a gap.
```

The layout line becomes `docs/             staging, recovery, reset and rewind guides,
Twelve-Factor adoption, diagrams`.

"Coding agents and DBOS" gets a step after the install command, since a skill update can
leave a copy where the link was (D12) and the new test then fails `make test`:

````markdown
The install can leave a copy at `.claude/skills/dbos-python` where the link was. Restore
the link before you commit; the unit suite fails on a copy:

```bash
[ -L .claude/skills/dbos-python ] || { diff -r .claude/skills/dbos-python .agents/skills/dbos-python && rm -r .claude/skills/dbos-python && git restore .claude/skills/dbos-python; }
```
````

The command does nothing when the path is still a link, and it stops before it removes
anything when the copy differs from the vendored skill. Lines 1 to 20 (title, badges, caption, Status note) are
not touched, no badge or SonarCloud link is added, and no `infrastructure/` path appears, so
`test_readme_badges.py` and `test_ops_paths.py` stay green. `CONTRIBUTING.md` gets one row in
"Rules the build enforces":

```markdown
| The standards keep one order of precedence in `openspec/config.yaml`, `CLAUDE.md` and `docs/twelve-factor.md`. Every Twelve-Factor rule has one row and one status there, a rule limited by DBOS names a reference of the vendored skill, and `.claude/skills/dbos-python` is a link to that skill, not a copy. | `tests/unit/infrastructure/test_engineering_standards.py` |
```

**D11. One test module, built from checks that return what is wrong.**
`tests/unit/infrastructure/test_engineering_standards.py` reads the files from disk, as its
neighbours do, and parses `openspec/config.yaml` with `yaml.safe_load` (PyYAML is in the dev
group). Each check is a small function over text, or over a path for the link, that returns
a list of problems. One test asserts the list is empty for the real file; further tests give
the function a bad input and assert the problem names the right thing. That covers each
failure scenario of the spec without editing a tracked file.

| Check | Passes when | A problem names |
| --- | --- | --- |
| Precedence, once for each of the three files | the section's numbered bold names are `Safety guarantees`, `DBOS`, `Twelve-Factor`, `Coding standards` | the file and the order found, or that it states none |
| Design rule | a `rules.design` entry names `Twelve-Factor` and `docs/twelve-factor.md` | the missing design rule |
| Pin | the record names `twelve-factor/twelve-factor`, the branch `next`, `CC BY 4.0`, and one 40-character SHA equal to the test's constant | what is missing, or both SHAs with the instruction to re-read the text and review the rule counts |
| Rows | the row identifiers are exactly the 33 built from the per-factor counts, each once | the missing, duplicated or unknown identifiers |
| Statuses | each row's status is one of the three | the rule and the value found |
| DBOS references | each `Limited by DBOS` row holds a `references/<name>.md` path that exists under `.agents/skills/dbos-python/` | the rule, and the missing file if one is named |
| Gap statements | no `Gap` row has an empty third cell | the rule |
| Skill link | `.claude/skills/dbos-python` is a symbolic link whose target is `../../.agents/skills/dbos-python` | the path and whether it is missing, a directory, or a link to another target (which it gives) |

The bad inputs for the link check are built under `tmp_path`, as
`test_staging_sdk_evidence.py` does for its copies. The module uses no network, imports
nothing from another test module, and never spells the name of another repository
(`tests/unit/test_isolation.py`). Docstrings follow `CLAUDE.md`: one line per test, the
imperative summary for helpers. Alternative: assert on the real files only and try each
failure once by hand, as the badges change did (the spec has too many failure scenarios here
for a hand check to stay honest).

**D12. Restore the skill link from git; edit no skill content.** At apply:

1. Compare `.claude/skills/dbos-python` with `.agents/skills/dbos-python` (`diff -r`). If
   they differ, stop and ask the owner which copy is right.
2. Remove the directory and restore the tracked link (`git restore .claude/skills/dbos-python`).
3. Confirm the path is a link and `git status` no longer lists it.

Git already tracks the link, so this step produces no diff and no commit; it is a repair of
the working tree that the new test then guards. CI always checks the link out, so the test
bites where the drift happens: on a machine where the skill installer replaced the link with
a copy. Its failure message gives the repair, and `README.md` gives the same repair as a
step of the skill update (D10). What replaced the link is not confirmed: the copy appeared
with the owner's skill update, and the install command in `README.md` is the likely cause.
The step does not depend on the cause. One follow-up for the owner sits outside the
repository: the machine-wide copy of the skill is still 2.1.0, and a session can load it
before the project's.

**D13. Notion: one "Standards" page under the curate.d project page, and Roadmap rows. No
database.** Three standards are too few to need a schema, and the gaps already have a home
in the Roadmap. The page follows the pattern of its sibling pages: an icon, an opening
callout with the repository source paths and the date, `##` headings, tables, links to
GitHub on `dev`.

- A precedence table: rank, standard, authority in the repository, status, last reviewed.
- One section for each standard: a short summary and links. The Twelve-Factor section holds
  the pin and a gap table that links each gap to its Roadmap row.
- The deployment findings that D6 keeps out of the repository.
- Links to the page from the project page's "Navigate" (under "Reference and research") and
  "Subpages".

Roadmap rows use the existing properties, with Area "Deploy & ops" and Status "Wanted":

| # | Item | Rules | Type | Horizon | Cross-links |
| --- | --- | --- | --- | --- | --- |
| 1 | Run migrations as a one-off release step and start workers on a verified schema | V.3, XII.1 | Feature | Now | #7, #74; carries the skill's least-privilege guidance |
| 2 | Make a config or secret change produce a new release | V.2 | Feature | Next | |
| 3 | Stop the rclone sidecar after the worker and size the drain to the grace period | IX.3 | Feature | Next | #74 |
| 4 | Close the parity gaps of the pooler and the PostgreSQL image locally and in CI | X.2 | Feature | Next | the pooler-check row, #48 |
| 5 | Decide who routes logs, and write the log stream to stdout | XI.1, XI.2 | Decision | Next | |
| 6 | Pin tool versions exactly | II.1, II.3 | Feature | Later | |
| 7 | Read the OpenRouter and pCloud endpoints from config | IV.3 | Feature | Next | |
| 8 | Stop branching on the environment name | III.3 | Decision | Next | |
| 9 | Decide how the provider request rate holds when I/O workers scale | VIII.3 | Decision | Next | |
| 10 | Measure startup time for the API and the workers | IX.2 | Feature | Later | |
| 11 | Ship the reset script in the image | XII.2 | Feature | Later | |
| 12 | Deploy curate.d to production | X.1 | Feature | Now | #7, #74 |

Row 12 was added after the apply, on 2026-11-05. The first version of this design gave X.1
no row, on the belief that the Roadmap already held the first production release; it did not.

One more row tracks this change itself: "Adopt the Twelve-Factor manifesto as a standard",
with Area "Specs", Type "Decision", Horizon "Now" and Status "Picked up". The owner asked for
it ahead of the apply, and it exists since 2026-11-05, so the work is visible before the
Standards page is. The stream links it to the Standards page and to the 12 gap rows, the
pull request is added to it when one opens, and it is set to "Done" after the archive.

The
owner approves the page outline and the row list before the first write, and separately the
edit of the project page, since both file a topic in a shared space for the first time. The
stream starts by reading the owner's operating guide for Notion, re-reads each target before
it edits, and re-reads each write afterwards. New Roadmap rows need no schema change.
Alternative: a Standards database with one row for each standard (a schema for three rows,
and a second place to keep the order).

**D14. No gap is fixed here.** The change edits no source, migration, image or workflow
file. Each gap is closed by its own change, whose design names the rule under the new design
rule and whose pull request sets the row to `Conforms`. This keeps the adoption reviewable as
documentation, and it lets the fixes be ordered by the Roadmap and not by this change.

## Risks / Trade-offs

- [The upstream draft moves, or its rules are renumbered] → statuses refer to the pinned
  commit only; D1 makes a pin move a deliberate change that touches the test.
- [A status goes stale after a fix that forgot the row] → the design rule is in every design
  prompt, and review returns a change that affects a rule it does not name. No test can tell
  that a status is true. Accepted.
- [A skill refresh changes guidance that a `Limited by DBOS` row leans on] → the test
  catches a removed or renamed reference file, not a reworded one. The "Assessed" line says
  which skill version the rows were read against, and the change that refreshes the skill
  re-reads the two limited rows.
- [`Limited by DBOS` becomes an excuse] → D3 restricts it to what the skill requires, and
  the test wants a reference file that exists. Two rows hold the status today.
- [The order drifts between the three files] → the test compares all three with one
  expected order.
- [The link test fails on a checkout without symbolic-link support] → the repository already
  tracks a link (`AGENTS.md`), and development and CI run on macOS and Linux. Accepted; the
  failure message says what the path is.
- [Disclosure in a public repository] → D6. The reviewer of the pull request checks the
  record's wording against it.
- [`config.yaml` grows in every artifact prompt] → about 1 KB added to 8.6 KB, of 51.2 KB.
- [The record reads as a list of faults] → it states the date of the audit and says which
  side each gap is on; 16 of 33 rules are met, and every gap has a Roadmap row.

## Migration Plan

Merge into `dev`. The next agent session and the next OpenSpec artifact read the new
`CLAUDE.md` and `config.yaml`; no process restarts and nothing is deployed. Rollback is a
revert of the merge, after which the link test and the record are gone and the Notion page
is marked as withdrawn by hand.

## Parallelism

Two work streams.

- **Stream 1, the repository.** `docs/twelve-factor.md` and
  `tests/unit/infrastructure/test_engineering_standards.py` (new), `openspec/config.yaml`,
  `CLAUDE.md`, `README.md`, `CONTRIBUTING.md`, and the repair of `.claude/skills/dbos-python`
  in the working tree. One test module pins every one of these edits, so the stream runs in
  order on one branch and is not split across agents. The link repair comes first, since it
  needs no commit and clears `git status`.
- **Stream 2, Notion.** It touches no repository file and takes its content from D5 and D13,
  so it can start at once, after the owner's approval of the outline. Its last task waits on
  the merge of Stream 1: the links to `docs/twelve-factor.md` on `dev` resolve only then.

## Open Questions

None that block the change. The two decisions the audit left open (who routes logs, how the
provider request rate holds under scale) are Roadmap rows 5 and 9, not part of this change.
