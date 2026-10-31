# Design

## Context

See proposal.md for the reason. What Gitar's documentation says about the label (read
2026-10-30):

- `gitar-managed` is added by "a person or automation". "Automation can include the label when
  creating a PR. Adding it to an existing PR starts processing" (docs.gitar.ai/labels).
- An explicit `gitar auto-apply:on` or `:off` on a pull request takes precedence over the label.
- Gitar skips bot-authored pull requests unless the bot is in its organization setting "Allowed
  bots". This setting is not in the repository.

GitHub gives a workflow's `GITHUB_TOKEN` write access on `pull_request` events from branches in
this repository, and read-only access for forks and for Dependabot. A label that `GITHUB_TOKEN`
adds triggers no other workflow, but GitHub still sends the `labeled` webhook to apps such as
Gitar.

## Goals / Non-Goals

**Goals:**

- The owner's pull requests open with auto-apply on, with no comment needed.
- Dependabot pull requests open with auto-apply off.

**Non-Goals:**

- Changing `apply.md` or `approve.md`. The label is not a condition in either, and stays so.
- Labelling pull requests from `claude[bot]` (the `claude.yml` Action) or any collaborator.
- Removing the label from Dependabot pull requests that are already open.
- A unit test that pins the label or the workflow: the label exists only for Gitar.

## Decisions

- **A workflow, not `gh pr create --label`.** A workflow labels every pull request the owner
  opens, from the GitHub UI, from `gh`, or from a Claude Code session, and needs no instruction
  in `CLAUDE.md` or a skill that an agent could forget. A Gitar setting would be simpler, but the
  documentation names none for auto-apply by author.
- **Trigger on `opened` only.** "Created with the label" is the request. On `reopened` or
  `synchronize`, the owner may already have removed the label on purpose; adding it again would
  undo that.
- **Match the author against `github.repository_owner`, and the head against this repository.**
  The owner's login is not written into the file, and a fork never matches. The author is the
  pull request's `user`, not the commit authors, so a `Co-Authored-By` trailer does not matter.
- **The REST labels endpoint through `gh api`, with `pull-requests: write` only.** The workflow
  checks nothing out and runs no third-party action, so there is no action version to pin and no
  code from the branch runs. Top-level `permissions: {}` keeps every other scope off.
- **Not a required check.** A failed run shows as a red run on the pull request and the label is
  missing; the owner adds it by hand. Labelling must not hold a merge.
- **Dependabot falls back to its default labels.** Removing `labels:` restores `dependencies`
  and the ecosystem label, which `labels` had replaced.

## Risks / Trade-offs

- [Gitar is a second writer on every owner branch: a plain push is refused until its commits are
  pulled, and a rebase with `--force-with-lease` after a fetch drops them silently] → Owner's
  rule, in `CONTRIBUTING.md` and the steward skill: pull before you push, and check `git log`
  for commits you did not write before a force-push; if there is one, merge instead of rebasing.
- [A Gitar fix on the owner's pull request ends its auto-approval: `approve.md` refuses a pull
  request that carries a bot's commit] → Intended, and confirmed by the owner on 2026-10-30: the
  owner reviews every change Gitar adds and merges those pull requests. `approve.md` keeps
  refusing Gitar's commits.
- [Gitar may not act on a label that `github-actions[bot]` adds] → The documentation says
  automation may add it. Verify on the first pull request after the merge; if Gitar ignores it,
  fall back to `gh pr create --label` in the steward skill.
- [The `gitar-managed` label is deleted from the repository] → GitHub's labels endpoint may
  create it again or refuse; either way the run's log shows it. Not verified; recreate the label
  with its usual name if needed.
- [Dependabot pull requests that are open today keep the label] → Remove it by hand or let the
  next run supersede them.
