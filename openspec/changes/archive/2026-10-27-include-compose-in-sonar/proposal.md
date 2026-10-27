# Proposal

## Why

The explicit Sonar scope omits the main Docker Compose definition. Include it without widening analysis to unrelated root files.

## What Changes

- Add `compose.yaml` to source and text/secret scope and exclude it from coverage.
- Update scope tests and contributor documentation; preserve explicit scope and unit-only coverage.
- Leave `compose.lifecycle.yaml` outside scope; the request names the main Compose file.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `release-pipeline`: Include the main Compose definition in the explicit Sonar source contract.

## Impact

Changes affect `sonar-project.properties`, its unit tests, contributor guidance and the release-pipeline specification. No application, dependency or Compose runtime changes.