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
