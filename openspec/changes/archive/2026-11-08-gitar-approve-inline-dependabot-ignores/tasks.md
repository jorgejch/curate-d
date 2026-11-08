# Tasks

## 1. Auto-approve criteria

- [x] 1.1 In `.gitar/config/approve.md`, list the ignored updates under **Not ignored**, judge the condition from the list alone, and add the "When uncertain" sentence. Verify `grep -n 'dependabot.yml' .gitar/config/approve.md` shows no condition that needs the file read
- [x] 1.2 Add the #141 reasoning and the `aioboto3` patch to the approve examples, and reword the `aioboto3` refuse example to cite the list

## 2. Guard and docs

- [x] 2.1 Add `test_approve_criteria_list_exactly_the_ignored_updates` to `tests/unit/infrastructure/test_dependabot_config.py`. Verify it passes, and fails when a list entry is removed
- [x] 2.2 Note the copy in `CONTRIBUTING.md`'s Gitar paragraph and in `.github/dependabot.yml`'s header, comment only. Verify the file still parses

## 3. Integration

- [x] 3.1 Sync the delta into `openspec/specs/release-pipeline/spec.md` and archive in the same pull request

## Workflow follow-up

- Owner, in Gitar's dashboard (Settings, Configuration, Behavior): check that "Auto-approve PRs
  based on code review" and "Auto-merge PRs after auto-approve" are on, that the merge method is
  Merge commit or Rebase (the `dev` ruleset refuses squash), and that the organization criteria
  don't also require low risk.
- After merge, the next Dependabot pull request's verdict should read "Auto-approval: Approved"
  under category 4, with auto-merge armed.
