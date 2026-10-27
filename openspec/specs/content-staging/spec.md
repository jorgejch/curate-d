# content-staging Specification

## Purpose

Provide recoverable temporary media storage between I/O and CPU workers while preserving immutable reads, bounded active occupancy, and separation from source-provider authority.

## Requirements

### Requirement: Persistent environment-isolated staging storage
Local development SHALL use a pinned self-hosted S3 engine through the same application S3
backend used for private OVH Standard S3 staging storage in the Kubernetes dev and prod
environments. Filesystem staging MUST NOT be the normal development path or substitute for
S3 integration testing. Runtime workers MUST NOT create or delete buckets. A missing or
inaccessible bucket MUST fail readiness explicitly.

#### Scenario: Empty staging
- **WHEN** all staged content has been released
- **THEN** the bucket remains provisioned and contains no retained completed media or unnecessary versions

#### Scenario: Missing bucket
- **WHEN** a worker cannot access its configured staging bucket
- **THEN** staging fails explicitly without creating a bucket or fetching from a different provider

#### Scenario: Local engine substitution
- **WHEN** the local S3 engine is replaced with a different implementation
- **THEN** the application adapter, request construction, integrity checks and failure handling are unchanged, and any change forced in application code is reported as a compatibility finding rather than absorbed

#### Scenario: Local engine is no longer maintained upstream
- **WHEN** the pinned local engine's upstream repository is archived or otherwise unmaintained
- **THEN** it is replaced by an actively maintained engine that meets the same compatibility bar, and it is kept only as the interim local engine until that replacement passes

### Requirement: Reader authority limited to staged media
CPU workers SHALL read staged objects using an environment-specific reader identity whose object access is GetObject on the staged-content prefix only. They MUST NOT receive source-provider, backup, writer, or bucket-management credentials. Workers SHOULD have network egress limited to what they need. Disallowed access MUST be denied.

#### Scenario: Reader access matrix
- **WHEN** the CPU identity reads staged media and attempts writes, deletion, or access to another environment or backup bucket
- **THEN** its staged-media read succeeds and every disallowed operation fails

### Requirement: Reader grants beyond the staged-content prefix are backend-specific
Where the backend cannot otherwise distinguish a missing object from a denial, the reader MAY also hold ListBucket on its own environment's staging bucket; that grant covers the whole bucket. Where the backend evaluates the prefix's readiness marker against its key without the trailing slash, the reader MAY also hold GetObject on that one slash-less key; that grant SHALL be documented.

#### Scenario: Reader bucket listing
- **WHEN** the CPU identity lists its own environment's staging bucket
- **THEN** the listing succeeds only on a backend where the reader holds the ListBucket grant, and is denied everywhere else

#### Scenario: Slash-less readiness marker key
- **WHEN** the backend evaluates the readiness marker against the prefix key without its trailing slash
- **THEN** the reader's GetObject on that one key is granted and documented, and no other key outside the staged-content prefix becomes readable

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

### Requirement: Bounded active staging capacity
Admission SHALL reserve the incoming declared maximum before consuming a source stream and
SHALL account for completed remote occupancy and live reservations across workers.
The system SHALL evict eligible idle content by age and oldest-first under pressure. An oversized stream, exhausted
capacity, or uncertain occupancy MUST fail explicitly. Residual multipart/orphan storage
SHALL be separately reported and cleaned; active capacity limits MUST NOT be presented as a
hard billing cap.

#### Scenario: Capacity pressure during active download
- **WHEN** admitting another item would exceed the configured bound and remaining candidates have active downloads
- **THEN** admission fails without removing any protected object

#### Scenario: A reservation counts before its bytes land
- **WHEN** one worker holds a reservation whose bytes have not yet appeared in the listing and a second worker's declaration would exceed the bound together with it
- **THEN** the second worker's admission reclaims or is refused exactly as if the reserved bytes were completed occupancy

#### Scenario: Eviction under pressure commits before bytes go
- **WHEN** admission evicts idle entries to make room
- **THEN** the unregistrations commit with the reservation, the evicted bytes are deleted afterwards, and a deletion failure abandons the reservation and is reported after the commit

### Requirement: Admissions do not wait for uploads
Reservations SHALL be accepted concurrently: a worker's admission MUST NOT wait for another
worker's upload, only for its admission or registration.

#### Scenario: Two workers upload at once
- **WHEN** one worker is uploading an admitted object and a second worker's declaration fits beside that reservation
- **THEN** the second worker is admitted and uploads while the first upload is still in flight

