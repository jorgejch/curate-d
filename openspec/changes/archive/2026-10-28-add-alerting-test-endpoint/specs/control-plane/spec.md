# Spec Delta

## ADDED Requirements

### Requirement: Deliberate server error for alerting tests
Where enabled, the API SHALL serve `GET /__alerting_test_internal_error__`, which always fails
as an unhandled internal error: it MUST answer `500 Internal Server Error`, MUST record the
exception in the request's telemetry and the server log with a message naming it a deliberate
alerting test, and MUST NOT read or change any state. The response body MUST NOT carry the
exception's message. The route MUST NOT appear in the OpenAPI document.

#### Scenario: The request fails with a server error
- **WHEN** a client sends `GET /__alerting_test_internal_error__` to an API where the route is enabled
- **THEN** the API answers `500 Internal Server Error`
- **AND** the request's span and the server log record the exception, its message naming a deliberate alerting test

#### Scenario: The failure discloses nothing
- **WHEN** a client sends `GET /__alerting_test_internal_error__` to an API where the route is enabled
- **THEN** the response body is the generic internal-error answer, without the exception's message or a traceback

#### Scenario: The failure changes nothing
- **WHEN** a client sends `GET /__alerting_test_internal_error__` any number of times
- **THEN** no backend or ledger state is read or changed

#### Scenario: The route stays out of the contract
- **WHEN** a client reads `/openapi.json`
- **THEN** the document lists no `/__alerting_test_internal_error__` path

### Requirement: Alerting test routes served per environment
The API SHALL serve the alerting test routes whenever `CURATE_D_ENVIRONMENT` is `local` or
`dev`. When it is `prod` or unset, it MUST serve them only if
`CURATE_D_ALERTING_TEST_ROUTES_FORCE_ON_IN_PROD` is `true`, which defaults to `false` and has
no effect in `local` or `dev`. Where the routes are not served, their paths MUST answer
`404 Not Found` like any unknown path. A non-boolean value MUST stop the API at settings load.

#### Scenario: Always on in local and dev
- **WHEN** the API starts with `CURATE_D_ENVIRONMENT` `local` or `dev`, and `CURATE_D_ALERTING_TEST_ROUTES_FORCE_ON_IN_PROD` unset, `true` or `false`
- **THEN** `GET /__alerting_test_internal_error__` answers 500

#### Scenario: Off by default in prod and when no environment is named
- **WHEN** the API starts with `CURATE_D_ENVIRONMENT` `prod`, or unset, and `CURATE_D_ALERTING_TEST_ROUTES_FORCE_ON_IN_PROD` unset or `false`
- **THEN** `GET /__alerting_test_internal_error__` answers 404

#### Scenario: Forced on in prod
- **WHEN** the API starts with `CURATE_D_ENVIRONMENT` `prod`, or unset, and `CURATE_D_ALERTING_TEST_ROUTES_FORCE_ON_IN_PROD=true`
- **THEN** `GET /__alerting_test_internal_error__` answers 500

#### Scenario: An invalid value stops the API
- **WHEN** the API starts with `CURATE_D_ALERTING_TEST_ROUTES_FORCE_ON_IN_PROD=sometimes`
- **THEN** settings fail to load and the API does not start
