# Design

## Context

- `dev` is the default and deployed branch. `checks.yml` runs on every push to it
  (`coverage-report`), CodeQL runs through GitHub's default setup (no workflow file), and
  SonarCloud treats `dev` as its main branch.
- `tests/unit/infrastructure/` already pins repository configuration (`checks.yml`,
  `integration.yml`, `sonar-project.properties`) by parsing it from disk; this test joins
  it.

## Goals / Non-Goals

**Goals:**
- One line of badges, every health badge's image URL and link pinned to `dev`.
- A unit test that fails on a lost branch, a wrong project key, or a License or Status
  badge drifting from the fact it repeats.

**Non-Goals:**
- Badges for `main` or feature branches, or badges that follow the branch a viewer
  selects (see D2).
- Badges for the integration suite or the container publish. Integration runs only on
  pull requests touching its declared paths, so its last run says nothing about `dev`.
- Checking that a badge URL resolves. That would need the network, which the unit suite
  blocks; task 1.2 checks it once by hand.

## Decisions

**D1. Layout: one line of badges under the title, then the branch-list link.**

```markdown
# curate.d

[![Checks](…/checks.yml/badge.svg?branch=dev)](…/checks.yml?query=branch%3Adev) [![CodeQL](…)](…) [![Quality gate status](…)](…) [![Unit-test coverage](…)](…) [![License](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE) [![Status](https://img.shields.io/badge/status-pre--release-orange)](docs/staging.md)

Badges show `dev`; [other branches on SonarCloud](https://sonarcloud.io/project/branches_list?id=jorgejch_curate-d).

Autonomous multi-cloud file and media organizer.
```

The caption says which branch the health badges show, since a badge has no visible
label for it. Health badge URLs (`B` = `dev`, `K` = `jorgejch_curate-d`):

| Badge | Image | Link |
| --- | --- | --- |
| Checks | `github.com/jorgejch/curate-d/actions/workflows/checks.yml/badge.svg?branch=B` | `…/actions/workflows/checks.yml?query=branch%3AB` |
| CodeQL | `…/actions/workflows/github-code-scanning/codeql/badge.svg?branch=B` | `…/actions/workflows/github-code-scanning/codeql?query=branch%3AB` |
| Quality gate | `sonarcloud.io/api/project_badges/measure?project=K&metric=alert_status&branch=B` | `sonarcloud.io/summary/new_code?id=K&branch=B` |
| Coverage | same, `metric=coverage` | same |

The alt texts are "Quality gate status" and "Unit-test coverage", as asked; "Unit-test"
is accurate because integration runs upload no coverage. The Sonar URLs name
`branch=dev` explicitly even though Sonar defaults to it, so the badges stay right if
Sonar's main branch ever changes.

**D2. No branch-following badges.** GitHub fetches README images through its camo proxy,
which passes on no hint of the viewed branch, and a per-branch URL would be merged into
`dev` along with the branch. Committing badge SVGs per branch (relative image paths do
follow the viewed branch) needs bot pushes to `dev`, which the ruleset forbids, and would
conflict on every merge. The SonarCloud branch-list link covers other branches, and Sonar
reports each pull request on the pull request itself.

**D3. A static License badge, pinned to `pyproject.toml`.** shields.io's
`github/license` badge reads GitHub's licence detection, but it depends on shields'
GitHub token pool. On 2026-10-18 it returned "Unable to select next GitHub token from
pool". A static `license-Apache--2.0-blue` badge never fails, and the test keeps it equal
to `project.license`.

**D4. Status is a static badge that the test ties to the "Status:" note.** There is no
release to read a status from (no GitHub release, version 0.1.0). The note is the
README's own statement of status, so making the badge agree with it means one edit at
release time can't silently leave the other stale.

**D5. The test parses Markdown with a regex and URLs with `urllib.parse`.** The badge
pattern is `[![alt](image)](link)`; badges are identified by alt text. The branch comes
from the `branch` query parameter (images, Sonar links) or from `query=branch:<B>`
(Actions links). Shields' static path decodes `--` to `-` and `_` to a space.
`sonar.projectKey` is read from the properties file by a one-line parse rather than
imported from `test_sonar_properties.py`, so neither test module depends on the other.
Every assertion message names the badge and the value found.

**D6. One work stream.** The README line and its test fail or pass together, so this
change can't be split across agents. Tasks run in order on one branch.

## Risks / Trade-offs

- [GitHub or SonarCloud change a badge URL scheme] → the badge renders as an error and
  the test still passes; this is caught by eye, not by CI. Accepted: a network check in CI
  would be flakier than the drift it guards against.
