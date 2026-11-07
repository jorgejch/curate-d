# Proposal

## Why

curate.d has two sets of engineering rules, the DBOS guidance in the vendored `dbos-python`
skill and the coding conventions in `openspec/config.yaml`, but nothing that governs the
contract between the application and the platform it runs on: config, releases, process
lifecycle, parity between environments, logs and admin tasks. Those are where a first
production deploy goes wrong, and an audit against the Twelve-Factor manifesto found 15 of
its 33 rules unmet. There is also no stated order between the rule sets, so a conflict
between them is settled case by case.

## What Changes

- Adopt the Twelve-Factor manifesto as a guiding principle, read from the `next` branch of
  `twelve-factor/twelve-factor` and pinned to a commit, since that text is a living draft.
- Name three standards under one **Standards** umbrella and state their precedence. A lower
  standard never overrides a higher one:
  1. the safety guarantees (above the umbrella; README, "The guarantee")
  2. **DBOS**: the vendored `dbos-python` skill, plus the departures recorded in `CLAUDE.md`
  3. **Twelve-Factor**
  4. **Coding standards**: the conventions in `openspec/config.yaml`
- Add `docs/twelve-factor.md`: the adoption record. It names the pinned text and its
  licence, the precedence, and the status of each of the 33 rules: conforms, limited by
  DBOS (with the skill reference that limits it), or gap.
- `openspec/config.yaml`: make `## Standards` the umbrella, with the precedence and the
  three standards, and add a design rule so that a change names the factor rules it affects.
  The numbering of the coding conventions does not change.
- `CLAUDE.md` (`AGENTS.md` links to it): add a `## Standards` section. No existing heading
  is renamed.
- `README.md`: a brief Standards note, the `docs/` line of the repository layout, and a
  step in the skill update that restores the link when the install leaves a copy.
- `CONTRIBUTING.md`: one row in "Rules the build enforces" for the new test.
- Restore `.claude/skills/dbos-python` as the symlink to `.agents/skills/dbos-python` that
  git tracks. The working tree holds a copy in its place, which lets two copies of the DBOS
  authority drift apart.
- Add a unit test that pins the above: the three files agree on the precedence, the pinned
  commit is present, all 33 rules have exactly one status, every "limited by DBOS" row names
  a reference file that exists in the vendored skill, and the skill path is a symlink.
- Outside the repository: a Standards page under the curate.d project in Notion, Roadmap
  items for the gaps, and one Roadmap item that tracks this change.

Not in this change: the fix for any gap. Each gap is closed by its own later change. No
source, migration, image or workflow file changes here.

## Capabilities

### New Capabilities
- `engineering-standards`: which standards govern curate.d's development, their
  precedence, where each one's authority lives, and how conformance to the Twelve-Factor
  rules and departures from them are recorded and kept consistent.

### Modified Capabilities

None.

## Impact

- **Files**: `docs/twelve-factor.md` (new), `openspec/config.yaml`, `CLAUDE.md`,
  `README.md`, `CONTRIBUTING.md`, `.claude/skills/dbos-python` (symlink restored),
  `tests/unit/infrastructure/test_engineering_standards.py` (new),
  `openspec/specs/engineering-standards/spec.md` (on sync).
- **Runtime**: none. No behaviour of the API, the workers or the pipeline changes.
- **Agents and contributors**: `openspec/config.yaml` is injected into every OpenSpec
  artifact prompt and `CLAUDE.md` into every agent session, so the precedence and the design
  rule take effect for the next change. A design that departs from a factor rule must say so.
- **Existing references**: `CLAUDE.md#dbos`, `#docstrings` and `#changes`, the README
  anchors, and "openspec/config.yaml, §8" in `sonar-project.properties` stay valid.
  `tests/unit/infrastructure/test_readme_badges.py` and `test_ops_paths.py` constrain the
  README edit.
- **CI**: the change touches no path the integration filter declares, so the integration
  suite is skipped; the checks workflow runs the new unit test. The auto-approve policy
  excludes `CLAUDE.md` and `openspec/`, so the pull request needs a human review.
- **Notion**: one new page and new Roadmap rows, with the existing properties. No new
  database or schema.
