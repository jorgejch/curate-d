# Spec Delta

## ADDED Requirements

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
- **THEN** one start waits until the other has recorded the placement, then records the attempt after it at its own locator
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
