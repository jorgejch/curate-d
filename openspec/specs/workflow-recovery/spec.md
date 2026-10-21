# workflow-recovery Specification

## Purpose

Recovers workflows that no live worker will run: rows pinned to a retired application version, and rows owned by an executor identity that never returns. Recovery runs on a schedule and on an operator's command, and never touches a version that some process still runs. Covers the version lease and registration that make a live version detectable, and the explicit promotion a rollback needs.

## Requirements

### Requirement: Workers register their version before launch

Every process that runs workflows SHALL record its application version in the ledger before it launches DBOS. The record SHALL hold the version and the time it was first recorded, taken from the database clock. Recording an already-recorded version SHALL leave its first-recorded time unchanged. If the version cannot be recorded, the process SHALL NOT launch DBOS and SHALL exit non-zero.

#### Scenario: First start of a version
- **WHEN** a worker of a version never recorded before starts
- **THEN** the version is recorded with the database's current time before DBOS launches

#### Scenario: Restart of a recorded version
- **WHEN** a worker of a version already recorded starts
- **THEN** the version's first-recorded time is unchanged

#### Scenario: Registration fails
- **WHEN** a worker cannot record its version because the database refuses or is unreachable
- **THEN** DBOS is never launched and the worker exits non-zero

### Requirement: Only processes that run workflows register

A process that runs no workflows (the control plane, the operator sweep, the promotion command) SHALL NOT record a version, including when it fails.

#### Scenario: Non-workflow processes
- **WHEN** the control plane, the operator sweep or the promotion command runs
- **THEN** no version is recorded

### Requirement: Workers hold a shared version lease before launch

After registering and before launching DBOS, a worker SHALL hold a shared session-level advisory lock on its version, on the same database session as its executor lease. The worker SHALL count its lease as acquired only when it holds both locks. If the version lock stays unavailable for the whole acquire budget, the worker SHALL exit with the lease-unavailable error without launching DBOS.

#### Scenario: Both locks taken
- **WHEN** a worker starts and no other process holds its executor identity or its version exclusively
- **THEN** it holds its executor lock and a shared lock on its version before DBOS launches

#### Scenario: Version held exclusively by a sweep
- **WHEN** a worker starts while a sweep holds that version's lock exclusively
- **THEN** the worker waits, within its acquire budget, until the sweep releases the lock, and then launches

#### Scenario: Acquire budget exhausted
- **WHEN** the version lock stays unavailable for the whole acquire budget
- **THEN** the worker exits with the lease-unavailable error and DBOS is never launched

### Requirement: Workers keep the version lease until they exit

A worker SHALL keep its executor and version locks until the process exits, including when shutdown leaves workflows running and the executor lease is retained. Losing either lock SHALL be treated as losing the lease: the worker stops at once without draining, as it does on executor-lease loss today.

#### Scenario: Session lost while serving
- **WHEN** the lease session's backend is terminated while the worker serves
- **THEN** both locks are freed, the worker's probe reports the lease lost, and the worker stops without draining

#### Scenario: Undrained shutdown
- **WHEN** a worker shuts down with workflows still running
- **THEN** it keeps both locks until the process exits

### Requirement: Version lock keys stand apart from other locks

The version lock's key SHALL be derived from the application's base name and the version under its own versioned key domain, so that it collides with no other advisory lock except by a 64-bit hash collision. A dead-executor check made by the sweep SHALL take only the executor lock, never a version lock.

#### Scenario: Same string as executor and version
- **WHEN** an executor identity and a version are the same string
- **THEN** their lock keys differ

#### Scenario: Dead-executor check
- **WHEN** the sweep checks whether an executor is alive
- **THEN** it takes, probes and releases only that executor's lock

### Requirement: Non-workflow processes hold a standalone version lease

A process that runs no workflows but acts on a version's behalf (the operator sweep) SHALL hold a shared lock on that version on a dedicated connection, with the same connection keepalives, the same 600 s acquire budget and the same monitoring as the executor lease. Once the monitor detects that the lock is lost, the process SHALL make no further write and SHALL exit non-zero. It SHALL NOT record the version in the ledger.

#### Scenario: Lock lost mid-run
- **WHEN** the standalone lease's session is lost while the operator sweep runs
- **THEN** the sweep makes no write after the loss is detected and the process exits non-zero

#### Scenario: Lock unavailable
- **WHEN** the standalone lease cannot be acquired within its budget
- **THEN** the process exits non-zero having written nothing

### Requirement: The sweep targets DBOS's latest version

