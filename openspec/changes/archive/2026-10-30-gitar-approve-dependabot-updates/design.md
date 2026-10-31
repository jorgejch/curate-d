# Design

## Context

Gitar judges `approve.md` against a pull request's metadata, commits, changed files, review results
and risk assessment; it runs nothing (see the `claude-action-tools-and-gitar-auto-approve` design).
An approval arms GitHub's auto-merge, which still waits for the `dev` ruleset: `Checks`,
`Integration tests`, code scanning, resolved threads, signed commits.

What a Dependabot version update looks like here (PR #124, `python-patches`, read 2026-10-30):

- Opened by `dependabot[bot]` from a branch in this repository (`dependabot/uv/dev/...`) into
  `dev`, labelled `dependencies`, `python:uv` and `gitar-managed`.
- One commit, authored by `dependabot[bot]`, committed by `web-flow` (GitHub's committer for
  commits it creates and signs), signature verified.
- The commit message ends in an `updated-dependencies:` block that gives, per dependency,
  `dependency-name`, `dependency-version`, `update-type` (`version-update:semver-patch`) and
  `dependency-group`.
- Within `~=` specifiers only `uv.lock` changes. `versioning-strategy: increase` rewrites a
  specifier in `pyproject.toml` only when the new version falls outside it.

A security update (PRs #81 and #92) differs: its branch has no target segment
(`dependabot/uv/uv-<hash>`), its block gives no `update-type`, it can bump indirect dependencies,
and it can cross a major (`pillow` to `13.0.0` in #81).

`.github/dependabot.yml` labels every entry `gitar-managed`, Gitar's own label for pull requests it
manages. GitHub's `labels` option reaches security updates "unless you use `target-branch` to check
for version updates on a non-default branch"; `target-branch: dev` names the default branch. Its
groups are `python-patches` (uv patches),
`image-patches` (docker patches) and `actions-patches-and-minors`; anything else opens on its own.

## Goals / Non-Goals

**Goals:**
- An eligible Dependabot update merges on the required checks alone.
- Every condition is something Gitar can read from the pull request, never a test run.

**Non-Goals:**
- GitHub Actions updates. A new action release runs in CI with the repository's secrets (Sonar,
  Claude, the image registry) as soon as it merges, so a hijacked release would run unreviewed.
  `.github/` stays human-only.
- Changing `.github/dependabot.yml` beyond its header comment (a cooldown, new groups), or
  `apply.md`.
- Any condition on the `gitar-managed` label. It keeps its Gitar-defined behaviour.

## Decisions

- **Version and security updates follow the same conditions** (owner's decision in review).
  Authorship, commits and diff content decide; nothing tells the two kinds apart, so nothing
  needs to. The first draft used the `gitar-managed` label to keep security updates out; that was
  dropped, as the label is Gitar's and must not gate approval.
- **Judge update type from the old and new versions in the diff, per dependency.** A security
  update's `updated-dependencies` block names no `update-type`, so the block can't be the source.
  The `uv.lock` `version` and the `FROM` tag are in every diff; their first three numeric parts
  give patch, minor or major. A block that does name an `update-type` must agree; a version that
  can't be compared refuses.
- **Allow `web-flow` as committer only on a commit `dependabot[bot]` authored.** That is how
  GitHub records Dependabot's own signed commits, including its rebases. A commit authored by
  anyone else, human or bot, refuses: once someone pushes to the branch, the diff is no longer
  Dependabot's alone.
- **Confine the diff to version references, and drop the size limit for this category.** A
  grouped `uv.lock` patch can pass 200 lines through wheel hashes alone; size says nothing about
  risk there. What does: no package added or removed in the lock, no `pyproject.toml` change
  beyond the named specifiers, no `Dockerfile` change beyond `FROM` tag and digest.
- **Refuse `dbos` by name.** `CLAUDE.md` requires a `dbos` bump to refresh the vendored
  `dbos-python` skill and `skills-lock.json` in the same change; Dependabot does neither.
- **`uv` minors are eligible, with the required checks as the only compatibility test.** The
  owner chose this over a range test (the minor already satisfies `pyproject.toml`), an allow-list
  of packages, or Gitar's reading of release notes. A minor opens as its own pull request, so one
  incompatible minor fails CI alone and does not hold a group back. `versioning-strategy: increase`
  rewrites the specifier when the minor leaves the declared range (`sqlalchemy~=2.1.1` →
  `~=2.2.0`); that rewrite is a version reference and is eligible. `dependabot.yml` ignores
  `aioboto3` minors until they are checked against RustFS and OVH.
- **Refuse what `dependabot.yml` ignores.** GitHub documents that `update-types` under `allow`
  does not reach security updates, and does not say whether the same holds under `ignore`. A
  security update could then bring an `aioboto3` minor. Category 4 refuses any update an
  `ignore` rule excludes from version updates, so a security update gets no path that a version
  update does not have.
- **No minor of a `0.x` package.** SemVer lets a `0.y` release break anything, so Dependabot's
  `semver-minor` for `0.142` → `0.162` names a breaking release. A minor is refused when the
  current or the new version's major is `0`. In `pyproject.toml` today that is `fastapi`,
  `uvicorn`, `httpx`, `pytesseract`, `ruff`, `pytest-split`, `openapi-spec-validator` and the
  `uv_build` backend.
  Read from the metadata's `dependency-version` and the lock's old version, both in the diff.
  Patches of a `0.x` package (`0.142.2` → `0.142.3`) stay eligible, like any other patch.
- **Docker patches stay eligible, digest-only refreshes do not.** A tag patch
  (`python:3.12.15` → `3.12.16`) is a version step the required checks build and boot; a digest
  under the same tag has no version to judge.
- **The other gates stay unchanged**: low risk, no blocking finding, no `.gitar/` change, base
  `dev`, not a fork, not a draft, no deleted, renamed or binary file.

## Risks / Trade-offs

- [A compromised patch or minor release of a Python package or base image merges without a human] → The
  required checks and the integration suite run first, and code scanning holds the merge. The
  window is still real: a Dependabot `cooldown` in `dependabot.yml` would delay adoption of a new
  release by days, and is a separate change if wanted.
- [The organization's Gitar criteria exclude dependency changes from approval] → Then nothing is
  approved: this file only narrows. The owner checks the organization criteria after merge.
- [Gitar's risk triage rates every dependency bump above low] → Then nothing is approved; the
  condition stays, since it is the organization-level safety net.
- [A `uv` patch or minor carries a behaviour change the suites do not cover] → SemVer is a
  convention, not a guarantee, and a minor adds and deprecates more than a patch. The unit and
  integration suites are the only check; a break they miss is found after the dev image deploys
  and reverted like any other merge. `0.x` minors, where SemVer itself promises nothing, wait for
  a human.

## Migration Plan

Merge into `dev`. Gitar reads `approve.md` from `dev`; with auto-approve and auto-merge on, the
next Friday's `python-patches`, `image-patches` or `uv` minor pull request is the first one judged. This pull
request itself changes `.gitar/` and so is refused, as intended. Rollback: revert the commit.

## Parallelism

Two work streams on disjoint files, both able to start at once:

- **A**: `.gitar/config/approve.md`.
- **B**: `CONTRIBUTING.md`.

No shared edit; the spec sync runs at archive.
