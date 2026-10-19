# Spec Delta

## ADDED Requirements

### Requirement: Writer-lock holdings are observable
Every fresh acquisition of the staging writer lock SHALL emit one `staging.writer_lock_held`
event after the transaction that owned the lock ends, carrying the caller, the wait to
acquire, the time held and `entry_claims`: the number of distinct entry locks the holding
took, every one held until the holding ended. A re-entrant acquisition MUST emit nothing.
The event MUST be emitted whether the holding committed or rolled back.

#### Scenario: A fresh writer holding reports its entry claims
- **WHEN** one holding evicts two registered entries and ends
- **THEN** exactly one event is emitted for it, with `entry_claims` of 2

#### Scenario: Claims count locks, not keys
- **WHEN** a holding claims the same key twice, or claims two keys whose entry lock ids collide
- **THEN** its event reports `entry_claims` of 1

#### Scenario: A claim a reader refused is not counted
- **WHEN** a holding tries an entry whose shared lock a reader holds and the try fails
- **THEN** the entry is left alone and its event reports no claim for it

#### Scenario: A holding that fails still reports
- **WHEN** a holding raises after taking entry locks and its transaction rolls back
- **THEN** its event is still emitted with the claims it took and the time it held the lock

### Requirement: Reservations report their phases
Every publication reservation SHALL emit exactly one `staging.reservation_phases` event,
however it ended, with the staged key, the declared size, the outcome (`registered`,
`refused`, `cancelled` or `failed`), the error class or null, and seconds for admission,
upload, registration and commit. A phase that never started MUST be null; one cut short by
a failure or cancellation MUST run to the end of the reservation, rollback included.

#### Scenario: A publication reports its phases
- **WHEN** a publication is admitted, uploads, registers and commits
- **THEN** one event is emitted with outcome `registered`, a null error and a non-null value for all four phases

#### Scenario: A failed upload
- **WHEN** the upload raises after admission
- **THEN** one event is emitted with outcome `failed`, the error's class name, a non-null upload phase and null registration and commit phases

#### Scenario: A refused reservation
- **WHEN** admission refuses the declared size before the upload begins
- **THEN** one event is emitted with outcome `refused`, null admission timing, null reclamation counts and null upload, registration and commit phases, and the refusal propagates to the caller unchanged

#### Scenario: A cancelled upload
- **WHEN** the task is cancelled while the upload is in flight
- **THEN** one event is emitted with outcome `cancelled` and the cancellation propagates

#### Scenario: A reclaiming admission reports what it reclaimed
- **WHEN** admission evicts idle entries or deletes orphans to admit the reservation
- **THEN** the event carries the counts of evicted keys, protected keys and deleted orphan objects, and the admission time includes the reclamation

### Requirement: Reader waits are observable and attributable
Every reader hold on a staged entry SHALL emit one `staging.reader_lock_wait` event with the
requested key, the time spent waiting for the shared entry lock alone, and whether the key
was registered. A reader MUST wait, never fail, while a writer holds the entry lock,
whether the writer claimed the reader's key or a key whose entry lock id collides with
it. Once the writer's transaction ends, the reader MUST receive the registered winner, or
a cache miss if the writer evicted the key.

#### Scenario: A reader behind a reclaiming writer reports its wait
- **WHEN** a writer deletes an orphan attempt under a registered key inside an open holding and a reader asks for that key
- **THEN** the reader blocks until the holding ends, then receives the winner, and its event reports `registered: true` with a wait that spans the block

#### Scenario: A reader behind a writer that claimed only a colliding key
- **WHEN** a writer's open holding claimed a different key whose 4-byte entry lock id equals the reader's key's
- **THEN** the reader stalls until the holding ends, then receives its own registered winner, and its event reports `registered: true`

#### Scenario: A reader's shared lock makes a colliding writer skip
- **WHEN** a reader holds a key's entry lock and a writer tries to reclaim a colliding key
- **THEN** the writer's try fails and it skips the entry without error, and a later pass reclaims it once the reader has released

#### Scenario: An unblocked reader of a missing key reports a miss
- **WHEN** no writer holds the entry lock and the key has no registration
- **THEN** the reader receives nothing and its event reports `registered: false`
