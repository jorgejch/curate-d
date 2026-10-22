# Design

## Context

The motivation is in proposal.md. This section covers only the facts that shape the
approach.

**The source branches.** PRs #17 (#11), #16 (#12), #23 (#14), #19 (#13) and #21 (#2) each
fork from 4ad75ee, 200 commits behind `dev` (bcec791). Since 4ad75ee, `dev` changed only
these of their files:

- `README.md`: #28 removed the Conductor sentences.
- `ledger/tables.py`: the `lease_versions` table.
- `tests/integration/test_api_configuration.py`: 6b9191a seeds configuration defaults before
  the restore snapshot.
- `migrations/versions/0020_lease_versions.py`.

The branches overlap each other heavily. Each is fetched locally as `refs/remotes/pr/<n>`,
and `git diff 4ad75ee pr/<n>` is the reference implementation for its issue.

**Stored configuration.** Facts found while planning, not in the issues:

- `ConfigurationRepository.activate` stores `parsed.model_dump(mode="json")`, the full dump.
  Every stored policy version therefore carries `source_counts_as_copy`, because the field
  defaulted to `true`. Removing the field without a lenient stored-policy parser would make
  every existing policy unreadable.
- Only the version in force of each kind is ever parsed on read: `active_policy`,
  `active_templates`, `active_taxonomy`, and #23's `orphans_in_force`.
  `ConfigurationRepository.history` returns metadata only, and nothing reactivates an older
  version.

**Schema drift test.** `tests/integration/test_ledger_schema.py::test_migrations_leave_no_difference_against_the_model_metadata`
compares `ledger/tables.py` with the migrated schema. A migration and its table columns must
therefore land in the same stream.

**Shared test fakes.** `tests/unit/api/fakes.py` holds `FakeConfiguration`, which #23
changes, and `FakeDryRuns`, which #19 changes, in the same file.

**DBOS.** All placement guards run inside the existing datasource transactions:
`_start_tx`, `_evidence_tx`, `_defer_tx`, `_fail_tx` and `_conclude_tx`. The verification
child stays a workflow-body enqueue under a deterministic ID. Nothing new starts a workflow
from a step. The `dbos-python` skill was consulted and nothing departs from it, so CLAUDE.md
gains no new departure. CLAUDE.md's rewind bullets do contradict decisions recorded on #2,
and are corrected (D12).

## Goals / Non-Goals

**Goals:**

- Land #11, #12, #14, #13 and #2 as one change, re-implemented on `dev` from the PR diffs.
  The PRs are references, not merge candidates.
- Settle the two questions the issues left to design: #14's read path (D6) and #13's
  unselected content (D8).
- Split the apply into as many parallel streams as the files allow, with the fewest
  cross-stream waits (D13).

**Non-Goals:**

- Release after repair, and provider copy and delete by object identity. Both stay on #2
  for the source-release work.
- Checking dropped destinations as well as counting only defined ones. Deferred to #3, whose
  change, `tie-placements-to-destination-identity`, rebases on this one.
- A rewind surface or a `workflow_rewound` audit type. Both were withdrawn on #2 on
  2026-10-11.
- A lenient read path for stored configuration (D6).

## Decisions

### D1. Re-implement per stream; close the PRs as superseded

Each stream re-applies its issue's diff from `pr/<n>` onto `dev`, adapted to the decisions
below. A rebase of five conflicting branches would cost more than it saves. When the change
lands, PRs #16, #17, #19, #21 and #23 are closed as superseded, with a link, never deleted.

### D2. #11: count only copies at destinations the policy in force defines

Keeps the choice recorded on #11.

- `decide_retention(rule, placements, *, destinations)` takes
  `PlacementPolicy.destination_names()`.
- Present placements elsewhere are excluded from both `strong_copies` and `weak_copies`.
  They are listed in `unchecked_copies`, appended to the reason as
  `; N copies at destinations no longer in the policy do not count (b)`, and written to the
  audit details.
- "Defined" means listed in `policy.destinations`, exactly the set `check_destinations`
  iterates. It does not mean selected by a rule.

**Alternative:** keep checking every destination that holds a PRESENT placement. That needs
each placement's account and root, so it is deferred to #3.

