# Proposal

## Why

Five open issues (#11, #12, #14, #13, #2) leave policy and placement decisions that the ledger
records but that cannot be trusted, or that an operator cannot check before they take effect:

- Retention counts copies nothing checks any more (#11).
- A retention field changes no outcome (#12).
- A policy can name values that never match or never render (#14).
- A draft policy cannot be tried before it is activated (#13).
- A rewound placement can verify the wrong copy, and its audit entries cannot explain it once
  workflow history is pruned (#2).

Nothing releases an original yet. Each of these gaps becomes a loss path once source release
is built, so they have to land before it.

The fixes rewrite the same functions: `domain/policy.py`, `reconciliation/retention.py`,
`configuration/documents.py` and `api/configuration.py`. Open PRs #17, #16, #23, #19 and #21
implement them. Each forks from 4ad75ee, 200 commits behind `dev`, and conflicts with the
others. This change lands the five as one, salvaging those PRs as reference implementations.

## What Changes

- **Retention counts only checked copies (#11).** `decide_retention` counts only placements
  at destinations the policy in force defines. Present placements elsewhere are listed as
  `unchecked_copies` in the decision, its reason and its audit details. Dropping a
  destination can therefore turn `release_permitted` back into `retain`, which is the safe
  direction.
- **BREAKING (policy document): `RetentionRule.source_counts_as_copy` removed (#12).** A new
  `PUT /configuration/policy` carrying the key is refused as an unknown field. Stored versions
  carry it, because activation stores the full dump, so the stored-policy parser drops it on
  read. No data migration. The retention reason no longer depends on the field. The README
  and the spec scenario that called the original a counted copy are rewritten.
- **Unmatchable values refused (#14).** Refused with 422 and their location, in placement
  rules, retention rules and templates:
  - a `media_kind` condition that is not a `MediaKind`;
  - a destination `profile` that is not a `CapabilityProfileName`.

  Category and subcategory conditions are checked against the taxonomy in force, inside the
  activation transaction, under the taxonomy's advisory lock held shared. Activating a
  taxonomy is never refused for orphaning a condition: it answers with the orphaned
  conditions and logs them as `configuration.orphaned_conditions`. Reads never check
  taxonomy values.
- **Stored configuration check before deploy (#14).** Reads stay strict, with no lenient
  path. A new operator command, `python -m curate_d.configuration.verify`, parses every
  stored version with the read parsers and exits non-zero if a version in force would not
  read. Running it is a documented pre-deploy step.
- **Dry run of a draft (#13).** `POST /dry-runs` accepts a draft `policy` and, optionally, a
  draft `templates`. Both pass the validation `PUT` applies, including the taxonomy check,
  and neither is stored as a configuration version.
  - The run header records `policy_source`, `policy_version`, `policy_rules`,
    `templates_draft` and the draft documents.
  - Each item names the rules that selected its destination.
  - The export counts every rule, zero for a rule that selected nothing.
  - Classified content that no rule selects is reported as one `unselected` item.
  - One migration covers these columns.
- **Rewind guards (#2).**
  - `place_content` keys its verification child on the attempt `start` recorded, not
    `request.attempt`.
  - A starting copy clears the previous copy's evidence.
  - Evidence, a wait reason and a failure are recorded only while their attempt is the
    placement's latest and still copying.
  - `conclude` acts only on a placement still VERIFYING that attempt. A superseded workflow
    ends `superseded` and writes nothing.
  - Audit details gain the source version marker, the destination provider, account and root,
    and the policy version on `placement_started`; `retention_decided` gains the policy
    version.
  - A new `docs/rewind.md` runbook: rewind collapsed children after the parent, never delete
    them.
  - The content-ledger spec gains the invariant that every audited decision commits with its
    audit entry in one transaction.
  - CLAUDE.md's rewind notes are corrected to match.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `placement-reconciliation`:
  - Retention counts only copies at destinations the policy in force defines (#11).
  - "Retention as policy outcome" is replaced by "Retention is a policy outcome". Its
    "source counts toward required copies" scenario becomes "required copies exist without
    the source" (#12).
- `control-plane`:
  - Unknown `media_kind` and profile values are refused, and taxonomy values are checked at
    activation (#14).
  - A taxonomy activation reports the conditions it orphans, and a stored version that no
    longer reads fails loudly (#14).
  - Dry runs accept drafts and export the selecting rules and unselected content (#13).
- `placement-execution`: a verification is bound to the attempt `start` recorded, and late or
  replayed attempts change nothing (#2).
- `content-ledger`:
  - Audit entries carry their evidence (#2).
  - New requirement: every audited decision commits with its entry in one transaction, and
    rewind re-executions append (#2).

## Impact

- **Code:**
  - `domain/`: `policy.py`, `conditions.py` (new), `taxonomy.py`, `placement.py`,
    `retention.py`, `__init__.py`.
  - `configuration/`: `documents.py`, `store.py`, `consistency.py` (new), `verify.py` (new).
  - `api/`: `configuration.py`, `dry_runs.py`.
  - `rendering/`: `templates.py`, `planning.py`.
  - `reconciliation/`: `retention.py`, `convergence.py`.
  - `placement/`: `policy.py`, `dry_run.py`, `pipeline.py`.
  - `workflows/`: `reconcile.py`, `placement.py`, `dry_run.py`.
  - Also `classification/taxonomy.py` and `ledger/tables.py`.
- **API:**
  - `PUT /configuration/{policy,templates}` gains 422 cases.
  - `PUT /configuration/taxonomy` gains an `orphaned` field.
  - `POST /dry-runs` gains `policy` and `templates`.
  - The status and export of `GET /dry-runs/{id}` gain policy and draft fields, `rules`,
    `rule_counts` and the `unselected` outcome.
- **Schema:** one migration on `dry_runs` and `dry_run_items`, taking the next free head
  after `0021_staging_reservations` (or `0002_*` if #7's baseline lands first).
  `test_migrations.py` gains a single-head assertion.
- **Workflows:** `verify_placement` gains an optional `attempt` argument. Verifications
  enqueued without it still run, checking only that the placement is VERIFYING.
  `PlaceOutcome` gains `SUPERSEDED`.
- **Docs:** `README.md`, the new `docs/rewind.md`, `docs/recovery.md` (stored configuration
  check) and `CLAUDE.md` (rewind notes).
- **Deploy:** run the stored configuration check before rolling out. It catches a version in
  force that names an unknown profile or `media_kind`, which would otherwise stop every read
  of it.
- **Out of scope:** release after repair, and provider copy and delete by object identity (#2,
  source-release work). Checking dropped destinations as well is deferred to #3. That change,
  `tie-placements-to-destination-identity`, rewrites `pipeline.py`'s `start`, `fail` and
  `conclude` and `decide_retention`'s destination parameter, so it rebases after this one.
