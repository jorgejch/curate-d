# Proposal

## Why

Gitar has withheld auto-approval on every pull request it has reviewed here, so it has never armed
auto-merge, and every Dependabot update has merged by hand. The one Dependabot pull request reviewed
under the medium-risk criteria of `gitar-approve-medium-risk-dependabot`, #141 (`astral-sh/uv`
`0.13.6` to `0.13.7`), met category 4 on every count but one. Gitar's verdict: "the 'Not ignored'
condition cannot be established because .github/dependabot.yml could not be checked, and the
criteria say uncertain conditions must not approve." Gitar's documentation lists the evaluation
input as the pull request's metadata, changed files and review results; it does not read other
repository files, and #141 doesn't change `dependabot.yml`. So the condition, as written, refuses
every Dependabot pull request.

## What Changes

- `approve.md`'s **Not ignored** condition lists the ignored updates itself (`uv` `aioboto3`
  minors; `docker` `python` minors and majors) and says to judge the condition from that list
  alone: a dependency or update type it doesn't name is not ignored, and an unread
  `dependabot.yml` refuses nothing.
- "When uncertain" adds that every fact a condition needs from a file the pull request doesn't
  change is stated in `approve.md`, so an unreadable file never withholds approval.
- Examples: #141's shape says why it isn't ignored; an `aioboto3` patch is not ignored.
- A unit test fails when `approve.md`'s list and `dependabot.yml`'s `ignore` rules differ.
- `CONTRIBUTING.md` and `dependabot.yml`'s header say the two are kept in step.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `release-pipeline`: adds a requirement that the auto-approve policy carries its own copy of the
  ignored updates, kept equal to `dependabot.yml` by the unit suite.

## Impact

- Files: `.gitar/config/approve.md`, `CONTRIBUTING.md`, `.github/dependabot.yml` (comment only),
  `tests/unit/infrastructure/test_dependabot_config.py`, `openspec/specs/release-pipeline/spec.md`.
- Behaviour: an eligible Dependabot update like #141 can now be approved, which arms auto-merge if
  Gitar's dashboard has it on. Which updates are eligible doesn't change.
- This pull request changes `.gitar/`, so Gitar refuses it and a human merges it.
