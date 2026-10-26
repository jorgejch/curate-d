# Rewinding a workflow

Nothing in curate.d rewinds a workflow. This page is for an operator repairing an incident
by hand with DBOS's `rewind_workflow`.

A rewind (DBOS 3.2) deletes a terminal workflow's checkpoints from `start_step` on, and
re-enqueues it under the same workflow ID, so the re-run executes every step from there.
It never undoes what those steps wrote, and it does not re-run child workflows: a parent
that starts a child under a deterministic ID gets the existing child back, and that child
acted on what the parent decided before the rewind.

## Where to run it

- Call `await DBOS.rewind_workflow_async(workflow_id, start_step=...)` from a process that
  launched DBOS through `runtime/dbos_app.py` with the ledger datasource registered, as a
  worker does. The rewind drops the datasource's checkpoints only for the datasources that
  process registered.
- Give that process an executor identity no worker uses (`CURATE_D_EXECUTOR_ID`). Started
  in a worker pod, it would otherwise take the pod's name (`POD_NAME`), and its launch would
  recover that worker's PENDING workflows while the worker runs them.
- Never `DBOSClient.rewind_workflow`: it passes no datasources, so the ledger datasource's
  checkpoints stay, and the re-run skips the transactions it was meant to repeat.
- The synchronous `DBOS.rewind_workflow` refuses to run while an event loop is running;
  from async code, as below, use `rewind_workflow_async`.
- Only a terminal workflow can be rewound. Cancel a running one first.
- The rewound workflow keeps its application version, so only executors of that version
  run it. Pass `application_version=` to move it onto the version running now, but only
  when that version's code records the same steps, in the same order, before `start_step`:
  those are replayed from their checkpoints.
- By default the workflow is re-enqueued on DBOS's internal queue, outside the transfer
  queues' limits. For `place_content`, pass the queue it ran on and its partition, the
  destination's provider: `queue_name="transfer-small"` (or `"transfer-large"`) and
  `queue_partition_key="pcloud"`.

## Steps

1. Record the children a workflow started from `start_step` on. Its rewind deletes these
   steps, so list them before each rewind, the parent's and every child's below:

   ```python
   async def children(workflow_id: str, start_step: int) -> list[str]:
       steps = await DBOS.list_workflow_steps_async(workflow_id, load_output=False)
       return [
           child
           for step in steps
           if step["function_id"] >= start_step and (child := step["child_workflow_id"]) is not None
       ]
   ```

2. Rewind the parent and wait for it to finish, its `before` list taken first.

3. List the parent's children again. A child in both lists collapsed: the re-run asked for
   it under the same ID and got the old one back. For each of them, record the children it
   started from its first step on, then rewind it from step 1, wait for it, and repeat this
   step for it. Its
   rewind deletes the records of the children it started, so a list taken after it can no
   longer tell a collapsed grandchild from a new one. Each workflow is rewound after its
   parent, and listed just before its own rewind:

   ```python
   async def rewind(workflow_id: str, start_step: int) -> None:
       before = await children(workflow_id, start_step)
       await (await DBOS.rewind_workflow_async(workflow_id, start_step=start_step)).get_result()
       after = await children(workflow_id, start_step)
       for child_id in (c for c in before if c in after):
           await rewind(child_id, start_step=1)


   await rewind(parent_id, start_step)
   ```

   A child only in `before` was not asked for again: the re-run took another path, and what
   that child did stands until someone decides otherwise. A child only in `after` is new
   and ran fresh.

4. Rewind a collapsed child; don't delete it. `DBOS.delete_workflow` removes only the
   system database's rows. The ledger datasource's checkpoints for that ID can survive it,
   and a child created again under the same ID would then skip its transactions.

`tests/integration/test_dbos_runtime.py` pins steps 2 and 3: a rewound parent does not
re-run its child, and rewinding the collapsed child afterwards makes it act on the new
decision.

## Parents and children

Children are enqueued under deterministic IDs and none of curate.d's workflows reads a
child's result.

| Parent | Children |
|---|---|
| `scan_root` | `ingest_source`, the scan's next part |
| `full_rescan` | `ingest_source`, the rescan's next part |
| `ingest_source` | `fingerprint_source` |
| `fingerprint_source` | `ingest_source` (staged content evicted before it was read), `classify_content` |
| `classify_content` | `classify_remote` |
| `release_holds` | `classify_remote` |
| `reclassify_rejected` | `classify_content` |
| `reconcile` | `place_content`, the run's next part |
| `check_destination` | `reconcile` (content a lost copy belonged to) |
| `place_content` | `verify_placement` |
| `verify_placement` | `ingest_source` (a source that changed since fingerprinting) |

The other scheduled workflows that start work (`scan_all`, `full_rescan_all`,
`reconcile_all`, `check_destinations`) only start the parents above.

## Placement

- `place_content` keys its verification on the attempt the start recorded, and passes that
  attempt to it: `verify_placement(placement_id, attempt)`. A rewound failed or untrusted
  attempt records the next attempt when it starts again, and one started over a placement
  still copying, verifying or registered at another locator records the attempt after the
  latest, so its copy gets a verification of its own. There is no collapsed verification
  to rewind. A start that finds its own attempt copying or verifying at another locator
  is a second workflow of that attempt, from overlapping reconciliations: it copies
  nothing and ends `superseded`.
- Rewinding a placement registered at its locator copies nothing: the start finds it
  present and the workflow ends `already_present`.
- A rewound `place_content` keeps the workflow ID of the attempt it originally ran as,
  while the placement records a later attempt. A reconciliation pass looks for the latest
  attempt's workflows, so while the rewound copy runs it can record the attempt as ended
  without concluding and start another. The rewound copy then records no evidence and
  ends `superseded`; the cost is a second copy. Rewind between reconciliation passes.
- An attempt rewound from its copy step, after the placement moved on, records neither
  its evidence nor its failure, and ends `superseded`: evidence, a wait for resources
  and a failure are each recorded only while that attempt is still copying.
- A verification run again after a newer attempt started, or after the placement was
  concluded, ends `superseded` and writes nothing: the new copy discarded the old evidence,
  and the conclusion checks, in its own transaction, that the placement is still verifying
  that attempt's copy.
- A verification enqueued by an earlier build carries no attempt. It concludes the
  placement only if the placement is verifying, whatever its attempt.

## Dry runs

A dry run writes only its own results, so starting a new run is always the cheaper repair.
If a `dry_run` workflow is rewound anyway, rewind it from step 1:

- Its opening transaction re-executes. It reads the policy and template set in force now
  (a draft comes from the request, as before), writes the header again with them, and
  discards every item the earlier execution recorded. Every page is then evaluated under
  that header, and the export never pairs one policy's header with another's items.
- Don't rewind a dry run from a later step. That rewind replays the original header and
  re-evaluates only the pages from that step. The header reads finished until the run
  closes again, items of the re-run pages that the re-run no longer produces stay, and
  locators the earlier execution proposed for later content count as taken.

## The audit trail

- Re-executed steps append the decisions they reach, beside the original entries.
  Entries are never deduplicated by workflow or step.
- The rewind itself writes no audit entry: the trail covers decisions about files. Record
  who ran it, the workflow ID, from which step and why in the issue tracking the incident.
