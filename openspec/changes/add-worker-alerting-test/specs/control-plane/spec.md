# Spec Delta

## ADDED Requirements

### Requirement: Deliberate worker failure for alerting tests
Where the alerting test routes are served, the API SHALL serve
`POST /__alerting_test_worker_error__/{profile}`, `profile` being `io` or `cpu`. It MUST
enqueue one workflow that only a worker of that profile dequeues, and answer `202 Accepted`
naming the profile, the queue and the workflow ID. Requests for one profile within the same
UTC minute MUST share one workflow. The route MUST NOT appear in the OpenAPI document.

#### Scenario: The request is accepted for the chosen worker
- **WHEN** a client sends `POST /__alerting_test_worker_error__/io` to an API where the alerting test routes are served
- **THEN** the API answers `202 Accepted` naming the `io` profile, its queue and the workflow ID

#### Scenario: Each profile is tested on its own
- **WHEN** a client sends `POST /__alerting_test_worker_error__/cpu`
- **THEN** the workflow is enqueued on a queue only the CPU profile dequeues

#### Scenario: Repeats within a minute fail once
- **WHEN** a client sends the same profile's request several times within one UTC minute
- **THEN** every answer names the same workflow ID and the worker runs it once

#### Scenario: An unknown profile is rejected
- **WHEN** a client sends `POST /__alerting_test_worker_error__/gpu`
- **THEN** the API answers `422` and nothing is enqueued

#### Scenario: Not served, nothing is enqueued
- **WHEN** a client sends `POST /__alerting_test_worker_error__/io` to an API where the alerting test routes are not served
- **THEN** the API answers `404 Not Found` and nothing is enqueued

#### Scenario: The route stays out of the contract
- **WHEN** a client reads `/openapi.json`
- **THEN** the document lists no `/__alerting_test_worker_error__/{profile}` path

### Requirement: The deliberate worker failure fails as a worker bug would
A worker that dequeues the deliberate failure SHALL fail it as an unhandled error: its
workflow and step spans MUST end with an error status, the exception MUST be logged at ERROR
with a message naming a deliberate worker alerting test, and the workflow MUST end `ERROR`,
all exported under that worker's service name. It MUST NOT read or change ledger state.

#### Scenario: The worker's telemetry records the failure
- **WHEN** an I/O worker dequeues the deliberate failure
- **THEN** the workflow ends `ERROR`
- **AND** its workflow and step spans are exported at error level under `curate-d-worker-io`
- **AND** its exception is logged at ERROR, naming a deliberate worker alerting test

#### Scenario: The failure changes no ledger state
- **WHEN** a worker fails the deliberate failure any number of times
- **THEN** no ledger row is read or changed; only the workflow's own row is written
