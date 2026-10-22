# control-plane Specification

## Purpose

Provides the HTTP API through which a person configures connections, taxonomy, and rules, observes what the system is doing, reviews proposed organization before committing to it, and resolves the items the automated pipeline could not decide on its own.

## Requirements

### Requirement: Connection management

The control plane SHALL allow a user to initiate provider authorization, view connection health, and re-authorize a connection whose credentials have expired.

#### Scenario: New connection authorized
- **WHEN** a user completes the authorization flow for a provider
- **THEN** the connection appears with healthy status and is available to scans

#### Scenario: Expired connection surfaced
- **WHEN** a connection requires re-authentication
- **THEN** the control plane shows it as requiring attention
- **AND** offers the action needed to restore it

### Requirement: Configuration management

The control plane SHALL allow a user to view and edit the taxonomy, path templates, and placement policies. Invalid configuration SHALL be rejected before it takes effect. A condition or destination naming a value that can never match or never render is invalid configuration. A category or subcategory condition is checked against the taxonomy in force when its policy or template set is activated, and never when a stored version is read.

#### Scenario: Valid configuration accepted
- **WHEN** a user submits a configuration change that satisfies validation
- **THEN** the change is persisted and applied to subsequent work

#### Scenario: Invalid configuration rejected
- **WHEN** a user submits a configuration change that fails validation
- **THEN** the change is rejected with an error identifying the problem
- **AND** the previously active configuration remains in effect

#### Scenario: Condition that can never match rejected
- **WHEN** a user submits a placement policy or template set with a placement rule, retention rule or template condition on a `media_kind` that is not a known media kind, or a policy naming a destination capability profile that does not exist
- **THEN** the change is rejected with 422 and an error locating the condition or destination and naming the value
- **AND** the previously active configuration remains in effect

#### Scenario: Condition value outside the taxonomy rejected
- **WHEN** a user submits a placement policy or template set with a category condition the taxonomy in force does not list, or a subcategory condition not listed under its category (or under any category, when the condition names none)
- **THEN** the change is rejected with 422 and an error locating the rule or template and naming the value
- **AND** the previously active configuration remains in effect

#### Scenario: Taxonomy changing during a policy activation
- **WHEN** a taxonomy activation and a policy or template activation run concurrently
- **THEN** either the policy or template set is checked against the new taxonomy, or the taxonomy activation reports its conditions as orphaned
- **AND** no condition the new taxonomy orphans is both accepted and unreported

#### Scenario: Taxonomy change reports orphaned conditions
- **WHEN** a user activates a taxonomy version that no longer lists a value a placement rule, retention rule or template selects on
- **THEN** the taxonomy version is activated
- **AND** the response lists each orphaned condition with its document, its rule or template, the field and the value, and the same list is logged as `configuration.orphaned_conditions`
- **AND** the policy and template set in force stay readable and unchanged

#### Scenario: Retired retention field refused on submission
- **WHEN** a user submits a placement policy whose retention rule carries `source_counts_as_copy`
- **THEN** the change is rejected with 422 as an unknown field, located at that rule
- **AND** the previously active configuration remains in effect

#### Scenario: Template change does not disturb existing placements
- **WHEN** a user changes the active path template version
- **THEN** existing placements remain valid and untouched until reconciliation acts on them

### Requirement: Job observability

The control plane SHALL report the state of scans, classification, and placements, including counts of pending, in-progress, completed, and failed work, and the reason for each failure.

#### Scenario: Progress reported
- **WHEN** work is in progress
- **THEN** the control plane reports current counts by state

#### Scenario: Failure detail available
- **WHEN** a unit of work has failed
- **THEN** the control plane reports which content object or placement failed and why

### Requirement: Review queue

The control plane SHALL present items the pipeline could not classify or render, and SHALL allow a user to resolve them by assigning facts, extending the taxonomy, or excluding the item.

#### Scenario: Item resolved by assigning facts
- **WHEN** a user assigns valid facts to a queued item
- **THEN** the item leaves the review queue
- **AND** it becomes eligible for rendering and placement

#### Scenario: Item resolved by extending taxonomy
- **WHEN** a user promotes a previously rejected value into the taxonomy
- **THEN** the value becomes valid for future classification
- **AND** the queued item can be resolved using it

#### Scenario: Invalid resolution rejected
- **WHEN** a user submits a resolution whose facts fail schema or taxonomy validation
- **THEN** the resolution is rejected with an error
- **AND** the item remains in the review queue

### Requirement: Audit inspection

The control plane SHALL allow a user to inspect the recorded history for a given content object, including how it was classified, where it was placed, and what verification each placement achieved.

#### Scenario: History retrieved
- **WHEN** a user inspects a content object
- **THEN** its classification decisions, placements, verification levels, and retention decisions are shown in order

#### Scenario: Unknown content object
- **WHEN** a user requests history for an identifier the ledger does not contain
- **THEN** the control plane reports that no such content object exists

### Requirement: Dry-run result export

The control plane SHALL expose a dry run's results for bulk review of the proposed organization: each content object's derived facts, its proposed locator, and the items that failed classification or rendering. When the run evaluated a policy's rules, each proposed destination SHALL name the rules that selected it, by position in the policy and by name. Every rule of the policy SHALL be counted, apart from any other rule of the same name, and classified content no rule selects SHALL be reported.

#### Scenario: Dry-run results exported
- **WHEN** a user requests the results of a completed dry run
- **THEN** each evaluated content object is reported with its derived facts and proposed locator

#### Scenario: Unresolved items included
- **WHEN** a dry run produced items that could not be classified or rendered
- **THEN** those items appear in the export with the reason they could not be resolved
- **AND** the values rejected for falling outside the taxonomy are included so they can inform taxonomy refinement

