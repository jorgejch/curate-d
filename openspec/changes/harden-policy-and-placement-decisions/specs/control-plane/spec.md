# Spec Delta

## MODIFIED Requirements

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

### Requirement: Dry-run result export

The control plane SHALL expose the results of a dry run in a form suitable for reviewing proposed organization in bulk. The export SHALL include the facts derived for each content object, the proposed locator, and any items that failed classification or rendering. When the run evaluated a policy's rules, each proposed destination SHALL name the rules that selected it, each by its position in the policy and its name. Every rule of the evaluated policy SHALL be counted, apart from any other rule of the same name, and classified content that no rule selects SHALL be reported.

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

## ADDED Requirements

### Requirement: Dry run against a draft configuration

A dry run SHALL accept a whole draft placement policy and, optionally, a draft template set, and SHALL evaluate with them instead of the versions in force. A draft SHALL pass the same validation as activating it, including the taxonomy check. A draft SHALL NOT be stored as a configuration version. A run's status SHALL record what it evaluated with: the policy version in force, a draft, or named destinations, and whether the templates were a draft.

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
