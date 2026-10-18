# Proposal

## Why

The README states the project's status in prose only. A visitor can't see whether `dev`
(the default, deployed branch) is green, how it fares against the SonarCloud quality
gate, or how much of it the unit tests cover, without going to Actions or SonarCloud.
Every source those badges need is already public: Checks, CodeQL (default setup), and
SonarCloud's branch analyses on the OSS offering.

## What Changes

- Add one line of badges at the top of `README.md`, under the title:
  - Checks, CodeQL, the SonarCloud quality gate and unit-test coverage, every image and
    link pinned to `dev`;
  - a License badge and a Status badge, which don't vary by branch;
  - a link to SonarCloud's branch list for other branches. A README can't follow the
    branch a viewer selects on GitHub: badge URLs are fixed text, and GitHub's image proxy
    doesn't pass the branch on.
- A unit test pins the line: the badges, their branch, the SonarCloud project key, and
  agreement between the License and Status badges and the facts they repeat.

## Capabilities

### New Capabilities

- `status-badges`: the README's status badges: which signals they show, for which
  branch, where they link, and how they stay consistent with the repository's own
  metadata.

### Modified Capabilities

None.

## Impact

- `README.md`: a new line under the title. The prose, including the "Status:
  pre-release" note, is unchanged.
- `tests/unit/infrastructure/test_readme_badges.py`: new.
- No workflow, Sonar setting, application code or dependency changes. A README-only diff
  still skips the integration suite (`release-pipeline`), and this one changes no path
  that suite declares.
