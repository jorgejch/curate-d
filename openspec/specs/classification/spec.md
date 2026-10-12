# classification Specification

## Purpose

Determines what a file is by escalating through progressively more expensive techniques, and emits structured facts describing it, so that destination paths can be derived consistently without spending inference cost on files that simpler methods already resolve.

## Requirements

### Requirement: Tiered escalation with confidence gating

The system SHALL attempt classification in ascending order of cost, and SHALL stop at the first tier whose result meets the configured confidence threshold. A more expensive tier SHALL NOT run when a cheaper tier has already produced a sufficiently confident result.

#### Scenario: Deterministic tier suffices
- **WHEN** file extension, embedded metadata, or filename patterns produce a result above the confidence threshold
- **THEN** no local inference or remote model call is made
- **AND** the recorded facts note which tier produced them

#### Scenario: Escalation to remote model
- **WHEN** neither deterministic nor local inference tiers reach the confidence threshold
- **THEN** the remote model tier is invoked
- **AND** the recorded facts note that the remote tier produced them

#### Scenario: All tiers fail to reach confidence
- **WHEN** no tier reaches the confidence threshold
- **THEN** the content object is placed in the review queue
- **AND** no destination path is derived for it
- **AND** the file is not transferred to an organized location

### Requirement: Facts-only output

Classification SHALL emit structured facts drawn from a defined schema. Classification SHALL NOT emit a destination path, filename, or folder name.

#### Scenario: Facts conform to schema
- **WHEN** any tier produces a classification result
- **THEN** the result consists only of fields defined in the facts schema
- **AND** each field value conforms to that field's declared type

#### Scenario: Model returns unusable output
- **WHEN** the remote model tier returns output that does not conform to the facts schema
- **THEN** the output is rejected
- **AND** the content object is placed in the review queue rather than being assigned invented facts

### Requirement: Closed-set taxonomy

Category and subcategory facts SHALL be drawn from a configured closed set. A value outside that set SHALL NOT be accepted.

#### Scenario: Value within taxonomy accepted
- **WHEN** a tier produces a category present in the configured taxonomy
- **THEN** the category is recorded on the content object

#### Scenario: Value outside taxonomy rejected
- **WHEN** a tier produces a category absent from the configured taxonomy
- **THEN** the value is rejected
- **AND** the content object is placed in the review queue
- **AND** the rejected value is recorded so an operator can decide whether to extend the taxonomy

### Requirement: Classification reuse

The system SHALL classify a given content fingerprint at most once, and SHALL reuse the stored result for any later source object resolving to the same fingerprint.

#### Scenario: Repeat content reuses result
- **WHEN** a source object resolves to a content fingerprint that already has recorded facts
- **THEN** the stored facts are reused
- **AND** no tier is invoked

### Requirement: Cost governance

The system SHALL enforce a configured spending cap on the remote model tier and SHALL stop invoking that tier when the cap is reached.

#### Scenario: Spend within cap
- **WHEN** remote model spending for the current period is below the configured cap
- **THEN** eligible content objects escalate to the remote tier normally

#### Scenario: Cap reached
- **WHEN** remote model spending for the current period reaches the configured cap
- **THEN** no further remote model calls are made in that period
- **AND** affected content objects are held for the next period rather than being discarded or misclassified
- **AND** the cap breach is reported through the control plane

### Requirement: Content read within the CPU worker's limits

The cheap tiers SHALL read content only within configured limits: every image decode SHALL stay within a pixel budget, and OCR SHALL be bounded in pages, embedded images, run time per image and extracted text. A limit that cut extraction short SHALL be recorded in the tier's evidence. An image above the pixel budget SHALL be refused before it is decoded, and a refusal SHALL NOT end the classification in an error.

#### Scenario: Image above the pixel budget
- **WHEN** an image would decode to more pixels than the configured budget, after any reduced-scale decode its format allows
- **THEN** it is not decoded, neither for OCR nor for the image the remote tier is shown
- **AND** the evidence records the decode budget as the limit that applied

#### Scenario: Image above the decoder's own ceiling
- **WHEN** an image declares more pixels than the image library itself accepts
- **THEN** metadata extraction yields no metadata and the image is treated as above the budget
- **AND** the classification workflow completes rather than failing

#### Scenario: Long scanned document
- **WHEN** a document without a text layer has more pages or embedded images than OCR reads, or its text reaches the cap
- **THEN** OCR stops at the first limit reached
- **AND** the text read so far is kept and the limit is recorded

#### Scenario: OCR runs past its time limit
- **WHEN** OCR of one image runs past its time limit
- **THEN** the OCR process is stopped, the text read so far is kept, and no further image is OCRed
- **AND** the limit is recorded

### Requirement: Content a limit kept unread goes to review

When a limit refused the content and the local tier read no text from it, and the cheaper tiers did not reach the confidence threshold, the system SHALL place the content object in the review queue with the reason `resource_limit` and a detail naming the limit. The remote tier SHALL NOT be called for it.

#### Scenario: Refused image the cheap tiers cannot settle
- **WHEN** an image is above the pixel budget and neither the deterministic nor the local tier reaches the threshold
- **THEN** a review item is opened with reason `resource_limit`
- **AND** no remote model call is made

#### Scenario: Capped text is still escalated
- **WHEN** text was read but cut at the text, page or image cap, and the cheap tiers do not settle the content
- **THEN** the content escalates to the remote tier with the text that was read

#### Scenario: Refused image settled by its name or metadata
- **WHEN** an image is above the pixel budget but the deterministic tier reaches the threshold
- **THEN** its facts are recorded as usual

### Requirement: Bounded remote answer

Each remote model call SHALL set a configured ceiling on the tokens the answer may use. An answer the model stopped at that ceiling SHALL be treated as output that does not conform to the facts schema.

#### Scenario: Answer cut off at the ceiling
- **WHEN** the remote model's answer ends because it reached the token ceiling
- **THEN** the answer is rejected as malformed
- **AND** the content object is placed in the review queue with the cost of the call recorded