A sweep SHALL take DBOS's latest application version, read when the sweep starts, as its target, and SHALL plan every recovery against it. If the latest version cannot be read, the sweep SHALL write nothing and fail.

#### Scenario: Sweeper on the target
- **WHEN** a sweep runs on DBOS's latest version and a row is pinned to an older, eligible version
- **THEN** the row is forked onto the latest version

#### Scenario: Latest version unreadable
- **WHEN** reading DBOS's latest version fails
- **THEN** the sweep writes nothing and fails with the database error

### Requirement: A sweeper off the target forks nothing

A sweep running on a version other than the target SHALL fork and retire nothing, counting those rows as skipped and logging `recovery_sweep.skipped`. It SHALL still re-enqueue rows of dead executors, which keep their version.

#### Scenario: Sweeper not on the target
- **WHEN** a sweep runs on a version that is not DBOS's latest
- **THEN** it forks and retires nothing, counts those rows as skipped, logs `recovery_sweep.skipped`, and still re-enqueues dead executors' rows

### Requirement: A target change mid-sweep stops forking

After taking a source version's exclusive lock, the sweep SHALL read the latest version again. If it is no longer the target, the sweep SHALL release the lock, fork or retire nothing more, and count the remaining fork and retire rows as skipped.

#### Scenario: Latest changes mid-sweep
- **WHEN** DBOS's latest version changes after the sweep started and before it forks a source version's rows
- **THEN** the sweep releases that version's lock, forks and retires nothing more, and counts the rest as skipped

### Requirement: Fork and retire run under the promotion fence

Before forking or retiring any row, the sweep SHALL try once, without waiting, to hold the promotion fence shared, and SHALL hold it until its last fork or retirement. Holding it, the sweep SHALL read the latest version again and stop as for a target change if it moved. If a promotion holds or awaits the fence, the sweep SHALL fork and retire nothing that tick.

#### Scenario: Promotion in progress
- **WHEN** a sweep has fork or retire rows and a promotion holds the fence
- **THEN** the sweep forks and retires nothing, counts those rows as skipped, logs `recovery_sweep.skipped`, and still re-enqueues dead executors' rows

#### Scenario: Latest moved before the fence
- **WHEN** a promotion lands between the sweep's first read of the latest version and its taking the fence
- **THEN** the sweep forks and retires nothing and counts those rows as skipped

#### Scenario: Rollback during a batch
- **WHEN** an operator promotes an older version while a sweep is forking onto the newer one
- **THEN** the promotion waits until the sweep releases the fence, so no row is forked onto the replaced version after the promotion

### Requirement: Only versions registered before the target are recovered

The sweep SHALL fork or retire a non-target version's rows only if that version is recorded in the ledger and was first recorded before the target. If the registrations cannot be read, the sweep SHALL fork and retire nothing and fail.

#### Scenario: Older registered version
- **WHEN** a row is pinned to a version first recorded before the target, and no process holds that version's lock
- **THEN** the row is forked onto the target

#### Scenario: Registrations unreadable
- **WHEN** reading the version registrations fails
- **THEN** no row is forked or retired and the sweep fails with the database error

### Requirement: Unregistered versions are left alone

A version never recorded in the ledger SHALL be left alone, logged as `recovery_sweep.unregistered_version` and counted as unregistered.

#### Scenario: Unregistered version
- **WHEN** a row is pinned to a version that was never recorded, such as a build that predates the version lease
- **THEN** the row is left untouched, logged as `recovery_sweep.unregistered_version` and counted as unregistered

### Requirement: Versions newer than the target are left alone

A version first recorded after the target, such as the newer build after a rollback promotion, SHALL be left alone, logged as `recovery_sweep.newer_than_target` and counted.

#### Scenario: Version newer than the target
- **WHEN** a row is pinned to a version first recorded after the target
- **THEN** the row is left untouched, logged as `recovery_sweep.newer_than_target` and counted

### Requirement: Fork and retire run under the source version's exclusive lock

The sweep SHALL fork or retire a non-target version's rows only while it holds that version's lock exclusively, on a dedicated connection, and SHALL hold the lock until that version's rows are done. It SHALL release the lock on every exit path, including a failed fork. Retiring a row of the target version, or of no version, SHALL take no version lock.

#### Scenario: Version done
- **WHEN** the sweep has handled every row of a source version
- **THEN** it releases that version's lock

#### Scenario: Target-version retirement
- **WHEN** a forked-from original pinned to the target version is still active
- **THEN** it is cancelled without any version lock

