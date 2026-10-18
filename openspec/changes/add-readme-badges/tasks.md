# Tasks

One stream (design D6): the README line and its test pass or fail together, so the tasks
run in order on one branch.

## 1. Badge line (`README.md`, `tests/unit/infrastructure/test_readme_badges.py`)

- [ ] 1.1 Create `tests/unit/infrastructure/test_readme_badges.py`, with one test per
  requirement in `specs/status-badges/spec.md`, parsing as design D5 describes. Run it
  against the current README and confirm that it fails, naming the missing badges and the
  missing branch-list link. Then add the line and its caption under `# curate.d` as design
  D1 lays them out, leaving the prose (the "Status:" note included) unchanged. Verify with
  `uv run pytest tests/unit/infrastructure/test_readme_badges.py --no-cov` passing, then
  break one badge's `branch=` by hand, confirm the failure names the badge, and revert.
  Commit the test and the README together.
- [ ] 1.2 Check every URL in the line by hand. Each image fetched with
  `curl -sL '<image>' | grep -oE '<title>[^<]*</title>|>[^<>]{3,40}</text>'` must render
  real values for `dev`, and the License and Status badges must show their values. Each
  link must return 200 from `curl -s -o /dev/null -w '%{http_code}' '<link>'`. Paste the
  results into the pull request description.

## 2. Integration checks

- [ ] 2.1 Run `make lint typecheck test` and verify it passes with the coverage floor met.
- [ ] 2.2 Run `openspec validate add-readme-badges --strict` and verify it passes.

## Workflow follow-up

- Sync the delta spec into `openspec/specs/status-badges/` and archive the change once the
  pull request merges into `dev`.
