# Tasks

The three groups touch disjoint files and can start at once. Group 4 waits on all three.

## 1. Dependabot

- [x] 1.1 In `.github/dependabot.yml`, remove `labels: [gitar-managed]` from the `uv`, `github-actions` and `docker` entries, and replace the header comment's label sentence with why no entry sets labels (the label turns on Gitar's auto-apply; a Gitar commit ends category 4 eligibility). Verify `grep -c gitar-managed .github/dependabot.yml` finds only the comment, and `uv run pytest tests/unit/infrastructure/test_dependabot_config.py` passes

## 2. Owner pull request labelling

- [x] 2.1 Add `.github/workflows/label-owner-prs.yml`: on `pull_request` `opened`; top-level `permissions: {}`; one job with `pull-requests: write`, run only when `github.event.pull_request.user.login == github.repository_owner` and the head repository is `github.repository`; one step that adds `gitar-managed` through `gh api repos/$REPO/issues/$NUMBER/labels` with `GH_TOKEN: ${{ github.token }}` and values passed through `env`, never interpolated into the script; no checkout and no third-party action. Verify `actionlint .github/workflows/label-owner-prs.yml` reports nothing

## 3. Contributor documentation

- [x] 3.1 In `CONTRIBUTING.md`'s Gitar paragraph, replace "auto-apply is enabled per pull request" with which pull requests open with `gitar-managed` (the owner's, by the labelling workflow; never Dependabot's) and that `gitar auto-apply:off` stops it on one pull request. Verify the paragraph names `label-owner-prs.yml` and `ls .github/workflows/label-owner-prs.yml` resolves
- [x] 3.2 Add the rule "Pull before you push, and check `git log` for Gitar commits before you force-push" (owner's rule, review follow-up) to `CONTRIBUTING.md`'s Branches, commits and pull requests section and the steward skill's Commits section: Gitar is a second writer on a `gitar-managed` branch, and `--force-with-lease` does not protect a commit a fetch already brought in. Verify `grep -c "Pull before you push" CONTRIBUTING.md .claude/skills/steward/SKILL.md` finds both

## 4. Integration

- [x] 4.1 Verify `make lint`, `make typecheck`, `make test` and `openspec validate gitar-managed-owner-prs --strict` pass

## Workflow follow-up

- Open this change's pull request against `dev`. The workflow runs from the pull request's own branch, so check that the run labels this pull request `gitar-managed`, and that Gitar's summary then shows auto-apply on.
- After the merge, check that the next Dependabot pull request carries `dependencies` and its ecosystem label and not `gitar-managed`.
- Sync the delta into `openspec/specs/release-pipeline/spec.md` and archive the change.
