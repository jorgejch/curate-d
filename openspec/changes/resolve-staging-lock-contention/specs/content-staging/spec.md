# Spec Delta

## ADDED Requirements

### Requirement: Reservations are durable
An admitted upload SHALL be recorded as a reservation naming its staging key, its attempt,
its reserved bytes and an expiry, visible to every worker. A reservation SHALL end in
exactly one of three ways: consumed by the registration of its attempt, abandoned when the
reserving block exits without registering, or expired.

#### Scenario: A reservation is consumed by its registration
- **WHEN** a reserved attempt's verified upload is registered
- **THEN** the registration and the reservation's removal commit together, and no worker counts the reservation afterwards

#### Scenario: A failed or refused upload abandons its reservation
- **WHEN** the reserving block exits with an error after admission, or the upload raises
- **THEN** the reservation is removed before the error reaches the caller, and a removal that fails is left for expiry rather than retried under the error

#### Scenario: A cancelled upload abandons its reservation
- **WHEN** the task is cancelled while the upload is in flight
- **THEN** the reservation is removed, any incomplete upload is aborted, no object becomes readable and the cancellation propagates

### Requirement: Reservations expire by the database clock
A reservation's expiry MUST be judged by the database clock alone, never by a worker's
clock, and MUST exceed the transfer deadline so a live upload cannot outlive its
reservation. An expired reservation MUST NOT be counted or consumed.

#### Scenario: A dead worker's reservation expires
- **WHEN** a worker dies after admission and its reservation reaches its expiry
- **THEN** admission on any worker stops counting it, reconciliation collects the attempt's completed object if one landed, and the incomplete-upload backstop collects its parts

#### Scenario: A registration after expiry publishes nothing
- **WHEN** an upload finishes after its reservation expired
- **THEN** nothing is registered, the attempt's bytes are removed or left as an orphan for reconciliation, a `staging.reservation_lost` event names the key, attempt and reserved bytes, and the caller receives a retryable staging failure

## MODIFIED Requirements

### Requirement: Bounded active staging capacity
Admission SHALL reserve the incoming declared maximum before consuming a source stream and
SHALL account for completed remote occupancy and live reservations across workers.
Reservations SHALL be accepted concurrently: a worker's admission MUST NOT wait for another
worker's upload, only for its admission or registration. The system SHALL evict eligible
idle content by age and oldest-first under pressure. An oversized stream, exhausted
capacity, or uncertain occupancy MUST fail explicitly. Residual multipart/orphan storage
SHALL be separately reported and cleaned; active capacity limits MUST NOT be presented as a
hard billing cap.

#### Scenario: Capacity pressure during active download
- **WHEN** admitting another item would exceed the configured bound and remaining candidates have active downloads
- **THEN** admission fails without removing any protected object

#### Scenario: Two workers upload at once
- **WHEN** one worker is uploading an admitted object and a second worker's declaration fits beside that reservation
- **THEN** the second worker is admitted and uploads while the first upload is still in flight

#### Scenario: A reservation counts before its bytes land
- **WHEN** one worker holds a reservation whose bytes have not yet appeared in the listing and a second worker's declaration would exceed the bound together with it
- **THEN** the second worker's admission reclaims or is refused exactly as if the reserved bytes were completed occupancy

#### Scenario: Eviction under pressure commits before bytes go
- **WHEN** admission evicts idle entries to make room
- **THEN** the unregistrations commit with the reservation, the evicted bytes are deleted afterwards, and a deletion failure abandons the reservation and is reported after the commit

### Requirement: Immutable atomic publication
Staged content SHALL be keyed by a validated source-derived identifier and SHALL become
readable only after a complete verified upload is registered. Registration of a reserved
attempt SHALL consume its live reservation in the same transaction, and MUST publish
nothing when that reservation is gone. Repeating a completed stage MUST preserve the
registered bytes. A failed or stale writer MUST NOT replace a published winner.