### Requirement: A live version's rows are left alone

If any process holds a source version's lock, shared or exclusive, the sweep SHALL leave that version's rows alone, count them as version-live (apart from rows of live executors), and log `recovery_sweep.version_live` with the version, the row count and the mode held.

#### Scenario: Version live in a worker
- **WHEN** a worker of a non-target version holds its shared lock
- **THEN** that version's rows are left, counted as version-live, and logged with mode `shared`

#### Scenario: Version held by another sweep
- **WHEN** another sweep holds a version's lock exclusively
- **THEN** this sweep leaves that version's rows, counts them as version-live, and logs mode `exclusive`

### Requirement: A version-lock error forks nothing

If taking a source version's lock raises an error, the sweep SHALL report that version's rows as failed, fork and retire none of them, and continue with the other versions.

#### Scenario: Lock error
- **WHEN** taking a version's lock raises a database error
- **THEN** that version's rows are reported as failed and nothing of that version is forked

### Requirement: A fork is confirmed before its original is cancelled

A fork SHALL be confirmed under its deterministic successor identifier before the original is cancelled. A fork that fails SHALL leave the original uncancelled, SHALL be reported as failed and logged as `recovery_sweep.fork_failed`, and SHALL NOT stop the rest of the sweep. After cancelling an original, the sweep SHALL read it back; an original that does not read CANCELLED SHALL be reported as failed and logged as `recovery_sweep.lineage_unverified`.

#### Scenario: Fork fails
- **WHEN** forking one row raises
- **THEN** the original stays uncancelled, the row is reported as failed and logged as `recovery_sweep.fork_failed`, the version's lock is released, and the sweep continues with the other rows

#### Scenario: Crash between fork and cancel
- **WHEN** a sweep committed a successor and crashed before cancelling the original
- **THEN** a later sweep confirms the existing successor, cancels the original, and creates no second successor

#### Scenario: Cancel not visible
- **WHEN** an original does not read CANCELLED after its cancel
- **THEN** the row is reported as failed and logged as `recovery_sweep.lineage_unverified`

### Requirement: Recovered rows keep a named, non-internal queue

The sweep SHALL keep a re-enqueued row on the queue it names, and SHALL fork a row onto the queue and partition it names. The sweep SHALL refuse to re-enqueue or fork a row whose queue is NULL or is not one of curate.d's declared queues (which excludes DBOS's internal queue): it SHALL report the row as failed, log `recovery_sweep.queue_refused`, and leave the row untouched. Retiring such a row SHALL still cancel it.

#### Scenario: Re-enqueued row keeps its queue
- **WHEN** a dead executor's row on queue `stage` is re-enqueued
- **THEN** the row is ENQUEUED on `stage`

#### Scenario: NULL-queue row
- **WHEN** a row needing a fork or re-enqueue has no queue
- **THEN** it is reported as failed and left untouched

#### Scenario: Undeclared-queue row
- **WHEN** a row needing a fork or re-enqueue names a queue no curate.d worker dequeues, DBOS's internal queue included
- **THEN** it is reported as failed and left untouched

#### Scenario: Retiring a NULL-queue row
- **WHEN** a forked-from original with no queue is retired
- **THEN** it is cancelled

### Requirement: The sweep's own stale ticks are retired, not recovered

A stranded row of the sweep's own schedule SHALL never be forked or re-enqueued. Under the same gate as a fork (target, registration, lock), it SHALL be cancelled, logged as `recovery_sweep.retired_tick`, and counted as a retired tick. The next tick does its work.

#### Scenario: Stale tick of a retired version
- **WHEN** a recovery tick is stranded on an older, registered version whose lock the sweep takes
- **THEN** the tick is cancelled and counted as a retired tick, and no successor is created

#### Scenario: Stale tick of a dead executor
- **WHEN** a recovery tick is PENDING under a dead executor of the target version
- **THEN** it is cancelled and counted as a retired tick, not re-enqueued

### Requirement: Some rows are deliberately left alone

The sweep SHALL NOT act on rows DBOS has marked as having exceeded their recovery attempts, and SHALL NOT fork DELAYED rows. DELAYED rows SHALL be handled by a later tick once they become ENQUEUED. Rows of a live executor SHALL be left alone and counted as left running. A scheduled sweep's own tick is one of these, so every scheduled run SHALL report at least one row left running.

#### Scenario: Recovery attempts exceeded
- **WHEN** a row is marked as having exceeded its recovery attempts
- **THEN** the sweep leaves it for an operator

