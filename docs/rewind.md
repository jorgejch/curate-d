# Rewinding a workflow

Nothing in curate.d rewinds a workflow, and the Conductor gate refuses `REWIND_WORKFLOW`.
This page is for an operator repairing an incident by hand with DBOS's `rewind_workflow`.

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
- Never `DBOSClient.rewind_workflow`: it passes no datasources, so the ledger datasource's
  checkpoints stay, and the re-run skips the transactions it was meant to repeat.
- Never the synchronous `DBOS.rewind_workflow`: it refuses an async datasource.
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

1. Record the children the parent started from `start_step` on. The rewind deletes these
   steps, so list them first:

   ```python
   async def children(workflow_id: str, start_step: int) -> list[str]:
       steps = await DBOS.list_workflow_steps_async(workflow_id, load_output=False)
       return [
           child
           for step in steps
           if step["function_id"] >= start_step and (child := step["child_workflow_id"]) is not None
       ]


   before = await children(parent_id, start_step)
   ```

2. Rewind the parent and wait for it to finish:

   ```python
   await (await DBOS.rewind_workflow_async(parent_id, start_step=start_step)).get_result()
   ```

3. List the parent's children again. A child in both lists collapsed: the re-run asked for
   it under the same ID and got the old one back. Rewind each of them from step 1, wait for
   it, and repeat these steps for its own children:

   ```python
   after = await children(parent_id, start_step)
   for child_id in (c for c in before if c in after):
       await (await DBOS.rewind_workflow_async(child_id, start_step=1)).get_result()
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

- `place_content` keys its verification on the attempt the start recorded. A rewound
  failed or untrusted attempt records the next attempt when it starts again, so its copy
  gets a verification of its own. There is no collapsed verification to rewind.
- Rewinding a placement registered at its locator copies nothing: the start finds it
  present and the workflow ends `already_present`.
- A rewound `place_content` keeps the workflow ID of the attempt it originally ran as,
  while the placement records a later attempt. A reconciliation pass looks for the latest
  attempt's workflows, so while the rewound copy runs it can record the attempt as ended
  without concluding and start another. The rewound copy then records no evidence and
  ends `superseded`; the cost is a second copy. Rewind between reconciliation passes.
- A verification run again after a newer attempt started, or after the placement was
  concluded, ends `superseded` and writes nothing: the new copy discarded the old evidence,
  and the conclusion checks, in its own transaction, that the placement is still verifying
  that attempt's copy.

## The audit trail

- Re-executed steps append the decisions they reach, beside the original entries.
  Entries are never deduplicated by workflow or step.
- The rewind itself writes no audit entry: the trail covers decisions about files. Record
  who ran it, the workflow ID, from which step and why in the issue tracking the incident.
