## MODIFIED Requirements

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

#### Scenario: Consumed version is released
- **WHEN** every workflow that reads a staged version has finished reading it, by fingerprinting it and, where classification was handed that version, by classifying it or reusing an existing classification
- **THEN** the version's registration is dropped and its bytes are deleted without waiting for age eviction or admission pressure

#### Scenario: Content classified from another source's copy
- **WHEN** a source is fingerprinted to content whose classification reads, or has already read, another source's staged copy
- **THEN** this source's staged version is released once fingerprinting ends, and the other copy stays until its own classification finishes

#### Scenario: Key restaged before the release runs
- **WHEN** a release names a version that a newer version of the same source has since replaced under the same key
- **THEN** the release changes nothing and the newer version stays registered and readable

#### Scenario: Download still holds the consumed version
- **WHEN** a release runs while a download holds the consumed version
- **THEN** the registration is dropped, the download keeps reading unchanged bytes, and the next periodic cleanup deletes them once the hold has ended

#### Scenario: Release fails
- **WHEN** the release cannot change the cache index or cannot delete the bytes
- **THEN** the workflow that consumed the version keeps its outcome, the failure is logged as a named event, and periodic cleanup reclaims the bytes: unregistered bytes on its next pass, a still-registered version once it ages out

#### Scenario: Consumer fails or is cancelled
- **WHEN** a workflow that reads a staged version ends by failure or cancellation before releasing it
- **THEN** the version stays registered for a resumed or forked run to read, and age eviction or admission reclamation removes it otherwise