### Requirement: Verified seekable read snapshots
CPU readers SHALL receive seekable local snapshots only after the requested immutable object's size and SHA-256 match its registered metadata. Local disk reservations SHALL remain active until the handle closes and SHALL be bounded across concurrent reads. A missing object, truncated response, checksum mismatch or disk exhaustion MUST raise a specific failure without source-provider fallback.

#### Scenario: Deletion after download
- **WHEN** remote content is released after its complete verified local snapshot is opened
- **THEN** the decoder continues to read unchanged bytes until it closes the handle

#### Scenario: Corrupt or unavailable content
- **WHEN** a download is missing, truncated or differs from its registered digest
- **THEN** no decoder handle is returned and temporary local resources are reclaimed

### Requirement: Cleanup without active ingestion
Completed objects SHALL be deleted promptly when no download uses them. Periodic cleanup
SHALL reclaim eligible aged entries even when no new jobs arrive. Failed cleanup MUST be visible and
retryable. Incomplete multipart uploads and abandoned content SHALL have independently
verified backend-appropriate cleanup backstops (lifecycle or server cleanup plus explicit
abort) without retention tiers or object versions that impose unnecessary minimum-duration
charges.

#### Scenario: Worker dies during transfer
- **WHEN** a worker dies after starting an upload or before publishing completed content
- **THEN** no incomplete object becomes readable, its reservation expires, and residual storage is reclaimed by reconciliation or the verified backend-specific lifecycle/server cleanup

### Requirement: Reconciliation spares live reservations
Reconciliation MUST spare a completed object whose attempt holds a live reservation and MUST
collect it once the reservation has ended without a registration.

#### Scenario: Reconciliation runs beside an open upload
- **WHEN** another worker's upload has completed its object but not yet registered it, under a live reservation
- **THEN** reconciliation leaves that object alone and the later registration succeeds

### Requirement: Compatibility verified on the local engine and OVH
For every S3 behavior introduced or changed, including SDK upgrades and changes of local
engine, the project MUST check authoritative documentation for the pinned local engine and
for OVH, and execute the affected shared contract tests on both the pinned local release
and the target OVH endpoint. Passing only one backend MUST NOT establish compatibility.
Failed or unavailable required checks SHALL block acceptance and activation of that
behavior.

#### Scenario: Local engine supports an operation that OVH does not
- **WHEN** an operation or request option succeeds on the local engine but is unsupported or fails on OVH
- **THEN** the implementation is rejected until a verified common behavior or explicit provisioning-specific solution preserves the required contract

#### Scenario: Remote tests cannot run
- **WHEN** local tests pass but required OVH checks cannot be executed
- **THEN** the report marks OVH verification pending and S3 activation remains blocked

#### Scenario: SDK or server changes
- **WHEN** a pinned local release or SDK version changes, or a provider capability used by staging changes
- **THEN** affected signatures, headers, checksums, multipart behavior, permissions and error handling are reverified on both backends before release

### Requirement: Compatibility evidence is attributable
Evidence SHALL identify the engine, its pinned version or digest, the endpoint, date,
results and documented differences.

#### Scenario: The local engine is replaced
- **WHEN** a different local S3 engine is adopted
- **THEN** every compatibility entry whose local evidence came from the previous engine returns to pending, is re-earned against the new engine at its pinned version, and the superseded evidence is retained and labelled rather than deleted

### Requirement: Equivalent development security and cleanup behavior
Local development SHALL provision dedicated reader/writer identities and verified
encryption and residual-upload cleanup. Provisioning mechanisms MAY differ from OVH, but
access and cleanup guarantees MUST be tested independently on each. Runtime workers MUST
NOT use bootstrap administrator credentials. Unsupported configuration MUST fail explicitly
rather than silently disabling the guarantee.

#### Scenario: Abort lifecycle rule is unsupported locally
- **WHEN** the local engine does not support the incomplete-upload lifecycle action used on OVH
- **THEN** initialization uses a verified engine-specific cleanup mechanism and tests explicit abort and stale-upload reclamation without claiming the OVH lifecycle configuration was tested locally

#### Scenario: Abort lifecycle rule is accepted locally
- **WHEN** the local engine accepts the same incomplete-upload lifecycle payload OVH accepts
- **THEN** reclamation is observed before the mechanisms are treated as converged, and a rule that is stored but not enforced keeps the cleanup evidence pending

#### Scenario: Encryption is unavailable
- **WHEN** key management required for the configured encryption mode is missing
- **THEN** initialization or encrypted staging checks fail and do not fall back to unencrypted storage

#### Scenario: Reader authority under a different policy mechanism
- **WHEN** the local engine expresses identity scoping through a different policy mechanism than the previous engine
- **THEN** the same allow/deny matrix is proven against it: the reader may read staged media under the content prefix and every write, delete, listing, prefix escape, other-bucket and bucket-creation attempt is denied