### D3. #12: remove the field; stored versions drop it on read

- `RetentionRule.source_counts_as_copy` is removed. Pydantic then refuses it as an unknown
  field, located at the rule, on `PUT` and in a dry-run draft.
- `configuration/documents.py` gains `parse_stored_policy`, which strips the keys in
  `_RETIRED_RETENTION_FIELDS` from each retention rule and then calls `parse_policy`. Every
  read of a stored policy uses it:
  - `placement/policy.py::active_policy`;
  - #23's `consistency.orphans_in_force` (#23 used `parse_policy`, which would now fail on
    every stored version);
  - the stored configuration check (D6).
- The reason branch that counted the original goes. With one strong copy of two required,
  the reason is `1 of 2 required copies are strongly verified`.
- Workflow checkpoints that pickled a `RetentionRule` with the attribute still load, because
  unpickling sets `__dict__` and does not validate.
- No data migration: stored versions are never edited in place.

### D4. #14: refuse values that can never match or render, in the domain

- `domain/conditions.py` (new) holds `CONDITION_FIELDS`, `matches`, `check_conditions`,
  `orphaned` and `taxonomy_in`.
  - `check_conditions` refuses a `media_kind` that is not a `MediaKind`, and a malformed
    category or subcategory (`domain/taxonomy.py`'s `_check_value` becomes the public
    `check_value`).
  - With a taxonomy, it also refuses a value the taxonomy does not list.
- Placement rules, retention rules and templates call `check_conditions` from their
  `model_validator`, which takes `ValidationInfo`. They read the taxonomy from the
  validation context: `parse_policy(..., taxonomy=)` and `parse_templates(..., taxonomy=)`
  pass `context={"taxonomy": taxonomy}`. Reads pass none, so a later taxonomy change can
  never make a stored document unreadable.
- `CapabilityProfileName(StrEnum)` lives in `domain/policy.py`, so the domain never imports
  rendering. `DestinationConfig.profile` stays declared a `str`, as on `dev`, and a field
  validator refuses a value no member holds with the same `enum` error, message and
  location the enum-typed field would give.
  - `rendering/templates.py`'s `PROFILES` is keyed by the enum.
  - A unit test asserts `set(PROFILES) == set(CapabilityProfileName)`.
  - `plan_at` drops its unknown-profile branch and indexes `PROFILES` with
    `CapabilityProfileName(destination.profile)`: the one reader converts at use.
- The field is not retyped because `DestinationConfig` is checkpointed in `PlacementRequest`
  and the `check_destination` input, and `DomainModel.__setstate__` restores a pickle's
  fields without validating them: a checkpoint written before this change holds the plain
  string, and a retyped field is a breaking deploy (CLAUDE.md, DBOS). Declaring `str` keeps
  the old pickle inside the declared type with no drain; a `StrEnum` hashing like its value
  would have hidden the mismatch, not removed it.

### D5. #14: taxonomy values are checked at activation, under the taxonomy's lock

- `ConfigurationRepository` gains `lock(kind)`, the existing exclusive transaction advisory
  lock factored out, and `lock_shared(kind)`, its shared form.
- A policy or template activation takes `lock_shared(TAXONOMY)`, reads the taxonomy in force
  (`consistency.taxonomy_in_force`, or the seed when none was ever stored), and validates
  against it in the same transaction.
- A taxonomy activation already takes the lock exclusively.
- `classification/taxonomy.py::promote` takes `lock(TAXONOMY)` before it reads the version in
  force, so a concurrent activation is extended rather than overwritten. Promotion only adds
  values, so it never orphans a condition.

A taxonomy activation then computes `orphans_in_force` against the policy and templates in
force, logs `configuration.orphaned_conditions` (WARNING) with the taxonomy version, the
count and the conditions, and answers with `orphaned`, a list of
`OrphanedCondition(kind, owner, field, value)`. `ConfigurationBackend.activate` returns
`Activated(version, orphaned)`.

**Alternative:** warn instead of refusing. Rejected: the API has no warning channel, and the
control-plane spec requires invalid configuration to be refused before it takes effect.

### D6. Read path: strict reads, plus a stored configuration check before deploy

Settles #14's open question.

