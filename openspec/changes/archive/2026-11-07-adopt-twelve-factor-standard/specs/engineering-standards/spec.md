# Spec Delta

## Purpose

States which standards govern curate.d's development, their order of precedence and where
each one's authority lives. Keeps the record of conformance to the Twelve-Factor rules, and
of the departures from them, complete and consistent with the repository.

## ADDED Requirements

### Requirement: The standards and their precedence are declared
`openspec/config.yaml`, `CLAUDE.md` and `docs/twelve-factor.md` MUST each state the same
order of precedence, highest first: the safety guarantees, DBOS, Twelve-Factor, the coding
standards. DBOS, Twelve-Factor and the coding standards form the Standards umbrella; the
safety guarantees rank above it. A unit test MUST pin that the three files state that order.

#### Scenario: The three files agree
- **WHEN** each of the three files states the order safety guarantees, DBOS, Twelve-Factor,
  coding standards
- **THEN** the unit suite passes

#### Scenario: A file states another order
- **WHEN** one of the three files states the standards in a different order
- **THEN** the unit suite fails, naming the file and the order it found

#### Scenario: A file states no order
- **WHEN** one of the three files does not state the order of precedence
- **THEN** the unit suite fails, naming the file

### Requirement: A lower standard never overrides a higher one
Where a Twelve-Factor rule conflicts with a safety guarantee or with DBOS guidance, the
higher standard MUST prevail and the rule's row in `docs/twelve-factor.md` MUST record what
limits it. Where a coding standard conflicts with a Twelve-Factor rule, the Twelve-Factor
rule MUST prevail. A conflict MUST NOT be settled by dropping either rule without a record.

#### Scenario: DBOS guidance limits a rule
- **WHEN** a Twelve-Factor rule cannot be met without departing from the vendored skill or
  from a departure recorded in `CLAUDE.md`
- **THEN** the DBOS guidance is followed, and the rule's row has the status
  `Limited by DBOS`

#### Scenario: A safety guarantee limits a rule
- **WHEN** meeting a Twelve-Factor rule would weaken a safety guarantee
- **THEN** the guarantee holds, and the rule's row has the status `Gap` and names the
  guarantee that keeps the rule unmet

#### Scenario: A coding standard conflicts with a rule
- **WHEN** a coding standard and a Twelve-Factor rule cannot both be followed
- **THEN** the Twelve-Factor rule is followed, and the change's design records the exception
  to the coding standard

#### Scenario: A conflict has no record
- **WHEN** a change finds a rule that a higher standard limits while its row says
  `Conforms`, or a conflict that was settled with no record
- **THEN** that change corrects the row before it is archived, and no conformance is claimed
  for the rule until the row is corrected

### Requirement: The DBOS standard has one authority in the repository
The guidance of the DBOS standard MUST live in one place, the vendored skill at
`.agents/skills/dbos-python`. `.claude/skills/dbos-python` MUST be a symbolic link to
`../../.agents/skills/dbos-python`, never a second copy of the skill. A unit test MUST pin
the link and its target.

#### Scenario: The path links to the vendored skill
- **WHEN** `.claude/skills/dbos-python` is a symbolic link to
  `../../.agents/skills/dbos-python`
- **THEN** the unit suite passes

#### Scenario: The path is a copy
- **WHEN** `.claude/skills/dbos-python` is a directory
- **THEN** the unit suite fails, naming the path and that it is a directory, not a link

#### Scenario: The path is missing
- **WHEN** `.claude/skills/dbos-python` does not exist
- **THEN** the unit suite fails, naming the missing path

#### Scenario: The link points elsewhere
- **WHEN** `.claude/skills/dbos-python` is a symbolic link to any other target
- **THEN** the unit suite fails, naming the target it found

### Requirement: The Twelve-Factor text is pinned
`docs/twelve-factor.md` MUST name the manifesto's repository `twelve-factor/twelve-factor`,
the branch `next`, the full SHA of the pinned commit, the date the text was read and the
CC BY 4.0 attribution. A unit test MUST pin the repository, the branch, a full 40-character
commit SHA and the licence name.

#### Scenario: The pin is complete
- **WHEN** the adoption record names the repository, the branch, a 40-character commit SHA
  and the CC BY 4.0 licence
- **THEN** the unit suite passes

#### Scenario: The commit is abbreviated or absent
- **WHEN** the adoption record holds no 40-character commit SHA
- **THEN** the unit suite fails, naming the missing pinned commit

#### Scenario: The source or the licence is absent
- **WHEN** the adoption record does not name the repository, the branch or the licence
- **THEN** the unit suite fails, naming what is missing

### Requirement: Conformance is measured against the pinned commit
Conformance MUST be measured against the pinned commit, not against the head of `next`.
When the upstream text moves, the recorded statuses MUST stand until one change re-reads the
text, reviews every row the new text affects, records the new read date and moves the pin.