#### Scenario: Concurrent or failed uploads
- **WHEN** two writers stage one key and one fails or finishes after losing ownership
- **THEN** readers see only the complete registered winner, never partial or overwritten content

#### Scenario: Registration without its reservation
- **WHEN** a writer registers an attempt whose reservation was abandoned or expired
- **THEN** no row is written and the writer receives a retryable failure rather than a published entry

### Requirement: Cleanup without active ingestion
Completed objects SHALL be deleted promptly when no download uses them. Periodic cleanup
SHALL reclaim eligible aged entries even when no new jobs arrive. Reconciliation MUST
spare a completed object whose attempt holds a live reservation and MUST collect it once
the reservation has ended without a registration. Failed cleanup MUST be visible and
retryable. Incomplete multipart uploads and abandoned content SHALL have independently
verified backend-appropriate cleanup backstops (lifecycle or server cleanup plus explicit
abort) without retention tiers or object versions that impose unnecessary minimum-duration
charges.

#### Scenario: Worker dies during transfer
- **WHEN** a worker dies after starting an upload or before publishing completed content
- **THEN** no incomplete object becomes readable, its reservation expires, and residual storage is reclaimed by reconciliation or the verified backend-specific lifecycle/server cleanup

#### Scenario: Reconciliation runs beside an open upload
- **WHEN** another worker's upload has completed its object but not yet registered it, under a live reservation
- **THEN** reconciliation leaves that object alone and the later registration succeeds

### Requirement: Writer-lock holdings are observable
Every fresh acquisition of the staging writer lock SHALL emit one `staging.writer_lock_held`
event after the transaction that owned the lock ends, carrying the caller, the wait to
acquire, the time held and `entry_claims`: the number of distinct entry locks the holding
took, every one held until the holding ended. A reservation SHALL take the lock for its
admission (`occupancy.reserve`), its registration (`occupancy.register`) and, when it
registers nothing, its abandonment (`occupancy.abandon`); no holding SHALL span the upload.
A re-entrant acquisition MUST emit nothing. The event MUST be emitted whether the holding
committed or rolled back.

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

#### Scenario: A reservation's holdings end before its upload
- **WHEN** a publication is admitted, uploads and registers
- **THEN** an `occupancy.reserve` event is emitted before the upload begins and an `occupancy.register` event after it ends, and neither `held_seconds` includes the upload

### Requirement: Reservations report their phases
Every publication reservation SHALL emit exactly one `staging.reservation_phases` event,
however it ended, with the staged key, the declared size, the outcome (`registered`,
`refused`, `cancelled` or `failed`), the error class or null, and seconds for admission,
upload, registration and commit. Admission SHALL be the admission holding, registration
SHALL include the wait for the registration holding, and commit SHALL be that holding's
commit; the four add up to the reservation's lifetime after the lock wait for admission. A
phase that never started MUST be null; one cut short by a failure or cancellation MUST run
to the end of the reservation, rollback included.

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

#### Scenario: A lost reservation
- **WHEN** the upload finishes after its reservation expired
- **THEN** one event is emitted with outcome `failed`, the lost-reservation error's class name, non-null upload and registration phases and a null commit phase

### Requirement: Reader waits are observable and attributable
Every reader hold on a staged entry SHALL emit one `staging.reader_lock_wait` event with the
requested key, the time spent waiting for the shared entry lock alone, and whether the key
was registered. A reader MUST wait, never fail, while a writer holds the entry lock,
whether the writer claimed the reader's key or a key whose entry lock id collides with
it. That wait MUST end with the holding that claimed the lock and MUST NOT span an upload
the holding admitted. Once the writer's transaction ends, the reader MUST receive the
registered winner, or a cache miss if the writer evicted the key.

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

#### Scenario: A reader is not blocked by an admitted upload
- **WHEN** an admission reclaimed an orphan under the reader's key, evicted the reader's key, or claimed a colliding key, and that admission's upload is still in flight
- **THEN** the reader receives its winner, or a miss for the evicted key, without waiting for the upload, and its event's wait is bounded by the admission holding