Reads parse with the same checks as submissions, except the taxonomy check and the retired
key D3 drops. A version in force that names an unknown profile or `media_kind` fails every
read of it, and nothing is evaluated from a partial reading. To catch such a version before
it can stop anything, a new operator command checks the stored versions:

```bash
kubectl -n <namespace> exec statefulset/curate-d-worker-io -- python -m curate_d.configuration.verify
```

- Run from the image about to be deployed, against the target database, before rollout.
  This mirrors `python -m curate_d.runtime.promote`; `docs/recovery.md` gains a "Before
  deploying" section.
- It parses every stored version of every kind with the read parsers (`parse_taxonomy`,
  `parse_templates`, `parse_stored_policy`) and prints each failure with its kind, version
  and problems.
- Exit 0: every version in force reads. Exit 1: a version in force does not read; older
  versions that fail are listed but do not change the code. Exit 2: the database could not
  be read.
- The parsing loop is a pure function over `(kind, version, active, document)` rows, so it is
  unit-testable without a database.

If the check is skipped and a version in force does not read:

- reconciliation's and dry runs' open transactions raise a `ConfigurationError` naming the
  kind and problems, rather than a bare `ValidationError`, and the workflow fails loudly;
- `GET /configuration/{kind}` fails;
- `PUT /configuration/taxonomy` is refused with 422 naming the unreadable kind, because its
  orphan report cannot be computed;
- recovery is a `PUT` of a valid document; policy and template activation never read the
  version in force.

**Alternative:** a lenient read that drops an unknown destination or condition. Rejected:
dropping a destination silently changes placement and, through D2, retention; dropping a
condition widens what a rule selects; both are inference over an uncertain value, which the
project context forbids. Today the same mistake already fails every placement at that
destination as `render_failed`; a loud failure fixed by one `PUT` is the better trade.

**Alternative:** a SQL query in the runbook. Rejected: it would hard-code `MediaKind` and the
profile names and drift from the parser.

### D7. #13: drafts travel in the request, are validated before enqueue, and are recorded on the header

- `DryRunOrder` and `DryRunRequest` gain `policy` and `templates`. A draft policy combined
  with named destinations is refused with 422.
- `LedgerDryRuns.start` validates each draft before it enqueues, through `check_submission`
  in `api/configuration.py`, the function activation also uses: a short transaction,
  `lock_shared(TAXONOMY)`, `taxonomy_in_force`, parse with `taxonomy=`, and a
  `ConfigurationError` mapped to `RejectedError` (422).
- The lock is not held until the workflow opens. Nothing is stored, the header records the
  `taxonomy_version` the run evaluated with, and a taxonomy changed in between only makes a
  rule match less.
- The workflow's `_open_tx` uses the request's drafts instead of the versions in force and
  records on `dry_runs`: `policy_source` (`active`, `draft` or `destinations`),
  `policy_version` (set only for `active`), `policy_rules` (rule names in policy order),
  `templates_draft`, and `policy_document` and `templates_document` (the drafts as
  evaluated). The documents are stored because DBOS's workflow-history prune eventually
  deletes the input that carried them.
- `PlacementPolicy.selections(facts)` returns `{destination: (rule names, policy order)}`,
  and `desired_destinations` is defined over it.
- Each `dry_run_items` row stores the `rules` that selected its destination. The export adds
  `rule_counts`, which counts every rule in `policy_rules` by distinct content objects and
  shows zero for a rule that selected nothing.

### D8. #13: classified content no rule selects produces an `unselected` item

Settles #13's open question.

- When a run evaluates rules (`active` or `draft`) and `selections(facts)` is empty, the
  evaluator writes one item: `DryRunOutcome.UNSELECTED`, `destination_name=NO_DESTINATION`,
  its facts and paths, and no rules.
- This matches how the evaluator already reports content it cannot place: one
  `unclassified` item with no destination.
- Runs with named destinations never produce it.

