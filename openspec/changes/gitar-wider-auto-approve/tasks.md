# Tasks

Streams 1 and 2 touch disjoint files and can start at once, on separate agents. Stream 3 waits
for both.

## 1. Auto-approve criteria (`.gitar/config/approve.md`, `CONTRIBUTING.md`)

- [ ] 1.1 In `.gitar/config/approve.md`: medium risk and the 20-file, 500-line cap for categories 1 to 3; a new category 5 for `.editorconfig` and `.gitignore` that combines with 1 to 3 (category 4 keeps its number); the `web-flow` committer rule; the lock-edge rule and the `github-actions` rule under the Dependabot category; examples for #164's and #165's shapes. Verify every category reference still names the right category (`grep -n 'ategor' .gitar/config/approve.md`)
- [ ] 1.2 Update `CONTRIBUTING.md`'s Gitar paragraph to the new scope

## 2. Dependabot cooldown (`.github/dependabot.yml`, `tests/unit/infrastructure/test_dependabot_config.py`)

- [ ] 2.1 Add `cooldown: {default-days: 7}` to the `github-actions` entry, with a comment saying why. Verify the file parses
- [ ] 2.2 Add a unit test that the `github-actions` entry's cooldown is at least seven days and that no other entry sets one. Verify it passes, and fails when the cooldown is removed

## 3. Integration (after 1 and 2)

- [ ] 3.1 Run `make check` and `openspec validate --all --strict`
- [ ] 3.2 Sync the delta into `openspec/specs/release-pipeline/spec.md` and archive in the same pull request

Apply streams 1 and 2 in parallel where more than one agent is available.

## Workflow follow-up

- After merge, the next Dependabot run's verdicts should read "Auto-approval: Approved" for an
  action patch or minor and for a `python-patches` group whose lock gains an edge.
