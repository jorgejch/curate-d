# placement-execution Specification

## Purpose

Carries a single content object to a single destination through copy, verification, and registration, establishing the verification level that later governs whether the source may ever be released.

## Requirements

### Requirement: Copy before registration

The system SHALL complete a copy and verify it before registering the placement as present. The system SHALL NOT modify, move, or delete source content during placement execution.

#### Scenario: Successful placement
- **WHEN** a placement executes without error
- **THEN** the content is copied to the destination, verified, and only then registered as present

#### Scenario: Source untouched during placement
- **WHEN** a placement executes, whether it succeeds or fails
- **THEN** the source content remains present and unmodified

#### Scenario: Interruption mid-transfer
- **WHEN** a placement is interrupted before verification completes
- **THEN** the placement is not registered as present
- **AND** a subsequent attempt re-executes the placement without requiring manual cleanup

### Requirement: Verification ladder

The system SHALL assign each placement a verification level reflecting the strongest check it was able to perform, from unverifiable through comparison of a cryptographic content hash on both sides.

#### Scenario: Common hash available on both sides
- **WHEN** source and destination expose a common cryptographic hash algorithm
- **THEN** the hashes are compared
- **AND** the placement is assigned the strong common-hash verification level on a match

#### Scenario: No common hash available
- **WHEN** source and destination expose no common cryptographic hash algorithm
- **THEN** the transferring worker computes a digest over the content it transfers or reads back from the destination, in an algorithm the opposite side reports, and compares it with that side's reported digest
- **AND** the placement is assigned the strong in-transit verification level on a match

#### Scenario: Only size and timestamp comparable
- **WHEN** neither a common hash nor an in-transit hash is obtainable
- **THEN** the placement is assigned a weak verification level
- **AND** the reason the stronger checks were unavailable is recorded

#### Scenario: Verification mismatch
- **WHEN** a verification check fails to match
- **THEN** the placement is not registered as present
- **AND** the mismatch is recorded with both observed values
- **AND** the destination copy is treated as untrusted

#### Scenario: Source changed since fingerprinting
- **WHEN** the source's reported digests or version marker at copy time differ from those recorded when its content was fingerprinted
- **THEN** the placement is not registered as present
- **AND** the change is recorded with both observed values
- **AND** the source is sent back for fingerprinting instead of being placed as the content that was classified

#### Scenario: Mismatch detected by the transfer service
- **WHEN** the transfer service's own post-copy size or hash check fails and it removes the destination copy
- **THEN** the placement is not registered as present
- **AND** the mismatch is recorded with both observed values taken from the transfer service's report

### Requirement: Idempotent execution

Executing the same placement more than once SHALL NOT produce duplicate destination content or duplicate ledger records.

#### Scenario: Repeated execution after success
- **WHEN** a placement that already completed is executed again
- **THEN** no additional copy is performed
- **AND** the existing placement record is unchanged

#### Scenario: Repeated execution after failure
- **WHEN** a placement that previously failed is executed again
- **THEN** the attempt proceeds
- **AND** any partial destination content from the failed attempt is superseded rather than duplicated

### Requirement: Bounded resource consumption

The system SHALL bound concurrent transfers so that combined in-flight content cannot exhaust available local scratch storage.

#### Scenario: Large transfers constrained
- **WHEN** transfers are scheduled whose combined size would exceed available scratch storage
- **THEN** execution is deferred until capacity is available
- **AND** smaller transfers are not blocked behind larger ones indefinitely

#### Scenario: Scratch storage exhausted
- **WHEN** scratch storage is exhausted despite constraints
- **THEN** the affected placement fails with an explicit resource error
- **AND** the placement is retried later rather than being abandoned

### Requirement: Dry-run evaluation

The system SHALL support evaluating placements without performing them. In dry-run mode the system SHALL report the destination locator and the verification level it expects to achieve, and SHALL NOT write to any destination, modify any source, or record a placement as present.

#### Scenario: Dry run reports proposed placement
- **WHEN** a placement is evaluated in dry-run mode
- **THEN** the proposed destination locator is reported
- **AND** no content is written to the destination

#### Scenario: Dry run leaves no durable placement state
- **WHEN** a dry-run evaluation completes
- **THEN** no placement is recorded as present
- **AND** a subsequent real execution proceeds as though the dry run had not occurred

