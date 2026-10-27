## MODIFIED Requirements

### Requirement: Cleanup without active ingestion
A completed entry SHALL be released when its key is re-staged or its bytes show a source
change, and otherwise SHALL leave only by age eviction or admission reclamation, never when
its readers finish. Periodic cleanup SHALL reclaim aged entries without new jobs. Failed
cleanup MUST be visible and retryable. Incomplete multipart uploads and abandoned content
SHALL have independently verified backend backstops (lifecycle or server cleanup plus
abort), with no retention tiers or object versions.

#### Scenario: Worker dies during transfer
- **WHEN** a worker dies after starting an upload or before publishing completed content
- **THEN** no incomplete object becomes readable, its reservation expires, and residual storage is reclaimed by reconciliation or the verified backend-specific lifecycle/server cleanup

#### Scenario: Consumed entry stays until aged or reclaimed
- **WHEN** fingerprinting and classification have both read a staged entry
- **THEN** the entry stays registered and its bytes stay in the store until age eviction or reclamation under admission pressure removes it

#### Scenario: A later reader finds the entry an earlier one read
- **WHEN** classification reads an entry that fingerprinting has already read and finished with
- **THEN** it reads the same registered bytes, because no reader's completion releases them; a reader that finds its entry missing gets a miss and never fetches from the source provider

#### Scenario: Re-stage releases the old version
- **WHEN** a key is staged again with a new version of its source
- **THEN** the previous registration is released before the new upload is admitted, and a reader still holding a reference to the old version gets a superseded or missing failure, never the new bytes as the old ones

#### Scenario: Source change releases the fetched bytes
- **WHEN** the bytes staged for a source do not match the digests its scan listed
- **THEN** the entry just published is released, the ingest ends as a source change, and no reader is handed it

#### Scenario: Aged entry reclaimed without new jobs
- **WHEN** no new job arrives and an entry is older than the configured age bound
- **THEN** the periodic sweep unregisters it and deletes its bytes, skipping an entry a download still holds until a later sweep

#### Scenario: Admission reclaims under pressure
- **WHEN** an admission does not fit beside what staging holds
- **THEN** unreferenced objects are reclaimed first, then aged entries, then the oldest entries no download holds, until the declaration fits or it is refused

#### Scenario: Failed cleanup
- **WHEN** a sweep's listing, unregistration or deletion fails
- **THEN** the failure is logged, unregistrations that committed stand with their bytes left as unreferenced objects, and the next sweep repeats the pass