#### Scenario: Upstream moves and nothing is changed here
- **WHEN** the `next` branch gains commits after the pinned one
- **THEN** the pin and every recorded status stay as they are, and conformance is still
  stated against the pinned commit

#### Scenario: A change moves the pin
- **WHEN** a change replaces the pinned commit
- **THEN** the same change records the new read date and updates the row of every rule the
  new text adds, removes, renumbers or rewords

#### Scenario: The pin moves but the rules are not reviewed
- **WHEN** a change replaces the pinned commit and the new text has a different set of
  rules than the adoption record lists
- **THEN** the unit suite fails until the record and the pinned set of rule identifiers
  match the new text

### Requirement: Every rule has exactly one recorded status
`docs/twelve-factor.md` MUST list each of the 33 rules of the pinned text, `I.1` to `XII.2`,
exactly once, each with one status: `Conforms`, `Limited by DBOS` or `Gap`. A unit test MUST
pin the set of rule identifiers and the three statuses.

#### Scenario: All rules are listed once
- **WHEN** the adoption record lists the 33 rules once each (three each for factors I to V,
  two each for VI and VII, four each for VIII and IX, two each for X to XII), each with one
  of the three statuses
- **THEN** the unit suite passes

#### Scenario: A rule is missing
- **WHEN** a rule of the pinned text has no row
- **THEN** the unit suite fails, naming the missing rule identifiers

#### Scenario: A rule is listed twice
- **WHEN** a rule identifier appears in more than one row
- **THEN** the unit suite fails, naming the duplicated identifier

#### Scenario: A row carries an unknown identifier or status
- **WHEN** a row has an identifier outside `I.1` to `XII.2`, or a status other than
  `Conforms`, `Limited by DBOS` or `Gap`
- **THEN** the unit suite fails, naming the row and the value it found

### Requirement: A rule limited by DBOS names the guidance that limits it
Each `Limited by DBOS` row MUST name the reference file of the vendored skill that limits
the rule, and that file MUST exist under `.agents/skills/dbos-python`. A unit test MUST pin
both.

#### Scenario: The named reference exists
- **WHEN** every `Limited by DBOS` row names a reference file that exists in the vendored
  skill
- **THEN** the unit suite passes

#### Scenario: A row names no reference
- **WHEN** a `Limited by DBOS` row names no reference file
- **THEN** the unit suite fails, naming the rule

#### Scenario: A skill refresh removes or renames the reference
- **WHEN** a `Limited by DBOS` row names a file that the vendored skill no longer holds
- **THEN** the unit suite fails, naming the rule and the missing file, until the row names
  an existing reference or the rule is given another status

### Requirement: A gap states what is unmet and is closed by a change
Each `Gap` row MUST state what is unmet. A rule MUST leave the status `Gap` only in the
change that brings it into conformance or records the standard that limits it, and that
change MUST update the row. A unit test MUST pin that no `Gap` row lacks its statement.

#### Scenario: Every gap is described
- **WHEN** every `Gap` row states what is unmet
- **THEN** the unit suite passes

#### Scenario: A gap has no statement
- **WHEN** a `Gap` row states nothing about what is unmet
- **THEN** the unit suite fails, naming the rule

#### Scenario: A change closes a gap
- **WHEN** a change brings a `Gap` rule into conformance
- **THEN** the same change sets the rule's row to `Conforms`

#### Scenario: A row and the repository disagree
- **WHEN** a change brings a rule into conformance and leaves its row at `Gap`, or sets a
  row to `Conforms` without the work that meets the rule
- **THEN** the change is not archived until the row states what the change did

### Requirement: A change's design names the factor rules it affects
`openspec/config.yaml` MUST carry a design rule: the design of a change names every
Twelve-Factor rule the change departs from or brings into conformance, and the change
updates that rule's row in `docs/twelve-factor.md`. A unit test MUST pin that the design
rule is present.

#### Scenario: The design rule is present
- **WHEN** the design rules in `openspec/config.yaml` include the rule about Twelve-Factor
  rules
- **THEN** the unit suite passes

#### Scenario: The design rule is removed
- **WHEN** `openspec/config.yaml` holds no design rule about Twelve-Factor rules
- **THEN** the unit suite fails, naming the missing design rule

#### Scenario: A change affects a rule
- **WHEN** a change departs from a Twelve-Factor rule or brings one into conformance
- **THEN** its design names the rule by its identifier and the change updates the rule's row

#### Scenario: A design omits a rule the change affects
- **WHEN** review finds that a change affects a Twelve-Factor rule its design does not name
- **THEN** the change is returned, and it is not archived until the design names the rule
  and the row is updated

#### Scenario: A change affects no rule
- **WHEN** a change neither departs from a Twelve-Factor rule nor brings one into
  conformance
- **THEN** its design need not mention the rules, and the adoption record is not changed