**Alternative:** no item, with zero-count rules as the only signal (today's behaviour,
#19's). Rejected: a zero count says a rule matched nothing, but not which content falls
through. A rule that matches some content while its category is misspelled for the rest
shows a non-zero count and hides the gap, and that gap is exactly what a draft review is
for. The cost is one row per unselected object; a policy with no rules (the seed) lists
everything.

### D9. One migration, the next free head, with a single-head assertion

`migrations/versions/0022_dry_run_drafts.py`, with `down_revision="0021_staging_reservations"` (dev took 0021 while the change was in flight):

- adds the six `dry_runs` columns with `ck_dry_runs_policy_source_known`;
- adds `dry_run_items.rules`;
- recreates `dry_run_items`' outcome CHECK with `unselected` added. Read the constraint's
  name from the migrated schema or `ledger/tables.py`'s `enum_check` naming before writing
  the migration.

The downgrade reverses each step. It refuses to run while `unselected` items exist, rather
than deleting them; the error names the `DELETE` to run first.

If #7's baseline lands first, the file becomes `0002_dry_run_drafts.py` on `0001_baseline`.
`tests/integration/test_migrations.py` gains `test_the_chain_has_a_single_head`, asserting
that `ScriptDirectory.get_heads()` returns exactly one revision.

### D10. #2: a verification is bound to the attempt `start` recorded

- `pipeline.recorded_attempt(existing, attempt)` decides the attempt a start records. If the
  placement is COPYING, VERIFYING or PRESENT under an attempt no older than the one asked
  for, the start records `existing.attempts + 1`; otherwise `max(existing.attempts,
  attempt)`. A rewound or forked old attempt therefore gets an identity of its own.
- `start` clears `evidence` in its transaction.
- `set_evidence`, `defer` and `fail` take `attempt=`. Each updates the placement only
  `WHERE attempts = :attempt AND state = 'copying'` and reports whether it did.
- `conclude` acts only on a placement VERIFYING with `attempts == attempt`, checked in its
  own transaction. Otherwise it returns `None` and writes no audit entry.
- `place_content` enqueues `verify_placement(placement.id, placement.attempts)` under
  `verification_workflow_for(placement.id, placement.attempts)`. `placement` is the output of
  the checkpointed `_start_tx` step, so the child ID stays deterministic on replay. If
  evidence or failure was not recorded, the workflow returns `PlaceOutcome.SUPERSEDED` and
  logs `placement.superseded`.
- `verify_placement(placement_id, attempt: int | None = None)` returns `SUPERSEDED` when the
  placement is not VERIFYING that attempt. `attempt=None` keeps verifications enqueued by an
  older build working (checks only VERIFYING), and in-flight rows still deserialize.

### D11. #2: audit details carry their evidence, as flat nullable keys

- `placement_started` gains `source_version_marker`, `destination_provider`,
  `destination_account`, `destination_root` and `policy_version`, built by
  `started_details` and `location_details`.
- The verification conclusion entries gain `attempt` and `source_version_marker`.
- `retention_decided` gains `policy_version`, through
  `RetentionDecision.audit_details(*, policy_version)`.
- The policy version flows from `ActivePolicy.version` through
  `ReconcileContext.policy_version` to `PlacementRequest.policy_version` (optional, default
  `None`, so a request checkpointed by an older build still loads) and into `_record_tx`.
  Unknown values are written as null.
- `details` is flat JSONB; no migration.

### D12. #2: a rewind runbook, and CLAUDE.md corrected

`docs/rewind.md` is #21's runbook minus the sentence about the Conductor gate (Conductor is
removed): where to run a rewind (`DBOS.rewind_workflow_async` from a process with the ledger
datasource registered; never `DBOSClient` or the sync call), listing children before the
rewind, rewinding collapsed children after the parent and never deleting them, the parent
and child table, and placement behaviour under rewind.

CLAUDE.md's DBOS bullets are corrected to match decisions already recorded on #2:

- "so rewind or delete the children too" becomes "so rewind the children after the parent;
  never delete them (`docs/rewind.md`)";
- "record who ran it, from which step and why in the audit trail" becomes "...in the issue
  tracking the incident; the rewind writes no audit entry".

The invariant "every audited decision commits with its entry in one transaction" goes into
the content-ledger spec. A task checks every existing `AuditRepository(...).append` site
against it.

### D13. Streams: a minimal S0, then seven parallel streams with one cross-stream wait

The aim is the shortest critical path. S0 holds only what every later stream compiles
against, plus what must change together to keep the suite green:

