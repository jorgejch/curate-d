# Proposal

## Why

A placement is keyed by its destination's **name** alone (#3): `placements` is unique on
`(content_object_id, destination_name)` and `(destination_name, locator)`, and the placement
identifier, the placement workflow identifiers, the advisory locks, reconciliation, the
destination checks, retention, dry runs and the API all key on that name. The destination's
provider, account and root are read from the policy in force, not from the placement. So when
a policy keeps a name but points it at another account or root, every placement recorded under
the name is taken to be at the new place: reconciliation calls copies PRESENT that are not
there, retention counts them, the daily check looks for them in the wrong account and copies
them again, and their locators stay reserved so new content there gets the content-hash suffix
instead of its plain path. Seen in dev on 2026-10-02 with `test-organized` moved between two
pCloud accounts. Nothing releases an original yet; this is a loss path once source release
lands, and #11's retention fix needs the identity this change records.

PR #24 implemented the decisions recorded on #3 (2026-10-11) but forked from a `dev` that is
now far behind: #21, #17 and #19 landed as `harden-policy-and-placement-decisions`, its
migration number is taken, and its branch is gone. This change re-plans the work on today's
`dev`, with #24's diff as the reference implementation.

## What Changes

- **Destination identity.** A destination is `(provider, account, root path)`, with the root
  in one canonical form. A `destinations` table holds one row per identity, keyed by a
  deterministic identifier derived from the three, so placement and workflow identifiers can
  be derived before any row exists. Each placement references its destination identity; the
  policy's name stays on the placement as a label, the latest name it was placed under.
- **Uniqueness per identity.** "One placement per content object per destination" and "one
  content object per locator per destination" hold per identity. Two destinations on one
  provider in different accounts, same root and template, each get every matching file at its
  plain locator.
- **Renamed and redefined destinations.** A name pointed at another account or root is a new
  destination: its old placements are not counted toward it, are not looked for there, and keep
  their real location. A rename that keeps the identity keeps its placements, and the next
  start under the new name relabels them.
- **Policy validation.** A policy, and the named destinations of a dry run, are refused when
  two destinations share an identity, when one root lies inside another's in the same account
  (one file would count as two copies), or when a root does not normalize.
- **Downstream keys.** Placement, content-placement and destination-check workflow identifiers,
  the loss-repair run name, the placement and path advisory locks, convergence's pairing of
  desired and existing placements, `present_at`, `destination_checks` and the dry-run locator
  index all take the identity. Retention's "destinations the policy defines" becomes a set of
  identities, reported by the policy's current name; an unchecked copy is reported with its
  location.
- **Surfaces.** The history view, the failed-units view and the dry-run export carry the
  destination identity and its location beside the name.
- **Migration with backfill.** One migration, `0023_destination_identity`, creates the table,
  moves the constraints, and assigns each existing placement its identity: from the location
  its latest `placement_started` entry recorded, else from the policy version that entry
  names, else from the policy version in force when it was started, else when it was created.
  It aborts, naming the placements, when any cannot be resolved or two would share an
  identity with one content object or one locator; it never guesses a location.
- **BREAKING (deploy).** `Placement` gains a required field and placement workflow identifiers
  change, so in-flight placements from the old build cannot be forked or recognised. The
  migration refuses to run while any placement is pending, copying or verifying; the deploy
  drains first. Documented under "Before deploying" in `docs/recovery.md`.
- **Sources** are already keyed by `(provider, account, native_id)`, connections and rclone
  remotes by `(provider, account)`, and scan identifiers carry the account (re-checked for
  this proposal). Pacers and queue partitions are per provider, a cap shared by its accounts,
  not a single-account assumption. No source change.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `content-ledger`: a destination is identified by provider, account and root path, and the
  name is a label; the two placement uniqueness rules hold per identity; a placement records
  its destination identity; upgrading an existing ledger backfills the identity or aborts.
- `placement-reconciliation`: a redefined name is a new destination and creates new work; a
  rename keeps its placements; a destination check looks only for placements at its identity;
  the verification gate counts copies at the identities the policy defines.
- `placement-execution`: a dry run proposes plain locators at two destinations in different
  accounts; two identities under one name are two placements.
- `path-rendering`: a locator occupied at another destination identity is no collision.
- `control-plane`: a policy or dry run whose destinations share a place is refused with 422;
  history, failed units and dry-run exports carry the destination identity.

## Impact

- **Code:** `domain/{ids.py, placement.py, policy.py, __init__.py}`;
  `ledger/{tables.py, mapping.py, repository.py, __init__.py}`; `runtime/workflow_ids.py`;
  `placement/{pipeline.py, dry_run.py}`; `rendering/planning.py`;
  `reconciliation/{convergence.py, retention.py, store.py}`;
  `workflows/{placement.py, reconcile.py}`; `api/{audit.py, jobs.py, dry_runs.py}`.
  The unused `domain.placement.Destination` model is replaced by `DestinationIdentity`.
- **Schema:** `migrations/versions/0023_destination_identity.py` after `0022_dry_run_drafts`,
  the head since #73 deleted 0017 to 0019 (#7's squash has not landed). New `destinations` table; `placements.destination_id` (FK,
  not null) with both unique constraints rekeyed; `destination_checks` keyed by
  `destination_id` with its rows dropped (latest-only; the next scheduled check refills them);
  `dry_run_items.destination_id` nullable, with the locator index moved onto it.
- **API:** `GET /content/{id}/history` placements gain `destination_id` and `location`;
  `GET /jobs` failed placement units name the location; `GET /dry-runs/{id}` items gain
  `destination_id`. `PUT /configuration/policy` and `POST /dry-runs` gain 422 cases.
- **Workflows:** placement, content-placement and destination-check identifiers change for
  every destination. Deterministic as before; no new workflow, step, queue or schedule. All
  new ledger writes (the `destinations` upsert) run in the existing datasource transactions.
  Nothing departs from the `dbos-python` skill; CLAUDE.md gains no departure.
- **Deploy:** breaking. Drain placements to zero, run the migration (it refuses otherwise),
  then roll out. `docs/recovery.md` "Before deploying" gains the procedure.
- **Tests:** 29 test files reference `destination_name`; the schema drift test, the migration
  round trip, the placement reservation and concurrency tests, and the reconcile and dry-run
  workflow tests gain identity scenarios. Comparisons in the new tests use separately built
  inputs (SonarCloud S5863 failed #24 twice on self-comparisons).
- **Out of scope:** whether placements at destinations no longer in the policy keep being
  checked (#11 follow-up, now possible with the identity); #7's migration squash.
