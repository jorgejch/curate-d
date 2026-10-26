# Design

## Context

Motivation is in proposal.md. Facts that shape the approach, as found on `dev` c2ecbf8
(after #73 removed migrations 0017 to 0019 and spliced `0020_lease_versions` onto 0016):

**Where the name is the key today.**

- `ledger/tables.py`: `placements` unique on `(content_object_id, destination_name)` and
  `(destination_name, locator)`; `destination_checks` primary key `destination_name`;
  `dry_run_items` primary key `(run_id, content_object_id, destination_name)` and locator
  index `(run_id, destination_name, locator)`.
- `domain/ids.py`: `derive_placement_id(content_object_id, destination_name)` is the row's
  primary key. `domain/placement.py`'s `Placement.build` derives it; `pipeline.start` reads
  the row back by that derived id (`get(built.id, for_update=True)`).
- `placement/pipeline.py`: `placement_lock_name(derive_placement_id(...))` and
  `path_lock_name(destination_name, locator)` are the two advisory locks a start takes;
  `reserve` and `rendering/planning.plan_at` look up occupants with
  `find_at_locator(destination_name=...)`. `PlacementRepository.upsert` conflicts on
  `(content_object_id, destination_name)`.
- `runtime/workflow_ids.py`: placement, content-placement and destination-check identifiers
  take the name; `workflows/reconcile.py` names the loss-repair run `loss-{name}-{cycle}`.
- `reconciliation/convergence.py`: `existing` is keyed by `destination_name`, so a redefined
  name inherits the old row as `SATISFIED`. `reconciliation/store.present_at(name)` is what
  the daily check compares the new account's listing against. `decide_retention(...,
  destinations=policy.destination_names())` (landed with #11) counts by name.
- `placement/dry_run.py`: proposals are keyed by name in memory and in `proposed_for`.
- API: `api/audit.py` `PlacementView.destination`, `api/jobs.py` `FailedUnit.where`
  (`name:locator`), `api/dry_runs.py` `ExportedItem.destination`.

**What the audit trail already records.** Since `harden-policy-and-placement-decisions`
(2026-10-21), every `placement_started` entry carries `destination_provider`,
`destination_account`, `destination_root` and `policy_version` (`pipeline.location_details`,
`workflows/placement._start_tx`). Entries before that date carry neither. Dev's ledger was
emptied by the reset script after the incident; prod has not been deployed.

**Pickled models.** `PlacementRequest` (a workflow input) already carries the whole
`DestinationConfig`, so an in-flight request has its identity. `Placement` is a step output
(`_start_tx` returns it) and gains a required `destination_id`: per CLAUDE.md, a required
field on a pickled model is a breaking deploy that must drain first. `DestinationConfig` is
checkpointed too; it gains a normalizing validator, not a new field, and unpickling does not
validate, so old checkpoints load.

**Mixed builds.** `path_lock_name`'s docstring says it is "unchanged across builds, so starts
of mixed builds still exclude each other". After the migration an old build cannot insert a
placement at all (`destination_id` is NOT NULL), so there are no mixed-build starts to
exclude; the drain (D8) is what guarantees that.

**Reference implementation.** PR #24 (closed, branch deleted) implemented the 2026-10-11
decisions against a `dev` 200 commits back. `gh pr diff 24` is readable and is the reference
for every stream below, adapted to the decisions here. Its migration, `0020_destination_
identity`, is the reference for D7, renumbered.

**Schema drift test.** `tests/integration/test_ledger_schema.py::test_migrations_leave_no_
difference_against_the_model_metadata` compares `ledger/tables.py` with the migrated schema,
so the migration and the table changes land in one stream.

**DBOS.** No new workflow, step, queue or schedule. The one new ledger write, the
`destinations` upsert, runs inside `_start_tx`, an existing datasource transaction.
`check_destinations` keeps enqueuing from the workflow body under deterministic identifiers.
The `dbos-python` skill was consulted; nothing departs from it, and CLAUDE.md gains no
departure.

## Goals / Non-Goals

**Goals:**

- Make `(provider, account, root)` the placement's destination key everywhere the name is one
  today, with the name kept as a label.
- A migration that assigns every existing placement its identity from recorded evidence or
  refuses, never guesses.
- A deploy procedure that cannot leave an old build's placement half-keyed.
- An apply split into streams on disjoint files after one foundation stream.

**Non-Goals:**

- Checking destinations the policy dropped (the open half of #11). The identity makes it
  possible; it is not done here.
- Rewriting existing placement identifiers or audit entries. Audit entries refer to the old
  identifiers and are append-only.
- Per-account pacers or queue partitions. Per-provider is a shared cap, not a bug.
- #7's migration squash. This migration is `0023`; if #7 lands first it becomes the first
  revision after the baseline, and nothing else changes.

## Decisions

### D1. Identity: a `destinations` table keyed by a derived identifier

`DestinationIdentity(provider, account, root_path)` with `root_path` in canonical form
(`normalize_root`: absolute; trailing slash dropped except on `/`; no empty, `.` or `..`
segment, else `ValueError`). `derive_destination_id` is `uuid5(uuid5(CURATE_D_NAMESPACE,
"destination"), provider ␟ account ␟ root)`, the same shape as the other identifiers in
`domain/ids.py`. The `destinations` table holds `(id, provider, account, root_path,
created_at)`, unique on the triple, with the provider CHECK the other tables use.

Deterministic rather than a serial, so `derive_placement_id` and every workflow identifier can
be computed before a row exists, which is what lets the reconciler enqueue optimistically
(`runtime/workflow_ids.py` docstring). A table rather than three denormalized columns, so the
identity is one foreign key and one index on `placements`, and the location is written once.

`DestinationRepository.ensure(identity) -> DestinationId` is an `INSERT ... ON CONFLICT DO
NOTHING` on the triple, called by `pipeline.start` before the placement upsert, in the same
transaction. Dry runs never call it (D5). `DestinationRepository.find_many(ids)` serves the
API (D6).

`DestinationConfig` gains the same normalizing validator on `root_path`, `identity` and `id`
properties. The unused `domain.placement.Destination` is replaced by `DestinationIdentity`
(its only use is `tests/unit/domain/test_serialization.py`).

### D2. Placement rows: new identifiers for new rows, old rows keep theirs

`derive_placement_id(content_object_id, destination_id)`. New rows get it. Existing rows keep
the identifier they have, because audit entries carry it and are never rewritten. Therefore
nothing looks a placement up by a derived identifier any more: `start` reads
`PlacementRepository.find(content_object_id, destination_id, for_update=True)`, and when a
row exists the upsert keeps `existing.id`. `upsert` conflicts on `(content_object_id,
destination_id)` and its `set_` includes `destination_name`, so a renamed destination's row
takes the new label on its next start. `get(placement_id)` stays for the verification path,
which holds the row id.

The placement advisory lock stays keyed by the derived identifier,
`placement:{derive_placement_id(content, destination_id)}`: it only has to be the same string
for every start of one placement, which it is whether or not the row's stored id matches.
`path_lock_name` becomes `f"{destination_id}\n{locator}"`; its mixed-build sentence goes
(Context).

Alternative rejected: rewriting old identifiers in the migration. It would orphan every audit
entry's `placement_id` and break "audit entries are never rewritten".

### D3. Redefinition is allowed; sharing a place is refused

As recorded on #3: a name pointed at another account or root is a new destination; a rename
keeps the identity and its placements. `check_destinations(destinations)` in
`domain/policy.py` refuses, with the names concerned: a repeated name (as today); two
destinations with one identity; one root inside another's in the same account
(`DestinationIdentity.contains`: same provider and account, and outer root is `/` or a prefix
at a segment boundary); a root `normalize_root` rejects. `PlacementPolicy._check` calls it,
and so do `DryRunRequest` and `api.dry_runs.DryRunOrder` for named destinations, so a dry
run cannot propose the same file as two copies either.

`PlacementPolicy` gains `destinations_by_id() -> dict[DestinationId, str]` (identity to the
policy's current name) beside `destination_names()`.

### D4. Retention counts identities and reports names people can read

`decide_retention(rule, placements, *, destinations: Mapping[DestinationId, str])`. A present
placement counts as checked when its `destination_id` is a key; `strong_copies` and
`weak_copies` list the policy's **current** name for it (so a renamed destination reads under
its new name). `unchecked_copies` lists `f"{placement.destination_name} ({label})"` with
`label = provider:account:root`, because the one case that produces an unchecked copy under a
name still in the policy is exactly a name moved to another account, and the bare name would
read as a contradiction. The reason text and `audit_details` follow. `convergence.evaluate`
passes `policy.destinations_by_id()` and keys `existing` by `destination_id`; `Target.existing
= existing.get(destination.id)`. Resolving the label needs the placement's location, which
the `Placement` model does not carry: `evaluate` reads `DestinationRepository.find_many` for
the placements' identities not in the policy (usually none), in the same read connection.

Alternative rejected: identities as opaque UUIDs in the decision. A retention reason is read
by a person deciding whether to delete an original.

### D5. Destination checks and dry runs key on the identity

`destination_checks` primary key `destination_id`, `destination_name` kept as the label at
check time; `present_at(destination_id, ...)`, `record_check` conflicts on `destination_id`,
`checks(ids=...)` replaces the name filter. The migration deletes the table's rows (D7): it
holds only each destination's latest check and the next scheduled cycle refills it.
`check_destination` passes the whole `DestinationConfig` into its transactions, enqueues under
`destination_check_workflow_id(destination.id, cycle)`, and names the loss-repair run
`loss-{destination.id}-{cycle}`.

`dry_run_items.destination_id` is nullable (NULL for `UNCLASSIFIED` and `UNSELECTED` items,
which have no destination) with no foreign key, because a dry run writes no `destinations`
row: "no durable placement state". The primary key stays `(run_id, content_object_id,
destination_name)`: within one run, names are unique by D3, whether they come from a policy
or from named destinations. The locator index moves to `(run_id, destination_id, locator)`,
`proposed_for` takes the identity, and the evaluator's in-memory proposals are keyed by it.

### D6. Surfaces

- `api/audit.py`: `PlacementView` gains `destination_id: str` and `location: str`
  (`provider:account:root`); `LedgerAudit.history` reads the placements' identities with
  `DestinationRepository.find_many` in its transaction. Placements stay ordered by name, then
  identifier.
- `api/jobs.py`: the failed-placements query joins `destinations`; `where` becomes
  `f"{name} ({provider}:{account}:{root}):{locator}"`.
- `api/dry_runs.py`: `ExportedItem.destination_id: str | None`.
- Workflow identifiers: `placement_workflow_id(fingerprint, destination_id)`,
  `placement_attempt_workflow_id`, `content_placement_workflow_id` and
  `destination_check_workflow_id` take `DestinationId`; `str(uuid)` holds no `:`.
- `pipeline._audit` adds `destination_id` to every placement entry's details, beside the
  `destination` name it already writes.

### D7. Migration `0023_destination_identity` with evidence-first backfill

`down_revision = "0022_dry_run_drafts"`, the single head (the chain is 0016, 0020, 0021,
0022 since #73; `remove-conductor-ledger-remnants` chose deletion over a reverting 0023, so
the number is free). The identifier derivation, `normalize_root` and
`contains` are frozen copies inside the migration, as #24's were, so the revision's result
never changes with the application. Order:

1. Refuse an active policy the new build would not load (`check_destinations` logic over its
   document), naming the conflicts: activate a corrected policy first.
2. Refuse while any placement is `pending`, `copying` or `verifying`, naming up to five:
   their workflows are keyed by the name and their checkpoints lack the field (D8).
3. Create `destinations`; add nullable `placements.destination_id`.
4. Backfill. For each placement, `started` is the latest `placement_started` entry for its
   `placement_id` (by `occurred_at`), or `created_at`. Resolution, first that applies:
   1. the entry's `destination_provider`, `destination_account`, `destination_root` details;
   2. the policy version the entry's `policy_version` names, by `configurations.version`;
   3. the latest policy version with `created_at <= started`;
   searching earlier versions, latest first, when the version found lacks the name (an
   attempt enqueued just before a policy change). A root that does not normalize, recorded
   or in the version that defines the name, leaves the placement unresolved: an earlier
   version is not where that copy went. Unresolved placements abort the upgrade, naming
   them. Then, before writing, two placements resolving to one identity with one
   content object or one locator abort it, naming them. Insert the identities, update the
   placements.
5. Set NOT NULL, add the foreign key, drop the two name constraints, add the two identity
   constraints. Rekey `destination_checks` after `DELETE`. Add `dry_run_items.destination_id`
   and move the index.

Downgrade refuses while two placements share a name with one content object or one locator,
then restores the name constraints, drops the column and the table, and empties
`destination_checks` again. Existing items keep a NULL `dry_run_items.destination_id`; the
export shows none for them.

Tier 1 did not exist when #24 was written; it is now the common case and needs no policy
history at all.

### D8. Breaking deploy: drain, migrate, roll out

Not blue-green. The old build's placement workflows carry name-keyed identifiers the new
reconciler would not find (it would read them as `STALE`, fail them and start again while the
old copy still runs) and `Placement` checkpoints the new build cannot unpickle. So:

1. Stop new placement work: pause the `reconcile` and `check-destinations` schedules.
2. Let the transfer queues drain; run one reconciliation by hand so rows whose workflow died
   are failed (`decide` returns `STALE` for them).
3. Stop the old workers. Run the migration; step 2 of D7 is the gate.
4. Roll out the new build; unpause the schedules. The first `check-destinations` cycle refills
   `destination_checks`.

Rollback before any new-build placement: downgrade and redeploy the old build. After the new
build has placed content at two identities under one name, the downgrade refuses, and the
only path is forward. `docs/recovery.md` "Before deploying" gains this as the first entry
of a "Breaking deploys" list, and the PR says so.

Alternative rejected: making `destination_id` optional on `Placement` with a default. The
default would have to be a guess at the location, which is the bug being fixed.

### D9. Streams: one foundation stream, then five on disjoint files

S0 holds everything the others compile against and everything that must change with the
schema: domain, ledger, migration and workflow identifiers. It ends with its own unit and
integration tests green; `make typecheck` on the whole tree goes green only when the streams
merge, because S0 changes signatures the streams' files call. Each later stream owns its
callers and their tests, so its tests go green with its own work.

| Stream | Owns |
|---|---|
| S0 foundation (runs alone, first) | `domain/{ids.py, placement.py, policy.py, __init__.py}`, `ledger/{tables.py, mapping.py, repository.py, __init__.py}`, `runtime/workflow_ids.py`, `migrations/versions/0023_destination_identity.py` (new); `tests/unit/domain/{test_identity.py, test_policy.py, test_serialization.py}`, `tests/unit/ledger/{test_mapping.py, test_destination_migration.py (new)}`, `tests/unit/runtime/test_workflow_ids.py`; `tests/integration/{test_migrations.py, test_ledger_schema.py, test_ledger_repository.py}` |
| S1 reconciliation | `reconciliation/{retention.py, convergence.py, store.py}`, `workflows/reconcile.py`; `tests/unit/reconciliation/{test_retention.py, test_convergence.py}`; `tests/integration/test_reconcile_workflow.py` |
| S2 placement | `placement/pipeline.py`, `workflows/placement.py`, `rendering/planning.py`; `tests/unit/placement/{test_attempt_lock.py, test_claim.py, test_recorded_attempt.py, test_audit_details.py}`, `tests/unit/rendering/{test_planning.py, test_render.py}`, `tests/unit/workflows/test_placement.py`; `tests/integration/{test_placement_workflow.py, test_placement_reservation.py, test_placement_concurrency.py}` |
| S3 dry runs | `placement/dry_run.py`, `api/dry_runs.py`; `tests/unit/placement/{test_dry_run_rows.py, test_dry_run_claims.py}`, `tests/unit/api/{test_dry_runs.py, dry_run_fakes.py}`; `tests/integration/{test_dry_run_workflow.py, test_api_dry_runs.py}` |
| S4 API views and policy refusals | `api/{audit.py, jobs.py}`; `tests/unit/api/{test_audit.py, fakes.py}`; `tests/integration/{test_api_audit.py, test_api_jobs.py, test_api_configuration.py}` |
| S5 docs | `docs/recovery.md` |

No stream edits another's files, and no task waits on another stream: S1 to S5 depend on S0
only. `tests/integration/test_end_to_end.py` and `tests/local/pcloud/in_pod.py` are touched
in the final integration group, after the merge, because they exercise several streams.

Model dispatch: planning stays here; each stream is applied by an Opus sub-agent on its own
worktree cut from S0's tip, with the stream's row above and its task group as the brief.

## Risks / Trade-offs

- [A prod or dev ledger with a placement nobody can resolve blocks the upgrade] → By design;
  the migration names them and changes nothing. The operator fails or deletes those rows
  through a recorded decision, then upgrades. Dev's database is recreated from an empty
  state (#73's migration plan) and prod has not been deployed, so this is expected to be
  theoretical.
- [Tier 3 picks the wrong version for an attempt enqueued just before a policy change] → Tier
  1 covers every placement started since 2026-10-21, and the latest-first search of earlier
  versions covers the older case #3 described; a name in no earlier version aborts.
- [A stuck in-flight row blocks the migration gate forever] → The reconciliation run in D8
  step 2 fails any row whose workflow is not running; a row whose workflow is running is
  exactly what the gate is for.
- [Two placements of one content object under one name, old and new account, both show in
  the history] → Intended; the location column is what tells them apart (D6).
- [Nested-root refusal rejects a policy someone relies on] → No stored policy nests roots
  today (dev was reset; prod is empty). The refusal exists because nested roots make one file
  two copies, which retention would count.
- [SonarCloud S5863 on the new tests] → Tests compare identifiers built from separately
  constructed inputs, never an expression against itself.

## Migration Plan

D7 is the migration; D8 the deploy. Verification in the integration group: `make test-stack`,
the migration round trip and backfill cases in `test_migrations.py`, then the targeted
integration files of every stream.

## Open Questions

None that change the specs, approach or tasks.
