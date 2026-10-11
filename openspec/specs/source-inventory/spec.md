# source-inventory Specification

## Purpose

Enumerates files in a connected provider and detects what has changed since the previous run, so that repeated scans cost little and no file is processed twice without reason.

## Requirements

### Requirement: Source enumeration

The system SHALL enumerate files within a configured provider subtree and record, for each file, the provider-native identifier, path, size, modification time, provider-supplied version marker where available, and each content digest the provider reports with its algorithm. Reported digests are recorded from listing metadata; the scan SHALL NOT read content to obtain them.

#### Scenario: Subtree enumerated
- **WHEN** a scan runs against a configured subtree
- **THEN** every file in that subtree is recorded as a source object
- **AND** each record carries the provider-native identifier for that file

#### Scenario: Provider-reported digests recorded
- **WHEN** a provider reports content digests for a file in its listing metadata
- **THEN** each digest is recorded on the source object with its algorithm
- **AND** no content is read to obtain them

#### Scenario: Provider error during enumeration
- **WHEN** the provider returns an error partway through an enumeration
- **THEN** source objects discovered before the error remain recorded
- **AND** the scan is marked incomplete with the failure reason
- **AND** a subsequent scan resumes without re-listing already-confirmed portions

### Requirement: Change detection between runs

The system SHALL skip files whose provider-native identifier, version marker, and size are unchanged since the previous successful scan, without re-reading their content.

#### Scenario: Unchanged file skipped
- **WHEN** a scan encounters a file whose identifier, version marker, and size match the previous scan
- **THEN** no content is read for that file
- **AND** no downstream classification or transfer work is created

#### Scenario: Modified file re-processed
- **WHEN** a scan encounters a file whose version marker or size differs from the previous scan
- **THEN** the source object record is updated
- **AND** downstream work is created for the new content

#### Scenario: Version marker unavailable
- **WHEN** a provider supplies no version marker for a file
- **THEN** the system falls back to comparing size and modification time
- **AND** records that the comparison used a weaker signal

### Requirement: Rate limit compliance

The system SHALL respect provider request quotas and SHALL NOT allow a scan against one provider to exhaust request capacity needed by another.

#### Scenario: Requests paced within quota
- **WHEN** a scan issues provider requests
- **THEN** the request rate stays within the configured quota for that provider

#### Scenario: Provider signals rate limiting
- **WHEN** a provider responds with a rate limit error
- **THEN** the system backs off before retrying
- **AND** the scan completes rather than failing, unless the limit persists beyond a configured bound

### Requirement: Periodic full rescan

The system SHALL periodically perform a full rescan that re-examines every file regardless of version marker, so that provider-side changes not reflected in version markers are eventually detected. The full rescan interval SHALL be configurable independently of the incremental scan interval.

#### Scenario: Full rescan detects an unreported change
- **WHEN** a full rescan encounters a file whose content fingerprint differs from the recorded one, despite an unchanged version marker
- **THEN** the source object record is updated
- **AND** downstream work is created for the new content
- **AND** the discrepancy is recorded so that reliance on that provider's version marker can be assessed

#### Scenario: Full rescan confirms no change
- **WHEN** a full rescan encounters a file whose content fingerprint matches the recorded one
- **THEN** no downstream work is created
- **AND** the verification time is recorded on the source object

#### Scenario: Full rescan interrupted
- **WHEN** a full rescan fails partway through
- **THEN** files already re-examined retain their updated verification time
- **AND** the next full rescan resumes rather than restarting from the beginning
