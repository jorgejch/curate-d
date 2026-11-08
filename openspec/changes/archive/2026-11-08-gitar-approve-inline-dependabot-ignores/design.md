# Design

## Context

See `proposal.md`. Evidence, read from GitHub on 2026-11-08:

- Every Gitar dashboard comment from #120 to #149 shows "Auto-approval: Withheld". No pull request
  in that range has a review from Gitar, and none had auto-merge armed except #139, which the owner
  armed. Every Dependabot pull request was merged by the owner.
- "Code Review: Approved" in those comments is the review verdict (no blocking finding). It is not
  a GitHub approval; Gitar's approval is a submitted review that reads "Gitar has auto-approved this
  PR", and auto-merge is armed only after one.
- #142 to #145 were reviewed at 00:08 UTC on 2026-11-07, before #146 admitted medium risk (merged
  02:42 UTC), and were not reviewed again before they merged. #141 was rebased at 02:43 UTC, reviewed
  under #146's criteria at 02:43:52 UTC, and withheld only on **Not ignored**.
- Repository settings already allow auto-merge; the `dev` ruleset needs no approving review and
  allows the merge and rebase methods.

## Goals / Non-Goals

**Goals:**
- Every category 4 condition can be judged from what Gitar reads.
- The copied ignore rules can't drift from `dependabot.yml` unnoticed.

**Non-Goals:**
- Changing which updates are eligible.
- Gitar's dashboard settings (auto-approve, auto-merge, merge method), which the repository can't hold.

## Decisions

- **Copy the rules into `approve.md`, guarded by a test.** Alternatives: tell Gitar to read
  `dependabot.yml` (the documentation gives criteria no way to read another file, and #141 shows it
  didn't); drop the condition (a security update for an `aioboto3` minor would then merge without
  the RustFS and OVH re-verification the ignore rule exists for). A copy is a second place to edit,
  and the unit test turns forgetting into a red build.
- **Judge from the list alone.** "Not on the list" must be an answer Gitar can give; otherwise
  "When uncertain" refuses again.
- **List entries by ecosystem, dependency and update type.** That is what an `ignore` rule with
  `update-types` holds, and `test_no_ignore_holds_back_patches` already forbids rules without
  `update-types` or with `versions`, so the triple is the whole rule.

## Risks / Trade-offs

- [Gitar's reading of the list differs from the intent] → The next Dependabot verdict shows it.
- [Someone edits the list and the file to agree on a wrong rule] → Both are human-only paths;
  the pull request is reviewed as any other `.gitar/` and `.github/` change.
- [Approval still arms nothing] → If the dashboard's auto-merge is off or set to squash, which
  the `dev` ruleset refuses, approved pull requests stay unmerged. The owner checks it.

## Migration Plan

A human merges this into `dev`. The next Dependabot pull request, or `@dependabot rebase` on an
open one, gets a new review under the new text.