#### Scenario: Dry run surfaces rendering failure
- **WHEN** a placement cannot be rendered during dry-run evaluation
- **THEN** the failure and its reason are reported
- **AND** the evaluation continues with the remaining content objects rather than aborting

### Requirement: Verification evidence collected by the transferring profile

The system SHALL obtain every value verification compares — provider-reported digests with their algorithms, sizes and modification times on both sides, and any digest it computes over content — on the worker profile that performs the transfer, and SHALL record them durably before the verification level is assessed. The assessment SHALL compare only recorded values and SHALL NOT read content.

#### Scenario: Evidence recorded before assessment
- **WHEN** a placement's copy completes
- **THEN** both sides' observed values are recorded with the algorithm of each digest
- **AND** the verification level is assigned from those recorded values alone

#### Scenario: Evidence partly unavailable
- **WHEN** a value needed for a stronger rung cannot be obtained after retry
- **THEN** its absence and the reason are recorded
- **AND** the placement is assigned the strongest rung the recorded values support

### Requirement: Verification bound to its attempt

Each copy SHALL be verified by a verification of the attempt the placement recorded when that copy started. A verification SHALL conclude a placement only while it is still verifying that attempt's copy, checked in the transaction that records the conclusion. Otherwise the verification SHALL change nothing and write no audit entry. Starting a copy SHALL discard the previous copy's evidence in the same transaction.

#### Scenario: A verification belongs to the attempt the start recorded
- **WHEN** a placement attempt runs again after the placement recorded a later attempt, as after a rewind of a failed or untrusted attempt
- **THEN** the copy is verified by a verification of the attempt recorded when it started
- **AND** it does not collapse onto the earlier attempt's finished verification, so the placement ends registered, untrusted or failed rather than left verifying

#### Scenario: An old attempt started over a placement that has a verification
- **WHEN** an old attempt runs again over a placement still copying, verifying, or registered at another locator
- **THEN** the copy records the attempt after the placement's latest
- **AND** it is verified by a verification of that attempt, not the finished one of the attempt it replaced

#### Scenario: Two first copies of one placement start at the same time
- **WHEN** two copies of the same content to the same destination start concurrently before the placement has a record, rendered to different locators
- **THEN** one start waits until the other has recorded the placement, then copies nothing, records nothing and ends as superseded
- **AND** the placement keeps the first start's locator and attempt, so reconciliation finds the first copy's workflow running and does not record its attempt as ended
- **AND** the first copy's evidence is never recorded against the second's locator

#### Scenario: A new copy discards the previous copy's evidence
- **WHEN** a copy starts
- **THEN** the evidence of any previous copy is discarded in the same transaction that records the start
- **AND** a verification of the previous copy run afterwards finds nothing to assess and registers nothing

#### Scenario: A verification concludes only a placement still verifying
- **WHEN** a verification runs again, by a rewind or a fork, after a newer attempt has started or after the placement was concluded
- **THEN** the placement is left unchanged, no audit entry is written, and the verification ends as superseded
- **AND** the old copy's evidence is never registered at the new attempt's locator

#### Scenario: Verification enqueued before attempts were bound
- **WHEN** a verification enqueued without an attempt runs
- **THEN** it concludes the placement only if the placement is verifying, whatever its attempt

### Requirement: Late attempts record nothing

An attempt's evidence, its wait for resources and its failure SHALL be recorded only while that attempt is the placement's latest and the placement is still copying. An attempt that finds it is no longer the latest SHALL end as superseded. It SHALL start no verification and write no audit entry.

#### Scenario: An old attempt's copy completes late
- **WHEN** an attempt's copy completes after a newer attempt has started
- **THEN** its evidence is not recorded, no verification is started for it, and the attempt ends as superseded

#### Scenario: An old attempt's copy fails late
- **WHEN** an attempt's copy fails after a newer attempt has started or the placement was registered
- **THEN** the placement is left unchanged, no audit entry is written, and the attempt ends as superseded

#### Scenario: An old attempt waits for resources late
- **WHEN** an attempt that must wait for resources is no longer the placement's latest
- **THEN** its wait reason is not recorded over the newer attempt's state

#### Scenario: An old attempt is superseded while waiting for resources
- **WHEN** a copy is deferred for admission or scratch and a newer attempt has started
- **THEN** the deferral is not recorded, the old attempt copies nothing more and ends as superseded

#### Scenario: A registered placement rewound
- **WHEN** a placement already registered at its locator is executed again by a rewind
- **THEN** no copy is performed and the placement record is unchanged
