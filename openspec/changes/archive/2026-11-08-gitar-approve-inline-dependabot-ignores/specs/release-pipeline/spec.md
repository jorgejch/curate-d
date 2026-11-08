## ADDED Requirements

### Requirement: The auto-approve policy carries its own copy of the ignored updates
The auto-approve policy MUST list every update an `ignore` rule in `.github/dependabot.yml`
excludes, by ecosystem, dependency and update type, and MUST direct that the ignored-update
condition is judged from that list, so a review that cannot read `.github/dependabot.yml` neither
refuses on that account nor misses an ignored update. The unit suite MUST fail when the list and
the file's `ignore` rules differ.

#### Scenario: dependabot.yml not readable in the review
- **WHEN** an otherwise eligible Dependabot update moves the `astral-sh/uv` image from `0.13.6`
  to `0.13.7`, and Gitar's review cannot read `.github/dependabot.yml`
- **THEN** the policy's own list decides: it names no `astral-sh/uv` rule, so the update is not
  ignored and the policy allows approval, provided the organization criteria allow it too

#### Scenario: Patch of a dependency whose minors are ignored
- **WHEN** a `python-patches` pull request moves `aioboto3` from `15.5.0` to `15.5.1`
- **THEN** the update is not ignored, since only `aioboto3` minors are listed

#### Scenario: Ignore rule changed without the policy
- **WHEN** `.github/dependabot.yml` gains or changes an `ignore` rule and the policy's list does
  not change with it
- **THEN** the unit suite fails