### Requirement: Writer-lock holdings are observable
Every fresh acquisition of the staging writer lock SHALL emit one `staging.writer_lock_held`
event after the transaction that owned the lock ends, carrying the caller, the wait to
acquire, the time held and `entry_claims`: the number of distinct entry locks the holding
took, every one held until the holding ended. A re-entrant acquisition MUST emit nothing. The event MUST be emitted whether the holding
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

### Requirement: No writer-lock holding spans an upload
A reservation SHALL take the lock for its admission (`occupancy.reserve`), its registration
(`occupancy.register`) and, when it registers nothing, its abandonment
(`occupancy.abandon`); no holding SHALL span the upload.

#### Scenario: A reservation's holdings end before its upload
- **WHEN** a publication is admitted, uploads and registers
- **THEN** an `occupancy.reserve` event is emitted before the upload begins and an `occupancy.register` event after it ends, and neither `held_seconds` includes the upload

### Requirement: Reservations report their phases
Every publication reservation SHALL emit exactly one `staging.reservation_phases` event,
however it ended, with the staged key, the declared size, the outcome (`registered`,
`refused`, `cancelled` or `failed`), the error class or null, and seconds for admission,
upload, registration and commit.

#### Scenario: A publication reports its phases
- **WHEN** a publication is admitted, uploads, registers and commits
- **THEN** one event is emitted with outcome `registered`, a null error and a non-null value for all four phases

#### Scenario: A refused reservation
- **WHEN** admission refuses the declared size before the upload begins
- **THEN** one event is emitted with outcome `refused`, null admission timing, null reclamation counts and null upload, registration and commit phases, and the refusal propagates to the caller unchanged

#### Scenario: A cancelled upload
- **WHEN** the task is cancelled while the upload is in flight
- **THEN** one event is emitted with outcome `cancelled` and the cancellation propagates

#### Scenario: A reclaiming admission reports what it reclaimed
- **WHEN** admission evicts idle entries or deletes orphans to admit the reservation
- **THEN** the event carries the counts of evicted keys, protected keys and deleted orphan objects, and the admission time includes the reclamation

### Requirement: Reservation phases add up to its lifetime
Admission SHALL be the admission holding, registration SHALL include the wait for the
registration holding, and commit SHALL be that holding's commit; the four add up to the
reservation's lifetime after the lock wait for admission. A phase that never started MUST
be null; one cut short by a failure or cancellation MUST run to the end of the reservation,
rollback included.

#### Scenario: A failed upload
- **WHEN** the upload raises after admission
- **THEN** one event is emitted with outcome `failed`, the error's class name, a non-null upload phase and null registration and commit phases

#### Scenario: A lost reservation
- **WHEN** the upload finishes after its reservation expired
- **THEN** one event is emitted with outcome `failed`, the lost-reservation error's class name, non-null upload and registration phases and a null commit phase

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

### Requirement: A reader's wait never spans an upload
A reader's wait for an entry lock MUST end with the holding that claimed the lock and MUST
NOT span an upload the holding admitted.

#### Scenario: A reader is not blocked by an admitted upload
- **WHEN** an admission reclaimed an orphan under the reader's key, evicted the reader's key, or claimed a colliding key, and that admission's upload is still in flight
- **THEN** the reader receives its winner, or a miss for the evicted key, without waiting for the upload, and its event's wait is bounded by the admission holding

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

### Requirement: The local staging bucket and prefix are fixed by the RustFS policies
The local stack SHALL stage in the one bucket and prefix both RustFS policies grant,
`curate-d-staging` and `content/`. Local provisioning MUST read both from the policies and
take neither as input, and the workers' compose values MUST be literals equal to that grant.
A policy without exactly one prefix grant, or two policies granting different ones, MUST fail
provisioning before it creates a bucket. OVH environments keep setting their own.

#### Scenario: A shell variable names another bucket or prefix
- **WHEN** `CURATE_D_STAGING_S3_BUCKET` or `CURATE_D_STAGING_S3_CONTENT_PREFIX` is exported and the local stack is provisioned
- **THEN** provisioning creates `curate-d-staging`, passes its access-matrix check, and the workers stage under `content/`

#### Scenario: The policies disagree
- **WHEN** the reader and writer policies grant different prefixes, or one grants none or several
- **THEN** provisioning fails naming the policy files and creates no bucket

#### Scenario: Compose drifts from the policies
- **WHEN** a worker's compose bucket or prefix is interpolated or differs from the policies' grant, or provisioning is given a bucket or prefix input
- **THEN** the unit suite fails naming the setting