#### Scenario: Delayed row
- **WHEN** a row pinned to a retired version is DELAYED
- **THEN** the sweep leaves it until a later tick finds it ENQUEUED

#### Scenario: Live executor
- **WHEN** a PENDING row's executor holds its lease
- **THEN** the row is left alone and counted as left running

### Requirement: One bad row never aborts a sweep

A failure recovering one row SHALL be reported for that row, with the operation and the reason, and SHALL NOT stop the sweep from handling the remaining rows. An unknown executor liveness SHALL never be treated as dead. If listing the active workflows fails, the sweep SHALL write nothing and fail.

#### Scenario: Liveness check fails
- **WHEN** checking an executor's lease raises an error
- **THEN** that executor's rows are reported as failed, none is re-enqueued, and the sweep continues

#### Scenario: Re-enqueue fails
- **WHEN** re-enqueueing one row raises
- **THEN** that row is reported as failed and the sweep continues with the others

#### Scenario: Listing fails
- **WHEN** listing active workflows fails
- **THEN** the sweep writes nothing and fails with the database error

### Requirement: The sweep runs on a persisted schedule

The I/O profile SHALL declare a persisted schedule named `recovery-sweep` that runs the sweep on the `maintenance` queue; the CPU profile SHALL declare none. Its cron SHALL come from the `recovery_sweep_schedule` setting, default `*/10 * * * *`. Each run SHALL record the sweep's outcome as its step output, made of plain values. A run that fails SHALL end the tick in error, and the next tick SHALL run normally.

#### Scenario: Default schedule
- **WHEN** an I/O worker starts with `recovery_sweep_schedule` unset
- **THEN** a `recovery-sweep` schedule with cron `*/10 * * * *` on queue `maintenance` is applied

#### Scenario: CPU profile
- **WHEN** a CPU worker starts
- **THEN** it declares no recovery schedule

#### Scenario: Tick completes
- **WHEN** a recovery tick runs on an I/O worker
- **THEN** its step output reads back as the sweep's outcome

#### Scenario: Tick fails
- **WHEN** a tick's sweep fails with a database error
- **THEN** the tick ends in error, and the next tick runs

### Requirement: An invalid recovery cron fails at settings load

An invalid `recovery_sweep_schedule` SHALL fail settings validation with an error naming the setting, so a worker with one exits before taking its lease or launching DBOS.

#### Scenario: Invalid cron
- **WHEN** `recovery_sweep_schedule` is not a valid cron expression
- **THEN** loading settings fails with a validation error naming the setting, and no lease is taken and DBOS is not launched

### Requirement: Recovery writes no audit entries

The sweep, scheduled or run by an operator, SHALL write no audit entries, whether it succeeds or fails.

#### Scenario: No audit entries
- **WHEN** a sweep forks, retires or re-enqueues rows
- **THEN** no audit entry is written

### Requirement: Rollback promotion is an explicit operator command

The system SHALL provide `python -m curate_d.runtime.promote <version>`, which makes a version DBOS's latest without launching DBOS, and logs the previous latest version. Promoting the current latest version SHALL succeed and change nothing else. On every refusal it SHALL exit non-zero having written nothing.

#### Scenario: Rollback
- **WHEN** an operator promotes an older registered version
- **THEN** it becomes DBOS's latest, the previous latest is logged, and the command exits 0

#### Scenario: Already latest
- **WHEN** the requested version is already the latest
- **THEN** the command exits 0

### Requirement: Promotion refuses unknown versions and clock skew

The promotion command SHALL refuse a version absent from DBOS's version list for this application, and SHALL exit non-zero for a version that belongs to another application sharing the database. It SHALL refuse when the local clock differs from the database clock by more than 1 s.

#### Scenario: Unknown version
- **WHEN** the requested version is absent from DBOS's version list
- **THEN** the command exits non-zero and nothing is written

#### Scenario: Another application's version
- **WHEN** the requested version belongs to another application sharing the database
- **THEN** the command exits non-zero

#### Scenario: Clock skew
- **WHEN** the local clock and the database clock differ by more than 1 s
- **THEN** the command exits non-zero and nothing is written

### Requirement: Promotion waits for a running sweep's forks

The promotion command SHALL change the latest version only while it holds the promotion fence exclusively, waiting at most `--fence-timeout` seconds (default 120) for sweeps holding it. On timeout it SHALL exit 3 having written nothing.

