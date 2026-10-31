# Tasks

Two work streams on disjoint files that can start at once (design.md, Parallelism): stream A is
group 1, stream B is group 2. Group 3 runs after both.

## 1. Auto-approve criteria (stream A)

- [x] 1.1 In `.gitar/config/approve.md`: narrow the "Written by a bot" and "Carries a bot's commit" refusals to "except an eligible Dependabot version update"; exempt that category from the "Too large" limit; add eligible category 4, **Dependabot version updates** (opened by `dependabot[bot]` from this repository; every commit authored by `dependabot[bot]` and committed by it or `web-flow`; the `gitar-managed` label; every `updated-dependencies` entry a `uv` `version-update:semver-patch`, a `uv` `version-update:semver-minor` whose old and new major are not `0`, or a `docker` `version-update:semver-patch`; no `dbos`; diff confined to `uv.lock`, the named specifiers in `pyproject.toml`, and `FROM` tag and digest in `docker/Dockerfile`; no package added to or removed from `uv.lock`); keep `.github/` and every `github-actions` update human-only; update Reporting and Examples (approve a `python-patches` group and a single `uv` minor that rewrites its `pyproject.toml` specifier; refuse an actions patch, a `uv` major, a `fastapi` `0.142` → `0.162` minor, a `docker` minor, a digest-only refresh, a security update, a `dbos` patch, a human commit on a Dependabot pull request; state that for a `uv` minor the required checks are the only compatibility test). Verify each spec scenario in `specs/release-pipeline/spec.md` maps to a rule or example in the file, and every backticked repository path it names exists (`ls -d` from the repository root)

## 2. Contributor guide (stream B)

- [x] 2.1 In `CONTRIBUTING.md`'s Gitar paragraph, add Dependabot `uv` patch updates, `uv` minor updates of packages at `1.0` or above, and `docker` patch updates to what `approve.md` may approve, and change "never a pull request a bot opened or committed to" to except them; verify the paragraph matches `approve.md`'s categories and the link resolves (`ls .gitar/config/approve.md`)

## 3. Integration

- [x] 3.1 Verify `make check` passes and `openspec validate gitar-approve-dependabot-updates --strict` passes

## Workflow follow-up

- Sync into `openspec/specs/release-pipeline/spec.md` and archive in the same pull request, with
  `openspec validate --all --strict` passing.
- After merge, with Gitar's auto-approve and auto-merge on: confirm the organization criteria do
  not exclude dependency changes, then watch the next Friday's `python-patches` pull request and
  check that Gitar's report names category 4, and that a `uv` minor pull request is approved while
  a `uv` major is refused with its update type named.
