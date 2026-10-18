# Design

## Context

See proposal.md, Why. Current state that shapes the approach:

- `docker/Dockerfile:16-17` set `ARG PYTHON_VERSION=3.12` and `ARG UV_VERSION=0.12.19`;
  lines 20, 26 and 53 interpolate them into `FROM`. Dependabot's docker parser matches
  `FROM [--platform] [registry/]image[:tag][@sha256:digest] [AS name]` with an image
  character class that excludes `$` and `{`; `ARG` is never evaluated
  (dependabot-core `shared_file_parser.rb`, issue #2057 still open). Those three lines are
  invisible to it today.
- Nothing passes `PYTHON_VERSION` or `UV_VERSION` at build time. The only build arg in the
  repository is `CURATE_D_BUILD_REVISION` (`docker-publish.yml:112-113`, `Dockerfile:76`).
- `pyproject.toml:64-68` pins `aioboto3~=15.5.0` with a comment requiring re-verification
  of a new minor against RustFS and OVH; `infrastructure/staging-compatibility.md:30,36`
  records aioboto3 15.5.0 and Python 3.12.14 as the verified pair.
- Dev dependencies live in PEP 735 `[dependency-groups]` (`pyproject.toml:80-99`), which
  Dependabot's uv parser reads; there is no `[tool.uv]` table and no workspace.
- The pull-request ruleset requires a head up to date with `dev`; `pyproject.toml`,
  `uv.lock` and `docker/Dockerfile` are declared paths, so every Dependabot pull request
  that touches them runs the whole integration suite, again after every rebase.
- `checks.yml:87-99` already runs the SonarQube scan on a Dependabot run with the
  Dependabot secret `SONAR_TOKEN` (release-pipeline, "The SonarQube scan skips only fork
  pull requests"). No workflow change is needed.
- Existing pin tests (`tests/unit/infrastructure/test_checks_workflow.py`,
  `test_integration_workflow.py`, `test_release_pipeline.py`) parse the workflow YAML with
  PyYAML (a dev dependency), use module-level `_helper` functions, plain `assert`s with the
  offending name as the message, and one-line behaviour docstrings.
  `test_release_pipeline.py:197-202` splits the Dockerfile on the literal `AS runtime`.

Verified against the Dependabot options reference and dependabot-core on 2026-10-18;
citations are in the per-decision notes below.

## Goals / Non-Goals

**Goals:**
- A `dependabot.yml` whose every observable property in the spec delta is pinned by a
  unit test, so a later edit that opens pull requests against `main`, groups a major, or
  lets the python image move minor fails the build.
- A Dockerfile Dependabot can update, with no behavioural change to the built image beyond
  pinning the exact digests it already resolves.

**Non-Goals:**
- Shaping security updates. With `target-branch` set, GitHub applies none of the block's
  options (ignore, groups, labels) to security updates, which always target the default
  branch (`main`). Accepted: they stay as GitHub raises them; forward-porting to `dev`
  is a manual step, as today.
- Compose images (`compose.yaml:34,80,107,187`): the Postgres image is
  `${CURATE_D_POSTGRES_IMAGE:-...}`, the RustFS digests are evidence recorded in
  `infrastructure/staging-compatibility.md:22-23`, and production versions live in Flux.
- Changing any pin or range in `pyproject.toml`, the `uv_build` build-backend bound, or
  the apt pin `TESSERACT_VERSION`.
- `cooldown`. GitHub already applies a 3-day default cooldown to version updates; nothing
  here lengthens it.

## Decisions

### D1. One `dependabot.yml`, three ecosystems, `target-branch: dev` on each

```yaml
version: 2
updates:
  - package-ecosystem: uv
    directory: /
    target-branch: dev
    schedule: {interval: weekly, day: friday, time: "16:00", timezone: America/Los_Angeles}
    versioning-strategy: increase
    groups:
      python-patches:
        applies-to: version-updates
        patterns: ["*"]
        update-types: [patch]
    ignore:
      - dependency-name: aioboto3
        update-types: ["version-update:semver-minor"]
  - package-ecosystem: github-actions
    directory: /
    target-branch: dev
    schedule: {interval: weekly, day: friday, time: "16:00", timezone: America/Los_Angeles}
    groups:
      actions-patches-and-minors:
        applies-to: version-updates
        patterns: ["*"]
        update-types: [patch, minor]
  - package-ecosystem: docker
    directory: /docker
    target-branch: dev
    schedule: {interval: weekly, day: friday, time: "16:00", timezone: America/Los_Angeles}
    groups:
      image-patches:
        applies-to: version-updates
        patterns: ["*"]
        update-types: [patch]
    ignore:
      - dependency-name: python
        update-types: ["version-update:semver-minor", "version-update:semver-major"]
```

(Block scalars and comments are the apply's choice; the shape is what the tests pin.)

- `target-branch: dev`: without it every pull request opens against the default branch
  `main` ("All pull requests for version updates are opened targeting the specified
  branch", options reference). Side effect, accepted under Non-Goals: the block's options
  "no longer apply to security updates".
- `uv` with `versioning-strategy: increase` is documented ("Supported by: bundler, cargo,
  composer, helm, mix, npm, pip, pub, and uv"). Open bug dependabot-core #16112 reports
  `increase` widening a range instead of raising it on some manifests; the first uv pull
  request is the check (Risks).
- `github-actions` at `/` covers `.github/workflows/`. SHA-pinned steps with a
  `# vX.Y.Z` comment are classified by the tag the SHA carries upstream, so they group by
  update type; tag-pinned steps (`actions/checkout@v7`, the docker actions in
  `docker-publish.yml`) only ever see a major bump. Both are already tolerated by the
  workflow pin tests (`test_checks_workflow.py:108` matches `actions/checkout@` as a
  prefix; `test_integration_workflow.py:356` requires a 40-hex SHA, which a Dependabot
  bump keeps).
- Schedule and limit as confirmed: weekly, Friday 16:00 America/Los_Angeles, default
  `open-pull-requests-limit` (5 per block; majors each take a slot).
- Alternative rejected: one `docker-compose` block as well. See Non-Goals.

### D2. Grouping: patches grouped, minors and majors individual; digests fall outside

The rebase cost under the up-to-date-with-base rule is per pull request: `n` open
Dependabot pull requests touching declared paths cost `n(n+1)/2` integration runs to
merge serially. Patches are low-risk and numerous, so they share one pull request; minors
and majors can each break CI in their own way and are kept apart so one failure does not
block the rest. `github-actions` minors join the patch group because action minors carry
no API change for this repository's usage and the workflow pin tests catch a shape change.

Digest-only image refreshes (same tag, new `sha256`) are not grouped. Dependabot's
`SemverGroupingRules#allow_grouping?` compares the current tag with the latest tag name;
on a digest refresh they are equal, no semver level matches, and the dependency is handed
back for an individual pull request. A group without `update-types` would catch digests
but would also take the `uv` image's minors and majors, which the spec forbids grouping.
Confirmed with the user: the `image-patches` group keeps `update-types: [patch]` and a
digest refresh opens on its own, at most one per image. dependabot-core PR #15103 (merged
2026-05) adds a server-side experiment that suppresses digest-only refreshes for
versioned tags; whether GitHub has enabled it is not documented, so the spec states the
behaviour either way as "opens individually" and the test pins only the group.

### D3. Two ignores, both by `update-types`, never by `versions`

- `aioboto3`: `version-update:semver-minor` only. `ignore.update-types` builds ranges
  relative to the current version, so patches (`15.5.x`) still flow and reach the patch
  group; `increase` rewrites `~=15.5.0` to `~=15.5.<n>`. A `versions:` range would need
  editing on every adopted minor. `types-aioboto3[s3]~=15.5` is left alone: it is a dev
  typing package and the pin comment does not cover it.
- `python` (docker): `semver-minor` and `semver-major`. `3.12.14 -> 3.12.15` and a digest
  refresh pass; `3.13.x` does not. Docker `dependency-name` is the repository name as
  written in `FROM`, so `python`, not `library/python`.
- On a security update these ignores do not apply (security jobs apply only explicit
  `versions` ranges, and the block's options are off under `target-branch` anyway). That is
  the wanted behaviour: a security fix in an aioboto3 minor is not held back.

### D4. Dockerfile `FROM` lines become literal tag + digest; the two ARGs go

```dockerfile
FROM ghcr.io/astral-sh/uv:0.12.19@sha256:<digest> AS uv
FROM python:3.12.<patch>-slim-trixie@sha256:<digest> AS builder
FROM python:3.12.<patch>-slim-trixie@sha256:<digest> AS runtime
```

- Literal because of the parser (Context). Digest-pinned because the user confirmed it for
  the `uv` image too, and because Dependabot then rewrites tag and digest together
  (`updated_requirements` resolves the new tag's digest); with a tag only, builds would
  float within the tag between runs.
- The Python patch is `3.12.14` (`staging-compatibility.md:36`) unless a newer 3.12 patch
  is current on Docker Hub at apply time; the apply records which in the pull request.
  Moving the patch here does not change the staging-compatibility evidence: that document
  records the pair the OVH rows were earned on, and #48 re-earns them.
- Digests are resolved at apply time from the registry (`docker buildx imagetools inspect
  <image>:<tag>` or `docker manifest inspect`), using the manifest-list digest, which is
  what Docker Hub and GHCR show and what Dependabot writes. Never typed from memory.
- `ARG TESSERACT_VERSION` stays; `AS uv`, `AS builder`, `AS runtime` stay (`COPY --from=uv`
  and `test_release_pipeline.py:197-202` depend on them). The build-arg declarations at
  lines 16-17 go; no caller exists.
- The `uv` binary and the `uv_build` backend (`pyproject.toml:101-103`,
  `>=0.12.19,<0.13.0`) are independent: a `uv` image minor from Dependabot does not need
  a `uv_build` bump to build. The local `uv 0.13.1` already builds the project with that
  backend bound.

### D5. Two new pin tests, one per stream, in `tests/unit/infrastructure/`

- `test_dependabot_config.py`: parses `.github/dependabot.yml` with PyYAML; asserts the
  three ecosystems and no `docker-compose`, `target-branch: dev` on each, the weekly
  Friday America/Los_Angeles schedule on each, `versioning-strategy: increase` on `uv`,
  each group's `applies-to`/`update-types` exactly, the `aioboto3` ignore exactly
  `["version-update:semver-minor"]`, the `python` ignore exactly minor and major, and that
  no ignore entry names `semver-patch`. Failure messages carry the entry's ecosystem,
  group or dependency name.
- `test_dockerfile_pins.py`: reads `docker/Dockerfile` as text; for every line starting
  with `FROM`, strips an optional `--platform=...`, matches
  `^FROM\s+(?P<ref>\S+)(\s+AS\s+\w+)?$` and asserts
  `ref` fullmatches `[a-z0-9./-]+:[\w][\w.-]*@sha256:[0-9a-f]{64}` and contains no `$`;
  asserts no `ARG PYTHON_VERSION` or `ARG UV_VERSION` line remains. Failure message is the
  offending line.
- Separate files so Streams A and B touch disjoint files. Same conventions as
  `test_checks_workflow.py`: `REPOSITORY_ROOT = Path(__file__).resolve().parents[3]`,
  `_helper()` loaders, `cast` for mypy strict, one-line behaviour docstrings.

### D6. Parallel work streams

Stream 0 is this spec delta, already written. Stream A (`.github/dependabot.yml`,
`test_dependabot_config.py`) and Stream B (`docker/Dockerfile`, `test_dockerfile_pins.py`)
share no file and run at once. The final group runs `make check`, `make image` and
`openspec validate --strict`; CI's integration job runs the suite and boots the image
because `docker/Dockerfile` is a declared path. No local integration test builds the image
(`tests/integration` has no docker build), so `make image` is the local build check.

## Risks / Trade-offs

- [uv `increase` widens instead of raising (#16112)] → First uv pull request is inspected
  for the rewritten range shape before merge; if it widens, switch to
  `versioning-strategy: lockfile-only` for the lock and bump ranges by hand, in a follow-up
  change (spec says `increase`, so that is a spec change).
- [Digest-only refreshes open individually, up to two pull requests a week] → Accepted
  (D2). If GitHub's suppression experiment is on, they do not open at all; either is
  within spec.
- [`uv` image minor PRs (0.13, 0.14 ...) arrive as individual pull requests] → Wanted:
  each is reviewed against the `uv_build` bound and merged when green.
- [Dependabot pull requests cost a full integration run each, again on every rebase] →
  Grouping (D2) bounds the count; the 5-per-block limit caps the queue.
- [Security updates target `main`, not `dev`] → Known and accepted (Non-Goals); a
  security pull request is cherry-picked or re-raised against `dev` by hand.
- [Resolved digest goes stale between apply and merge] → Harmless: Dependabot's first run
  proposes the refresh. The apply still resolves fresh digests rather than copying from a
  cached build.
- [A tag-pinned action's SHA is not tagged upstream] → Falls outside groups and semver
  ignores and opens individually; no such pin exists today (all SHA pins carry a `# vX`
  comment matching a release tag).

## Open Questions

None that change the specs or the tasks. The Python patch (3.12.14 or newer) is decided at
apply time against the registry and stated in the pull request.