#### Scenario: Sweep mid-batch past the timeout
- **WHEN** a sweep holds the fence for longer than the promotion's timeout
- **THEN** the command exits 3 and the latest version is unchanged

#### Scenario: No sweep running
- **WHEN** no sweep holds the fence
- **THEN** the command promotes the version under the fence and releases it

### Requirement: Promotion is verified by read-back

After promoting, the command SHALL read the latest version back and exit non-zero unless the version read back is the one requested.

#### Scenario: Read-back mismatch
- **WHEN** the latest version read back after promoting is not the requested one
- **THEN** the command exits non-zero

### Requirement: Workers never promote a version

No worker SHALL change DBOS's latest application version, at startup or at any other time. No background process SHALL do so either.

#### Scenario: Restarting an older build
- **WHEN** workers of a build that is registered but not latest start
- **THEN** DBOS's latest version is unchanged and the workers leave schedules to the latest version

### Requirement: The operator sweep recovers without launching DBOS

The system SHALL provide `python -m curate_d.runtime.operator_sweep --target-version <v>`, which runs the same gated sweep through DBOS's client. It SHALL NOT construct or launch DBOS, run startup recovery, start queue or scheduler threads, or record a version. It SHALL hold the target's standalone shared version lease for the run. It SHALL be safe to run alongside live workers and a scheduled sweep.

#### Scenario: Recovery after scale-down
- **WHEN** an operator runs it with the matching target after a pod was removed with PENDING rows
- **THEN** those rows are recovered, forked rows under their deterministic successor identifiers

#### Scenario: Live executor untouched
- **WHEN** it runs while an I/O worker holds its executor lease
- **THEN** that executor's PENDING rows are left untouched

### Requirement: The operator sweep refuses a wrong target

The operator sweep SHALL exit 2, having written nothing, unless `--target-version` is given and equals both DBOS's latest version and the running build's own version. It SHALL refuse a target that is not recorded in the ledger, exiting non-zero having written nothing.

#### Scenario: Missing or mismatched target
- **WHEN** `--target-version` is missing, is not DBOS's latest, or is not the running build's version
- **THEN** the command exits 2 and workflow rows, application versions and version registrations are unchanged

#### Scenario: Unregistered target
- **WHEN** the target version is not recorded in the ledger
- **THEN** the command refuses, exits non-zero, and writes nothing

### Requirement: The operator sweep reports its outcome

The operator sweep SHALL print every field of the sweep's outcome. It SHALL exit 1 when any row is reported as failed, and 0 otherwise.

#### Scenario: Outcome printed
- **WHEN** a run completes
- **THEN** every outcome field is printed

#### Scenario: Failures reported
- **WHEN** any row is reported as failed
- **THEN** the command exits 1

### Requirement: Re-enqueue is gated and verified

The sweep SHALL re-enqueue a dead executor's row through DBOS's public resume, only after re-reading, immediately before the resume, that the row is still PENDING under that executor, always onto the row's own queue. After the resume it SHALL read the row back: ENQUEUED counts as re-enqueued; PENDING under another executor counts as left running; anything else SHALL be reported as failed and logged as `recovery_sweep.redispatch_unverified`.

#### Scenario: Row changed after listing
- **WHEN** the re-read shows the row no longer PENDING under the dead executor
- **THEN** nothing is resumed and the row is not counted as re-enqueued

#### Scenario: Row cancelled after the counter write
- **WHEN** the row no longer reads PENDING under the dead executor right before the resume
- **THEN** nothing is resumed and the row is not counted as re-enqueued

#### Scenario: Resume does not read back
- **WHEN** the row reads back neither ENQUEUED nor PENDING under another executor after the resume
- **THEN** the row is reported as failed and `recovery_sweep.redispatch_unverified` is logged

### Requirement: Re-enqueue is capped per application version

Because a resume resets DBOS's recovery-attempt count, the sweep SHALL count its own redispatches of a row in the row's workflow attributes, per application version, and SHALL refuse a row at `MAX_SWEEP_REDISPATCHES` (10), reporting it as failed and logging `recovery_sweep.redispatch_cap`. A count recorded under another version SHALL start at zero.

#### Scenario: Row at the cap
- **WHEN** a row's recorded redispatch count under its version is at the cap
- **THEN** it is not resumed, is reported as failed and logged as `recovery_sweep.redispatch_cap`

#### Scenario: New version resets the count
- **WHEN** a row's recorded count was written under a different application version
- **THEN** the count starts at zero and the row is resumed

### Requirement: A lost lock stops the batch

