# Design

## Context

See `proposal.md` for the motivation. The `gitar-approve-dependabot-updates` design (2026-10-30)
kept the low-risk gate and foresaw this outcome: "Gitar's risk triage rates every dependency bump
above low → Then nothing is approved". The evidence since then:

- Gitar rated the Dependabot pull requests read here medium: #124 to #127, #134, #141.
- Its verdicts say what it can't read. #134: "the commit's authorship/committer and its
  updated-dependencies block are not visible". #141 says the same. Gitar's documentation lists
  metadata, changed files, diff and review results as approval input, and never mentions commit
  authors, committers or messages.
- The GitHub API shows #141 meets the old condition (`dependabot[bot]` author, `web-flow`
  committer, signed, block present), so the gap is in what Gitar reads, not in the pull request.
- Gitar's documentation: repository risk guidance "can raise the risk level" and "cannot lower the
  organization's assessment". Criteria may accept a higher level ("Approve only low- or medium-risk
  changes."). Organization and repository criteria combine, "so the PR must satisfy all criteria".
- "By default every auto-approved PR is armed for merge", unless the organization requires
  `.gitar/config/merge.md`. No such file exists here.
- The `dev` ruleset needs no approving review; `Checks`, `Integration tests`, code scanning and
  resolved threads hold the merge, and it allows only the merge and rebase methods.

## Goals / Non-Goals

**Goals:**
- An eligible Dependabot update that Gitar rates medium is approved and auto-merge is armed.
- Every condition in category 4 is one Gitar can establish from what it reads.

**Non-Goals:**
- Categories 1 to 3. They stay at low risk with their commit conditions. The same blind spot may
  stop them ("Identity unknown"); that is not confirmed, and it is a separate change if so.
- Widening which updates are eligible: no `github-actions`, major, `0.x` minor or `docker` minor.
- Gitar's dashboard settings, which this repository can't hold.
- A new workflow or required check.

## Decisions

- **Accept medium in `approve.md`, for category 4 only.** Alternatives: `.gitar/config/risk.md`
  can only raise a rating. Organization risk instructions could lower it, but they reach every
  pull request, not only Dependabot's.
- **Accept medium for the whole of category 4, not only `docker` patches.** Every Dependabot pull
  request rates medium, so a narrower gate would leave `uv` patches and minors unreachable and
  undo the owner's decision of 2026-10-30 that they are eligible. This is the assumption to
  confirm in review: to limit it, name patches only in the risk rule, and `uv` minors go back to
  waiting for a human.
- **High and unavailable still refuse.** Gitar uses high when its assessment fails, so this also
  keeps the fail-closed path.
- **A commit shown refuses; a commit not shown does not.** The old condition can never be
  established, and "When uncertain" turns that into a refusal of every pull request. Alternatives:
  a workflow that checks commits and sets a label Gitar reads, or a `Checks` step. The label adds a
  privileged workflow and a race with Gitar's review; the check would also fail a human's
  legitimate fix on a Dependabot branch. Both stay available later, see Risks.
- **The diff replaces the `updated-dependencies` block.** The old text already judged update types
  from the diff, "never from the block alone", so the block only supplied the list of names, and
  the diff gives that too. Dependabot truncates long descriptions, so the description is a
  cross-check that refuses on disagreement and never a required source.
- **No `merge.md`.** Approved pull requests are armed by default, and a file would add a second
  policy to keep in step. If the organization requires the file, add it then.

## Risks / Trade-offs

- [A medium rating can name a real behaviour change, as with `pypdf` parsing in #134, and it no
  longer stops the merge] → The required checks and the integration suite are the only test. A
  break they miss is found after the dev image deploys and is reverted like any other merge.
- [A compromised release merges without a human] → Unchanged from the 2026-10-30 design in kind,
  and larger in effect, since this change is what makes that path reachable. A Dependabot
  `cooldown` would delay adoption and is a separate change.
- [A write-access account pushes to a Dependabot branch and Gitar does not see it] → The diff rules
  still bind every line, so the push can change only version references, and only an account with
  write access can do it. Gitar's auto-apply is off there, since Dependabot's pull requests carry
  no `gitar-managed` label. If this becomes a concern, the label workflow above closes it.
- [Organization criteria also require low risk, or auto-merge is off] → This change then approves
  or merges nothing. Neither can be read from the repository; the owner checks both.
- [Gitar's reading of a free-text policy differs from the intent] → The verdict on the next
  Dependabot pull request shows it, and a refusal names its condition.

## Migration Plan

A human merges this into `dev`, since Gitar refuses a change to `.gitar/`. Gitar reads the file
from `dev`. A pull request is judged again only on a new review, and no comment command forces one:
open pull requests #141 to #145 need a new push, such as `@dependabot rebase`, or they wait for
Friday's updates. Rollback: revert the commit.

## Parallelism

Two work streams on disjoint files, both able to start at once:

- **A**: `.gitar/config/approve.md`.
- **B**: `CONTRIBUTING.md`, `.github/dependabot.yml`.

No shared edit; the spec sync runs at archive.
