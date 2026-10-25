# Design

## Context

The plan job's `changes` step (`dorny/paths-filter`) reads the pull request's changed files
from the GitHub API, which lists at most 3,000 of them. Its `suite` output is `true` when a
listed file matches a declared path. The next step, "Refuse an undecided detection", gets that
output as `SUITE` and `github.event.pull_request.changed_files` as `CHANGED_FILES`, and fails the
plan on three conditions: `SUITE` is neither `true` nor `false`; `SUITE` is `false` and
`CHANGED_FILES` is 3,000 or more (empty or unset reads as 3,000); `SUITE` is `false` and a
packaging file is missing. `tests/unit/infrastructure/test_integration_workflow.py` pins the
step's position, its env and that its script mentions `-ge 3000`, but not the condition the
count check runs under.

## Goals / Non-Goals

**Goals:**
- Every pull request whose file list may be incomplete fails the plan, and `Integration tests`
  with it, whatever the detection reported.
- A unit test that runs the guard's script, so the condition is pinned by behaviour, not by
  the script's text.

**Non-Goals:**
- Listing more than 3,000 files (a `git diff` against the base instead of the API). It would
  lift the limit, but it changes how the detection works; the requirement asks only that an
  uncertain list fail.
- Changing the packaging-file check. It guards the image build on a skip; on a selected run
  the boot job builds the image and fails on its own.

## Decisions

- **Fail at the limit whatever `SUITE` is.** A `true` from a truncated list is correct about
  the suite but still came from a list the requirement calls uncertain, and the plan has no
  other use for an incomplete list. One rule keeps the outcome from depending on which 3,000
  files the API happened to list first. The alternative, failing only on `false`, is the
  behaviour this change removes: it is the narrower reading the spec's scenario took, not what
  the requirement says.
- **Drop the condition, keep the order.** The `SUITE` value check stays first, so a broken
  detection is named as such; the count check follows unconditioned; the packaging check stays
  last and conditioned on `false`.
- **Test the script by running it.** The test extracts the step's `run` and executes it under
  `bash --noprofile --norc -eo pipefail`, the shell GitHub Actions uses for `run` steps, with
  `SUITE` and `CHANGED_FILES` set per case and the repository root as the working directory (the
  packaging files are there). A text assertion on the script would pass for a reordered or
  reworded condition that still exempts `true`.

## Risks / Trade-offs

- [A legitimate pull request of 3,000 files or more that selects the suite can no longer
  merge] → Intended: the requirement already says it must fail, and the error says to split it.
  None is open today.
- [The test needs `bash` on the machine running the unit suite] → `bash` is on every CI runner
  and developer machine the repository supports; the test fails, not skips, without it.

## Migration Plan

Merge into `dev`. Rollback is reverting the commit.

## Parallelism

One work stream. The workflow guard and the test that runs it touch two files that depend on
each other, so the change can't be split.