Before each fork the sweep SHALL check, on the lock sessions themselves, that it still holds the promotion fence and the source version's lock. If either is lost it SHALL report the row as failed, log `recovery_sweep.version_lock_lost`, and skip every remaining fork and retirement of that sweep. A lock lost between a fork and its cancel SHALL still cancel the original, then stop the batch the same way.

#### Scenario: Fence lost before a fork
- **WHEN** the promotion fence is no longer held when a fork is about to run
- **THEN** nothing more is forked, the row is reported as failed and `recovery_sweep.version_lock_lost` is logged

#### Scenario: Version lock lost after a fork
- **WHEN** the version lock is lost after a successor was created and before the original was cancelled
- **THEN** the original is still cancelled, the row is reported as failed, and the remaining rows are skipped

### Requirement: A lost lock before a cancel retires only a confirmed successor's original

Before the cancel of an original whose successor is already confirmed, and before cancelling a stale tick of its own schedule, the sweep SHALL check both locks the same way. Lost before such a cancel, it SHALL still cancel an original whose successor is confirmed, SHALL NOT cancel a stale tick, SHALL report the row as failed, log `recovery_sweep.version_lock_lost` and skip the rest of the batch.

#### Scenario: Version lock lost before retiring a committed successor's original
- **WHEN** the version lock is lost and the row's deterministic successor already exists
- **THEN** the original is still cancelled, the row is reported as failed, `recovery_sweep.version_lock_lost` is logged and the remaining rows are skipped

#### Scenario: Lock lost before cancelling a stale tick
- **WHEN** either lock is lost right before the sweep cancels a stale tick of its own schedule
- **THEN** the tick is not cancelled, the row is reported as failed, `recovery_sweep.version_lock_lost` is logged and the remaining rows are skipped

### Requirement: Schedule reconciliation is serialized and verified

Workers SHALL reconcile persisted schedules under a session advisory lock held on a dedicated connection, through DBOS's public schedule API, only when their version is DBOS's latest. After writing, a reconciliation SHALL verify that it still holds the lock and that the latest version has not changed; if either fails it SHALL log `worker.schedule_reconciliation_unverified` and SHALL NOT fail the worker.

#### Scenario: Lock lost mid-write
- **WHEN** a worker's authority session is terminated while it writes schedules
- **THEN** its writes are reported as unverified and the latest version's next reconciliation restores the rows

#### Scenario: Builds exclude each other
- **WHEN** a build holding the authority as a transaction lock and one holding it as a session lock reconcile at once
- **THEN** one waits for the other

### Requirement: Schedule reconciliation repeats and repairs

An I/O worker SHALL re-run the reconciliation every `schedule_reconciliation_interval_seconds` (default 600) from a worker thread until it stops, so a stale write is repaired within one interval and a promoted version's schedules apply without a restart.

#### Scenario: Promotion without restart
- **WHEN** a version is promoted while its workers run
- **THEN** those workers apply their schedules within one reconciliation interval

### Requirement: Schedule declarations are the only source of truth

The declarations in code and the settings they read are the only source of a schedule's cron, queue and context: a hand edit to a schedule row SHALL be reverted by the next reconciliation. A paused schedule SHALL stay paused across reconciliations, so pause and resume remain the operator's kill switch.

#### Scenario: Hand-edited schedule row
- **WHEN** an operator changes a schedule's cron or queue directly in the database
- **THEN** the next reconciliation restores the declared values, while a paused status is kept

### Requirement: Workflow history pruning is application-owned

Workflow-history pruning SHALL follow only `workflow_retention_days`, run by `runtime/retention.py` on `workflow_retention_schedule`, through DBOS's public workflow listing and deletion, in batches, for this application's rows.

#### Scenario: Aged workflow
- **WHEN** a terminal workflow completed before the retention cutoff
- **THEN** the prune deletes it and its checkpoints on the next tick

#### Scenario: Live workflow's checkpoints
- **WHEN** a datasource checkpoint belongs to a workflow still present
- **THEN** the prune leaves it in place

#### Scenario: Another application's live workflow
- **WHEN** a datasource checkpoint older than the cutoff belongs to a workflow owned by another application in the same system database
- **THEN** the prune leaves it in place and does not prune the workflow record

#### Scenario: Workflow revived mid-prune
- **WHEN** a listed row no longer reads terminal and completed before the cutoff when the prune re-reads its batch before the delete
- **THEN** the prune leaves it and logs `workflow_history.revived_during_prune`
