# Tasks

Work streams (design, "Parallelism"): **Stream 1** (groups 1 to 4 and 6) is the repository
work. One test module pins every edit, so its tasks run in order on one branch and are not
split across agents. **Stream 2** (group 5) is the Notion work. It touches no repository file
and takes its content from design D5 and D13, so it can start at once, beside group 1. Group
6 waits on groups 1 to 4. The follow-up at the end waits on the merge.

No task edits a file under `src/`, `migrations/`, `docker/` or `.github/` (design D14).

## 1. Working tree (`.venv`, `.claude/skills/dbos-python`; no commit)

- [x] 1.1 Run `make install` so the environment matches `uv.lock`. Verify `uv run python -c "import importlib.metadata as m; print(m.version('dbos'))"` prints `3.4.0`
- [x] 1.2 Restore the skill link (design D12). Run `diff -r .claude/skills/dbos-python .agents/skills/dbos-python`; if it reports any difference, stop and ask the owner which copy is right. Otherwise remove the directory `.claude/skills/dbos-python` and run `git restore .claude/skills/dbos-python`. Verify `readlink .claude/skills/dbos-python` prints `../../.agents/skills/dbos-python`, `git status --short .claude .agents skills-lock.json` prints nothing, and `git diff --stat HEAD -- .agents/skills/dbos-python` is empty (the skill's contents are not edited)

## 2. Adoption record and its checks (`tests/unit/infrastructure/test_engineering_standards.py`, `docs/twelve-factor.md`)

- [x] 2.1 Add `tests/unit/infrastructure/test_engineering_standards.py` with the eight checks of design D11, each a function that returns a list of problems: precedence of a section, design rule, pin (the SHA of design D1 as a constant), rows (built from the per-factor rule counts), statuses, DBOS references, gap statements, skill link. Add one test on bad input for every failure scenario of the spec's test-pinned requirements (a file with another order, a file with no order; a directory, a missing path, a link to another target, built under `tmp_path`; no 40-character SHA, another SHA, a missing repository, branch or licence; a missing, duplicated or unknown rule, an unknown status; a `Limited by DBOS` row with no reference, and with a reference file that does not exist; a `Gap` row with an empty statement; no design rule), each asserting that the problem names what the scenario says. Add the test that the real `.claude/skills/dbos-python` passes the link check. Docstrings follow `CLAUDE.md`; the module spells no other repository's name. Verify with `uv run pytest tests/unit/infrastructure/test_engineering_standards.py tests/unit/test_isolation.py --no-cov` passing and `make lint typecheck` clean
- [x] 2.2 Add the tests that the real `docs/twelve-factor.md` passes the pin, rows, statuses, DBOS references, gap statements and precedence checks; run them and see them fail, naming the missing file. Then write `docs/twelve-factor.md` with the outline and table format of design D4, the 33 rows of design D5 and the precedence list of design D7, in the public wording of design D6. Take each factor's link from the pinned tree (`content/<factor>.md` at the pinned commit; list it with `gh api "repos/twelve-factor/twelve-factor/git/trees/3ad5a5f36312cc8ad876abae1bd691acd790d4d1?recursive=1" --jq '.tree[].path'`). Verify with `uv run pytest tests/unit/infrastructure/test_engineering_standards.py --no-cov` passing; every repository path the record cites resolves with `ls`; `grep -ci notion docs/twelve-factor.md` prints `0`; and `grep -oE 'https?://[^) >]+' docs/twelve-factor.md | sort -u` lists only the manifesto repository at the pinned SHA, the licence, and this repository's issues

## 3. Precedence where agents read it (`openspec/config.yaml`, `CLAUDE.md`)

- [x] 3.1 Add the tests that the real `openspec/config.yaml` passes the precedence and design-rule checks; see them fail. Then rewrite `## Standards` in its `context` and add `rules.design`, with the text of design D8. Keep the Versioning and Git Commits bullets, and do not touch `### 1` to `### 9` or the Tech Stack section. Verify with `uv run pytest tests/unit/infrastructure/test_engineering_standards.py tests/unit/infrastructure/test_sonar_properties.py --no-cov` passing; `grep -cE '^  ### [1-9]\. ' openspec/config.yaml` prints `9`; `uv run python -c "import yaml; print(len(yaml.safe_load(open('openspec/config.yaml'))['context'].encode()))"` prints a number below `51200`; and `openspec instructions design --change adopt-twelve-factor-standard --json` shows the new rule under `rules`
- [x] 3.2 Add the test that the real `CLAUDE.md` passes the precedence check; see it fail. Then insert the `## Standards` section of design D9 between the opening note and `## Changes`. Rename and remove no heading. Verify with `uv run pytest tests/unit/infrastructure/test_engineering_standards.py --no-cov` passing; `grep -n '^## ' CLAUDE.md` lists Standards, Changes, Integration tests, Docstrings, Repository boundaries, DBOS, in that order; and `readlink AGENTS.md` prints `CLAUDE.md`

## 4. Pointers for people (`README.md`, `CONTRIBUTING.md`)

- [x] 4.1 In `README.md`, add the `## Standards` section of design D10 between "Tech stack" and "System dependencies", change the `docs/` line of "Repository layout" to the one D10 gives, and add the "restore the link" step of D10 after the install command in "Coding agents and DBOS". Run that step's command as written: it prints nothing, and `readlink .claude/skills/dbos-python` still prints `../../.agents/skills/dbos-python`. Verify with `uv run pytest tests/unit/infrastructure/test_readme_badges.py tests/unit/infrastructure/test_ops_paths.py --no-cov` passing; `git diff -U0 README.md` shows no hunk in lines 1 to 20; and `ls docs/twelve-factor.md openspec/config.yaml` resolves both link targets
- [x] 4.2 In `CONTRIBUTING.md`, add the row of design D10 to the "Rules the build enforces" table. Verify `grep -c 'test_engineering_standards.py' CONTRIBUTING.md` prints `1`, the row sits inside that table, and `uv run pytest tests/unit/infrastructure/test_ops_paths.py --no-cov` passes

## 5. Notion (Stream 2; no repository file)

Follow the owner's Notion rules throughout: read the operating guide and the enabled Skills
before the first write, read Notion's guide to databases before adding structure, re-read
each target before an edit, re-read each write after it, and report only what was done. No
Notion link goes into a repository file (design D6).

- [x] 5.1 Show the owner the page outline and the 11 Roadmap rows of design D13, and ask for approval of three things: the new "Standards" page under the curate.d project page, the rows, and the links to the page from the project page's "Navigate" and "Subpages". Verify the owner's answer is in the session before any write; with no approval, stop the stream and show the unsaved content in chat
- [x] 5.2 Create the "Standards" page under the curate.d project page, with the layout of design D13: the opening callout (repository source paths, the date), the precedence table, one section for each of the three standards with a summary and links to the repository on `dev`, the pin, and the deployment findings that design D6 keeps out of the repository. Summaries and links only; no copied repository text and no credentials. Verify by fetching the page: the four ranks read in the order of design D2, and the three standards each have a section
- [x] 5.3 Create the 11 rows of design D13 in the Roadmap items database, with the existing properties and options only: Area "Deploy & ops", Status "Wanted", the Type and Horizon D13 gives, a Why of one to three sentences, Source "Twelve-Factor audit, 2026-11-04", and a body with Scope bullets that name the rules covered and link the existing rows D13 cross-links. Edit no existing row and add no property, option or view. Verify by querying the data source: 11 new rows with those values, and the schema unchanged
- [x] 5.4 On the Standards page, add the Twelve-Factor gap table: one line for each of the 15 gaps, with its rule and its Roadmap row (X.1 points at the row of task 5.6). Then re-read the project page and add the page to "Navigate", under "Reference and research", and to "Subpages" if the new child page is not already listed there. Verify by fetching both pages: every gap has a link, and the project page links the Standards page once in each place
- [x] 5.5 Keep the tracking row of this change current (design D13). Query the Roadmap items database for the row "Adopt the Twelve-Factor manifesto as a standard", which exists since 2026-11-05; if the query does not find it, create it with Area "Specs", Type "Decision", Horizon "Now", Status "Picked up" and Source "Twelve-Factor audit, 2026-11-04". Re-read the row, then add to its body a link to the Standards page and to each of the 11 gap rows. It is the only existing row this stream edits. Verify by fetching the row: one row with that title exists, its Status is "Picked up", and its body links the Standards page and the 11 rows
- [x] 5.6 Create row 12 of design D13, "Deploy curate.d to production", because no existing row held the first production release, and point X.1 at it on the Standards page and in the tracking row. Verify by querying the data source and fetching both pages

## 6. Integration checks

- [x] 6.1 Verify `make lint typecheck test` passes, `openspec validate adopt-twelve-factor-standard --strict` passes, and `git status --short` lists only `docs/twelve-factor.md`, `tests/unit/infrastructure/test_engineering_standards.py`, `openspec/config.yaml`, `CLAUDE.md`, `README.md`, `CONTRIBUTING.md` and this change's directory

## Workflow follow-up

- Open the pull request against `dev`. It touches `CLAUDE.md` and `openspec/`, which the auto-approve policy excludes, so it needs the owner's review. The integration suite is skipped by its path filter; the checks workflow runs the new unit test.
- The reviewer reads `docs/twelve-factor.md` against design D6: no other repository, hostname or account is named.
- When the pull request opens, add its link to the first line of the tracking row's body.
- After the merge, check that the Standards page's links to `docs/twelve-factor.md` on `dev` resolve, and add an "Updated" line with the merge date and the pull request to the page's callout.
- Sync the delta into `openspec/specs/engineering-standards/spec.md` and archive the change.
- After the archive, set the tracking row's Status to "Done", and re-read the row to confirm it.
- Owner, outside the repository: the machine-wide copy of the `dbos-python` skill is still 2.1.0, and a session can load it before the project's copy.