#### Scenario: Export requested for an unknown run
- **WHEN** a user requests results for a dry run identifier that does not exist
- **THEN** the control plane reports that no such run exists

#### Scenario: Export names the selecting rule
- **WHEN** a user exports a dry run evaluated with a policy's rules
- **THEN** each item names the rules that selected its destination, in policy order, each by its position in the policy and its name
- **AND** every rule of the evaluated policy is counted by the content objects it selected, in policy order, a rule that selected nothing as zero

#### Scenario: Rules sharing a name counted apart
- **WHEN** a user exports a dry run whose policy holds two rules of one name, one selecting a content object and the other selecting none
- **THEN** the rule counts hold one entry for each rule, by position, with counts 1 and 0

#### Scenario: Content no rule selects
- **WHEN** a dry run evaluated with a policy's rules reaches classified content that no rule selects
- **THEN** the export holds one item for it, with outcome `unselected`, its facts, and no destination
- **AND** it is counted under that outcome

#### Scenario: Named destinations select nothing by rule
- **WHEN** a user exports a dry run that named its destinations
- **THEN** its items name no rules, its rule counts are empty, and no item is `unselected`

### Requirement: Runtime throughput adjustment

The control plane SHALL allow a user to view and adjust the concurrency and rate limits governing each stage of work while the system is running, without redeployment. Adjusted values SHALL persist across worker restarts and SHALL NOT be overwritten by worker startup.

#### Scenario: Concurrency adjusted at runtime
- **WHEN** a user lowers the concurrency limit for a stage
- **THEN** workers apply the new limit without being restarted
- **AND** work already in progress is allowed to finish rather than being cancelled

#### Scenario: Adjustment survives a worker restart
- **WHEN** a worker restarts after a limit has been adjusted at runtime
- **THEN** the adjusted value remains in effect
- **AND** the value configured at startup does not replace it

#### Scenario: Invalid limit rejected
- **WHEN** a user submits a limit that is negative or otherwise outside the permitted range
- **THEN** the adjustment is rejected with an error identifying the permitted range
- **AND** the previously effective limit remains in force

### Requirement: Dry run against a draft configuration

A dry run SHALL accept a whole draft placement policy and, optionally, a draft template set, and SHALL evaluate with them instead of the versions in force. A draft SHALL pass the validation activating it would, taxonomy check included, when the run is requested and again when it opens. A draft SHALL NOT be stored as a configuration version. A run's status SHALL record what it evaluated with: the policy version in force, a draft, or named destinations, and whether the templates were a draft.

#### Scenario: Draft policy evaluated
- **WHEN** a user starts a dry run with a draft policy
- **THEN** the run reports the locators the draft's rules produce
- **AND** its status records that it evaluated a draft, with the draft's rule names
- **AND** the policy version in force and the policy's version history are unchanged

#### Scenario: Run with the policy in force records its version
- **WHEN** a user starts a dry run with neither a draft policy nor named destinations
- **THEN** its status records that it evaluated the policy in force, and that policy's version

#### Scenario: Invalid draft refused
- **WHEN** a user starts a dry run with a draft that activating it would refuse, including a category or subcategory the taxonomy in force does not list
- **THEN** the control plane refuses the request with 422 and each problem located
- **AND** no run starts and nothing is recorded

#### Scenario: Taxonomy changed before the run opens
- **WHEN** a draft accepted for a dry run names a category or subcategory that a taxonomy activated before the run opens no longer lists
- **THEN** the run fails with each problem located, and its status reports it as failed
- **AND** it records no header and no items, so no export claims a taxonomy version the draft was not checked against

#### Scenario: Draft policy combined with named destinations
- **WHEN** a user starts a dry run with both a draft policy and named destinations
- **THEN** the control plane refuses the request with 422, because named destinations ignore the policy's rules
- **AND** no run starts

#### Scenario: Draft documents kept with the run
- **WHEN** a user exports a dry run that evaluated a draft
- **THEN** the export includes the draft documents as evaluated, after the workflow's own history has been pruned

#### Scenario: Run rewound from its start
- **WHEN** an operator rewinds a dry run's workflow from its first step after the policy in force changed
- **THEN** its status records the policy it now evaluates with, and that it is unfinished until the re-run closes it
- **AND** its export holds only items the re-run evaluated

### Requirement: Stored configuration stays readable or fails loudly

A stored version SHALL be read with the checks a submission passes, except that values only the taxonomy constrains are not checked. A field retired from the document is dropped on read. A version in force that no longer reads SHALL NOT be evaluated in part. The failure SHALL name the kind and each problem, and a valid submission SHALL replace it. Operators SHALL be able to check every stored version before deploying a build.

#### Scenario: Stored policy with a retired field
- **WHEN** the policy in force was stored with `source_counts_as_copy` on a retention rule
- **THEN** it reads as the same policy without that field, and reconciliation and dry runs use it unchanged

#### Scenario: Version in force no longer reads
- **WHEN** the policy or template set in force names a capability profile or `media_kind` that a submission would now refuse
- **THEN** every read of it fails with an error naming the kind and each problem, and no placement or retention decision is taken from it
- **AND** a taxonomy activation is refused with 422 while that version cannot be read, so its orphans are never silently unreported
- **AND** activating a valid policy or template set replaces it without reading it

#### Scenario: Pre-deploy check
- **WHEN** an operator runs the stored configuration check from the build about to be deployed
- **THEN** each stored version that would not read is reported with its kind, version and problems
- **AND** the check exits non-zero if a version in force is among them, and zero otherwise
