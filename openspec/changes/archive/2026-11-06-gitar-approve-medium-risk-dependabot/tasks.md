# Tasks

Two work streams on disjoint files that can start at once (design.md, Parallelism): stream A is
group 1, stream B is group 2. Group 3 runs after both.

## 1. Auto-approve criteria (stream A)

- [x] 1.1 In `.gitar/config/approve.md`, let category 4 accept low or medium risk in the "Risk above low" refusal, with high and unavailable still refusing, and limit "Identity unknown" for category 4 to the pull request's author. Verify with `grep -n 'medium' .gitar/config/approve.md` that the rule names category 4 only, and that categories 1 to 3 still read low
- [x] 1.2 In category 4, replace the **Commits** condition with "a commit by another account that the review shows refuses; commit data it does not show does not", and replace the `updated-dependencies` block in **Update types** and **Version references only** with the dependencies the diff changes, adding the refusal for a title or description that disagrees with the diff. Verify `grep -n 'updated-dependencies\|update-type' .gitar/config/approve.md` returns only `update-types`, the name of `dependabot.yml`'s own option in the **Not ignored** condition, and that each scenario in this change's `specs/release-pipeline/spec.md` maps to a rule or an example
- [x] 1.3 Update `Reporting` and `Examples`: drop the statement that `dependabot[bot]` authored every commit from a category 4 approval, state the risk rating instead, add #141's shape as an approve example at medium risk, add a high-risk refuse example, and reword examples that quote `semver-patch` or `semver-minor` metadata to use the diff's versions. Verify `grep -n 'semver-\|web-flow' .gitar/config/approve.md` returns nothing

## 2. Contributor guide and Dependabot comment (stream B)

- [x] 2.1 In `CONTRIBUTING.md`'s Gitar paragraph, state that Dependabot updates are approved at low or medium risk, and replace "since a Gitar commit would end their auto-approval" with a reason that still holds. Verify the paragraph matches `approve.md`'s categories and `ls .gitar/config/approve.md` resolves its link
- [x] 2.2 In `.github/dependabot.yml`'s header comment, reword "a Gitar commit on a Dependabot pull request ends its auto-approval" to match, comment only. Verify `git diff .github/dependabot.yml` changes only comment lines and the file still parses (`uv run python -c "import yaml; yaml.safe_load(open('.github/dependabot.yml'))"`)

## 3. Integration

- [x] 3.1 Verify `openspec validate gitar-approve-medium-risk-dependabot --strict` and `make check` pass

## Workflow follow-up

- Sync into `openspec/specs/release-pipeline/spec.md` and archive in the same pull request, with
  `openspec validate --all --strict` passing. A human merges it: Gitar refuses a `.gitar/` change.
- Owner, in Gitar's dashboard (Settings, Configuration, Behavior): check that the organization
  criteria do not also require low risk, that auto-merge is on with the merge or rebase method,
  and that "Require `.gitar/config/merge.md`" is off.
- After merge, comment `@dependabot rebase` on #141 to get a new review. Check that the verdict
  names category 4 at medium risk, and that auto-merge is armed and merges the pull request once
  the required checks pass. Do the same on #142 to #145, or wait for Friday's updates.
