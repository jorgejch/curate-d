# Spec Delta

## MODIFIED Requirements

### Requirement: Deliberate server error for alerting tests
Where enabled, the API SHALL serve `GET /__alerting_test_internal_error__`, which always fails
as an unhandled internal error: it MUST answer `500 Internal Server Error`, MUST record the
exception in the request's telemetry and the server log with a message naming it a deliberate
alerting test, and MUST NOT read or change any state. The body MUST be a `Problem` whose
`detail` is the exception's message. The route MUST NOT appear in the OpenAPI document.

#### Scenario: The request fails with a server error
- **WHEN** a client sends `GET /__alerting_test_internal_error__` to an API where the route is enabled
- **THEN** the API answers `500 Internal Server Error`
- **AND** the request's span and the server log record the exception, its message naming a deliberate alerting test

#### Scenario: The failure discloses nothing
- **WHEN** a client sends `GET /__alerting_test_internal_error__` to an API where the route is enabled
- **THEN** the response body carries the exception's message and nothing else from the server, no traceback included

#### Scenario: The response names the failure
- **WHEN** a client sends `GET /__alerting_test_internal_error__` to an API where the route is enabled
- **THEN** the response body is a `Problem` whose `detail` is the exception's message, naming a deliberate alerting test, without a traceback

#### Scenario: Other server errors stay generic
- **WHEN** any other request fails with an unhandled error on an API where the route is enabled
- **THEN** the response body is the generic internal-error answer, without the exception's message or a traceback

#### Scenario: The failure changes nothing
- **WHEN** a client sends `GET /__alerting_test_internal_error__` any number of times
- **THEN** no backend or ledger state is read or changed

#### Scenario: The route stays out of the contract
- **WHEN** a client reads `/openapi.json`
- **THEN** the document lists no `/__alerting_test_internal_error__` path
