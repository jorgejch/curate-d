# Design

## Context

See `proposal.md`. Evidence, read from GitHub on 2026-11-14:

- #163, #164 and #165 were each reviewed "Approved" at medium risk. Only #163 shows
  "Auto-approval: Approved" and a submitted Gitar approval; it auto-merged. #164 and #165 show
  "Auto-approval: Withheld" with the reasons quoted in the proposal, and were merged by the owner.
- All three show "Rules: No rules evaluated". That is Gitar's Rules feature, which needs rules
  configured; it is not part of the auto-approve verdict.
- #165's `uv.lock` diff: four version, source, sdist and wheel changes, two `requires-dist`
  specifier rewrites matching `pyproject.toml`, and one added line, `{ name = "anyio" }`, in
  `fastapi`'s `dependencies`. `anyio` has its own `[[package]]` entry before and after.
- Workflow `uses:` refs today: most third-party actions are pinned to a commit SHA with a
  `# vX.Y.Z` comment (`claude-code-action` carries `# v1`); `actions/*` and some `docker/*` actions
  use a major tag (`@v7`). Dependabot updates a SHA pin by moving the SHA and the comment, and a
  tag pin only across a major.
- GitHub's Dependabot now applies a three-day cooldown to version updates by default (GitHub blog,
  2026-07-23); `cooldown` applies to version updates only, not security updates.

## Goals / Non-Goals

**Goals:**
- An update shaped like #164 or #165 can be approved, with the risk it carries bounded and stated.
- Human pull requests that can't change what runs aren't refused for a medium rating or for
  touching editor and Git ignore files.
- Every new condition can be judged from what Gitar reads: the pull request's metadata, diff and
  review, and `approve.md` itself.

**Non-Goals:**
- Agent and reviewer instructions (`CLAUDE.md`, `.claude/`, `.agents/`), specs, code, or any
  workflow change beyond an action's pinned commit.
- Gitar's own fix commits, which `CONTRIBUTING.md` says always wait for a human.
- Gitar's dashboard settings, which the repository can't hold.

## Decisions

- **Lock edges among locked packages.** A changed `dependencies` list can't install anything the
  lock didn't already hold, since adding a package still refuses, and the required checks run on
  the result. Alternative: list the edge as a reason to refuse (today's reading); every patch that
  adds or drops a requirement would wait for a human. Alternative: allow any lock change bounded
  by the package set; that would admit a `requires-python` or marker change Gitar can't judge.
  The rule names the three list keys and nothing else.
- **Actions only by commit SHA, update type from the version text.** A SHA can't be moved under
  the pull request after review, and a tag pin changes only across a major, which refuses anyway.
  The update type comes from the trailing `# vX.Y.Z` comments when both carry three numeric parts,
  otherwise from Dependabot's title (`from 1.0.268 to 1.0.271`); the pull request's author is
  `dependabot[bot]`, so the title is Dependabot's, and a diff that contradicts it still refuses.
  Gitar can't check that a SHA belongs to the named release; the cooldown and the required checks
  are what bound that.
- **Explicit seven-day cooldown on `github-actions`.** The default three days covers fast
  takedowns; a week covers more of the compromised-release window seen in past action supply-chain
  incidents, at the cost of action fixes landing a week later. Security updates aren't delayed by
  `cooldown`, by GitHub's design. Only `github-actions` gets it: `uv` and `docker` updates already
  run nothing with secrets on merge beyond what the required checks run.
- **`0.x` actions follow the `uv` rule.** A minor of a `0.x` action refuses, as for packages.
- **Medium risk for categories 1 to 3.** These categories can't change what runs; the rating
  measures uncertainty Gitar can't resolve, and a blocking finding still refuses. High stays out.
- **Size 20 files / 500 lines.** A docs restructure or a test file of several cases passes 200
  lines without being any harder to judge by category. #161 (364 lines) fits.
- **Hygiene files as a category of their own**, not as "documentation": `.editorconfig` and
  `.gitignore` change what an editor or Git does locally. `.dockerignore` changes the image build
  context and `.gitattributes` can name filters, so both stay human.
- **`web-flow` with a human author is the human.** GitHub commits web-editor edits as `web-flow`
  (`GitHub <noreply@github.com>`) and signs them; the author is the human who made the edit.

## Risks / Trade-offs

- [A compromised action patch or minor merges unread and runs with the repository's secrets] →
  SHA pins, seven-day cooldown for version updates, majors and tag refs refuse, the required checks
  still run first. Residual: a security update skips the cooldown. The owner can drop the action
  bullet from category 4 to undo it alone.
- [A changed lock edge alters what gets installed] → Only among packages already locked; the
  required checks run against the new lock.
- [A medium-risk prose change is wrong] → It can't change what runs; runbooks stay human.
- [Gitar reads a new condition differently from the intent] → The next Dependabot run and the
  next docs pull request show the verdicts; the examples pin #164's and #165's shapes.

## Migration Plan

A human merges this into `dev`. The next Friday Dependabot run, or `@dependabot rebase` on an open
pull request, gets a review under the new text. To roll back, revert the merge.

## Twelve-Factor

No rule's status changes: this touches review policy and Dependabot's schedule, not the
application, its config or its processes.
