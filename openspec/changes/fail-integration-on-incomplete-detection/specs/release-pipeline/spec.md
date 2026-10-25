# Spec Delta

## MODIFIED Requirements

### Requirement: Uncertain change detection fails the integration job
The plan MUST fail when the changed files cannot be determined or may be incomplete, and
`Integration tests` MUST fail with it. A pull request that changes 3,000 files or more, or
whose changed-file count is missing, MUST fail the plan whether the detection selected the
suite or not.

#### Scenario: Change detection fails
- **WHEN** the step that lists the pull request's changed files fails, or reports a
  value other than a match or no match
- **THEN** the plan fails before any later step, naming the detection, no shard or boot
  job starts, `Integration tests` fails, and the pull request cannot merge

#### Scenario: Changed-file list may be truncated
- **WHEN** the pull request changes 3,000 files or more, or its changed-file count is
  missing, whether or not the detection finds a declared path
- **THEN** the plan fails before any later step, naming the file count and telling the
  author to split the pull request, no shard or boot job starts, `Integration tests` fails,
  and the pull request cannot merge

#### Scenario: Truncated list selects the suite
- **WHEN** the detection finds a declared path and the pull request changes 3,000 files or
  more
- **THEN** the plan fails before any later step, the suite does not run, and
  `Integration tests` fails

#### Scenario: A packaging file is removed
- **WHEN** the detection finds no declared path and `README.md`, `LICENSE` or `NOTICE` is
  missing from the checkout
- **THEN** the plan fails before any later step, naming the file, and `Integration tests`
  fails