- the domain types;
- the condition check wired into policies and templates (the same validator pattern in two
  files; keeping `rendering/templates.py`'s parser in S0 is what removes the S3→S2 wait);
- the field removal, which breaks `reconciliation/retention.py` and every stored-policy read
  unless the rest of #12 lands with it;
- the fake split.

After S0, seven streams start at once, on disjoint files:

| Stream | Issue | Owns (after S0) |
|---|---|---|
| S0 contracts | all | `domain/{conditions.py (new), taxonomy.py, policy.py, placement.py, retention.py, __init__.py}`, `configuration/documents.py`, `placement/policy.py`, `rendering/templates.py` (condition check and `parse_templates(taxonomy=)` only), `tests/unit/domain/{test_conditions.py (new), test_policy.py}`, `tests/unit/rendering/test_templates.py`, `tests/unit/api/dry_run_fakes.py` (new; `FakeDryRuns` moved out of `fakes.py`), `tests/unit/configuration/__init__.py` (new); and, for #12 only, edits to `reconciliation/retention.py`, `tests/unit/reconciliation/test_retention.py`, `tests/integration/test_reconcile_workflow.py` and `tests/integration/test_api_configuration.py`, which pass to later streams. |
| S1 retention | #11 | `reconciliation/{retention.py, convergence.py}`, `tests/unit/reconciliation/test_retention.py`, `tests/integration/test_reconcile_workflow.py` |
| S2 rendering | #14 | `rendering/{templates.py (PROFILES only), planning.py}`, `tests/unit/rendering/{test_templates.py (profile test only), test_planning.py}` |
| S3 configuration API | #14 | `configuration/{store.py, consistency.py (new)}`, `api/configuration.py`, `classification/taxonomy.py`, `tests/unit/api/{fakes.py, test_configuration.py}`, `tests/unit/configuration/test_consistency.py` (new), `tests/unit/classification/test_taxonomy.py`, `tests/integration/{test_api_configuration.py, test_configuration_store.py, test_end_to_end.py}` |
| S4 stored check | #14 | `configuration/verify.py` (new), `tests/unit/configuration/test_verify.py` (new), `docs/recovery.md` |
| S5 dry runs | #13 | `migrations/versions/0022_dry_run_drafts.py` (new), `ledger/tables.py`, `api/dry_runs.py`, `placement/dry_run.py`, `workflows/dry_run.py`, `tests/unit/api/{test_dry_runs.py, dry_run_fakes.py}`, `tests/unit/placement/test_dry_run_rows.py`, `tests/integration/{test_api_dry_runs.py, test_dry_run_workflow.py, test_migrations.py}` |
| S6 placement guards | #2 | `placement/pipeline.py`, `workflows/{placement.py, reconcile.py}`, `tests/unit/placement/{test_recorded_attempt.py, test_audit_details.py}` (new), `tests/integration/{test_placement_workflow.py, test_dbos_runtime.py, test_reconcile_audit.py (new)}` |
| S7 docs | #12, #2 | `README.md`, `docs/rewind.md` (new), `CLAUDE.md` |

S2's `rendering/templates.py` edit is confined to the `PROFILES` mapping, which S0 does not
touch; S2 and S0 are sequential anyway.

There is exactly one cross-stream edge: **S5's draft-validation task waits on S3's
`check_submission` task.** Every other task in every stream depends only on S0. S3 puts
`check_submission` first, so S5 waits as little as possible. The edge is kept rather than
moved into S0 because `check_submission` needs `lock_shared`, `taxonomy_in_force` and the
`RejectedError` mapping, which would pull three more files onto the critical path for one
task's benefit.

Departures from the split proposed with the request, and why:

- **#12 lands entirely in S0, not after #11 in S1.** Removing the field breaks
  `reconciliation/retention.py` and every stored-policy read until `parse_stored_policy`
  and `active_policy`'s switch land with it. S0's edits to S1's files happen before S1
  starts, so nothing is shared concurrently.
- **`RetentionDecision.audit_details(*, policy_version)` lands in S0.** S0 already rewrites
  `audit_details` for #12. This removes the edge where S6 (wiring) would wait on S1
  (keyword).
- **The template condition check lands in S0.** It is the same `ValidationInfo` pattern as
  the policy validators, and it is what the S3 activation task binds under the shared lock.
  With it in S0, S3 depends on nothing but S0.
- **S6 owns all of `workflows/reconcile.py`**, including #17's `check_destinations`
  docstring, so the file has one owner.
- **The `retention_decided` policy-version assertion moves to a new
  `tests/integration/test_reconcile_audit.py`, owned by S6.** It would otherwise sit in S1's
  `test_reconcile_workflow.py` and wait on S6.
- **#14 splits into S2 (profiles and planning), S3 (configuration and API) and S4 (stored
  check).**
- **The domain parts of #14 and #13 move to S0:** `domain/conditions.py`, `check_value`,
  `CapabilityProfileName`, `PolicySource`, `DryRunOutcome.UNSELECTED`, `selections()` and
  `destination_names()`. They live in files only S0 edits.
- **`ledger/tables.py`'s dry-run columns move to S5, with the migration.** The schema-drift
  test needs both in one stream. S0 adds `DryRunOutcome.UNSELECTED`, which changes the
  metadata's derived CHECK, but `compare_metadata` does not compare CHECK constraints, and
  nothing writes `unselected` before S5.
- **README.md, `docs/rewind.md` and CLAUDE.md form S7**, so no code stream holds a doc file.
- **`FakeDryRuns` moves to `tests/unit/api/dry_run_fakes.py` in S0** (a move, no behaviour
  change), so S3 and S5 don't share `fakes.py`.
- **The four S5778 smells** (`test_configuration_store.py:120`, `test_policy.py:188`,
  `test_templates.py:22` and `:52`) are avoided at the source: each `pytest.raises` block
  wraps only the raising call, with arguments built before the block. These fall in S3, S0
  and S0.

**Landing.** If the change is split into PRs, S0 lands first. S1 to S7 then land in any
order, except that S5 lands with or after S3. `tie-placements-to-destination-identity` (#3)
follows the whole change.

## Risks / Trade-offs

- **[A version in force names an unknown profile or `media_kind`, so reads stop after
  deploy.]** → Run the D6 check before rollout. If skipped, the failure is loud (workflow
  errors, a failing GET), and one valid `PUT` recovers.
- **[Dropping a destination turns `release_permitted` back into `retain`.]** → Intended, and
  in the safe direction; audited with `unchecked_copies`.
- **[A rewound `place_content` keeps the workflow ID of its original attempt while recording
  a later attempt.]** A reconciliation pass looking for the latest attempt's workflows can
  mark that attempt stale and start another, costing a second copy. → `docs/rewind.md` says
  to rewind between reconciliation passes; the rewound copy ends `superseded` and registers
  nothing.
- **[The verification lands seconds after `_start_tx` reads `placement.attempts`.]** →
  Every write re-checks attempt and state in its own transaction; a stale read only leads to
  `superseded`.
- **[A dry-run draft is validated against the taxonomy at request time, not at the
  workflow's open.]** → The header records the taxonomy version used; a draft is never
  stored, so a stale check cannot put an invalid version in force.
- **[`unselected` rows enlarge exports for broad policies.]** → One row per object, the same
  as `unclassified`; the outcome counts make the volume visible.
- **[S5 lands without S3.]** → The landing order requires S5 with or after S3; S5's
  draft-validation test cannot pass without `check_submission`.
- **[Downgrading the migration drops draft documents and selecting rules from past runs.]**
  → Dry-run data is review-only and regenerable by running the dry run again.

## Migration Plan

1. Merge the change into `dev` (all streams; D13 gives the PR order if split).
2. Before rollout, from the new image against the target database, run
   `python -m curate_d.configuration.verify` (D6). On exit 1, `PUT` a corrected policy or
   template set with the current build, then re-run the check.
3. `alembic upgrade head` (`make migrate`) applies the dry-run migration.
4. Roll out the workers and API. No promotion is needed for a first deploy of the build.
5. **Rollback.** The old build reads newer stored policies fine: a missing
   `source_counts_as_copy` defaults, and the profile values are plain strings. Rows of the
   newer build stay with it (`docs/recovery.md`, "Rollback and roll-forward"), so the old
   `verify_placement` never receives the new `attempt` argument. Downgrade the migration
   only after deleting `unselected` items (D9).
