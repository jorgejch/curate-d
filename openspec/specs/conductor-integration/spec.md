# conductor-integration Specification

## Purpose

Optionally connects workers to DBOS Conductor, read-only, for workflow observability, without making workflow execution, recovery, retention or auditing depend on Conductor being configured, reachable or licensed.

## Requirements

### Requirement: Conductor is opt-in by configuration

The system SHALL connect to Conductor only when the Conductor feature flag is enabled and a Conductor key is configured. The flag SHALL default to disabled. A disabled flag SHALL keep Conductor off regardless of any key, URL or DBOS environment variable. A configured key SHALL be treated as a secret: it SHALL NOT appear in logs, error messages or settings dumps.

#### Scenario: Flag disabled, key configured
- **WHEN** a worker starts with the flag disabled or unset and a Conductor key configured
- **THEN** no Conductor connection is attempted
- **AND** the worker logs once that Conductor is disabled by the flag

#### Scenario: Flag enabled, no key
- **WHEN** a worker starts with the flag enabled and the Conductor key setting unset
- **THEN** no Conductor connection is attempted
- **AND** the worker logs a `conductor.misconfigured` error once, naming the missing key setting
- **AND** the worker launches and executes workflows normally

#### Scenario: Flag enabled, no environment
- **WHEN** a worker starts with the flag and key set and `CURATE_D_ENVIRONMENT` unset
- **THEN** no Conductor connection is attempted: the worker runs DBOS as `curate-d-local`, and a deployment that forgot its environment is not connected under another environment's application
- **AND** the worker logs a `conductor.misconfigured` error once, naming `CURATE_D_ENVIRONMENT`
- **AND** the worker launches and executes workflows normally

#### Scenario: Flag and key supplied through configuration
- **WHEN** a worker of either profile is deployed
- **THEN** the flag and the key are supplied through configuration, and a missing key leaves the worker startable with Conductor disabled
- **AND** the key is not required for any other function

#### Scenario: Conductor reachable
- **WHEN** Conductor is enabled
- **THEN** the worker can open an outbound HTTPS/WebSocket connection to the Conductor endpoint, directly or through an optional configured proxy

#### Scenario: Proxy configured
- **WHEN** a worker starts with Conductor enabled and a Conductor proxy URL configured
- **THEN** its Conductor connection, and nothing else in the process, goes through that proxy

#### Scenario: Proxy unreachable or refusing
- **WHEN** the configured proxy is down or refuses the connection
- **THEN** the worker keeps executing workflows, and retries the Conductor connection with backoff, logging a `conductor.*` error per attempt

#### Scenario: DBOS's own variable does not enable Conductor
- **WHEN** a worker starts with `DBOS_CONDUCTOR_KEY` set in its environment but the curate.d flag disabled or no curate.d key
- **THEN** no Conductor connection is attempted

#### Scenario: Flag enabled and key configured
- **WHEN** a worker of either profile starts with the flag enabled and a Conductor key configured
- **THEN** it connects to the configured Conductor URL, or to the hosted Conductor if no URL is configured, under the application name `curate-d-<environment>`, the deployment environment from `CURATE_D_ENVIRONMENT` (for example `curate-d-dev`), the same name DBOS runs under and owns its workflows, queues and schedules by, so the Console lists that environment's workflows and each environment is a separate Conductor application
- **AND** the key appears in no log line or error message

#### Scenario: Malformed Conductor URL
- **WHEN** the configured Conductor URL is not a `ws://` or `wss://` URL, or the configured proxy URL is not an `http://` URL
- **THEN** settings validation fails and the worker does not start, with an error naming the setting

### Requirement: Every worker reports, only after launch

Every worker process, on both the I/O and the CPU profile, SHALL connect to Conductor when Conductor is enabled, so that every DBOS executor is visible to Conductor. A worker SHALL connect only after it holds its executor lease and DBOS has launched. The API process is not a DBOS executor and SHALL NOT connect, even when the flag and a key are present in its environment. A worker's Conductor connection SHALL close when the worker shuts down.

#### Scenario: Connection follows lease and launch
- **WHEN** a worker of either profile with the flag enabled and a Conductor key starts
- **THEN** it takes its executor lease, launches DBOS, and only then opens the Conductor connection

#### Scenario: Both profiles reported
- **WHEN** Conductor is enabled and I/O and CPU workers are running
- **THEN** Conductor lists every worker as an executor under its stable executor identity, with its profile in the executor metadata

#### Scenario: API given a key
- **WHEN** the API process starts with the flag enabled and a Conductor key in its environment
- **THEN** no Conductor connection is attempted

#### Scenario: Launch fails
- **WHEN** DBOS launch or lease acquisition fails
- **THEN** no Conductor connection is opened

#### Scenario: Shutdown
- **WHEN** a connected worker shuts down
- **THEN** its Conductor connection is closed before the process exits

### Requirement: Workflow execution is independent of Conductor

Executor identity, startup recovery, the recovery sweep, workflow-history retention and queue behaviour SHALL be identical whether Conductor is enabled, disabled, unreachable, or rejecting the key. Each worker SHALL report the same stable executor identity to Conductor that it uses for recovery.

#### Scenario: Identity unchanged by Conductor
- **WHEN** a worker starts with Conductor enabled
- **THEN** its DBOS executor identity is the stable configured identity, not a generated one
- **AND** it recovers its own pending workflows at startup exactly as it does without Conductor

#### Scenario: Conductor unreachable at startup
- **WHEN** a worker starts with Conductor enabled and Conductor cannot be reached
- **THEN** the worker launches, becomes ready and executes workflows
- **AND** it keeps retrying the connection in the background

#### Scenario: Key rejected
- **WHEN** Conductor rejects the configured key, as it does after a trial expires or while the key is still a provisioning placeholder
- **THEN** the worker keeps executing workflows and stays ready
- **AND** it logs one `conductor.rejected` error per connection attempt, with attempts backing off until they are no more frequent than once every five minutes

#### Scenario: Connection lost mid-run
- **WHEN** a connected worker loses its Conductor connection
- **THEN** in-flight workflows continue unaffected
- **AND** the worker reconnects when Conductor becomes reachable again

### Requirement: Disabling Conductor is a configuration change only

Disabling the flag and restarting the workers SHALL fully disable Conductor, with the key still configured, with no data migration and no stranded workflows. Removing the key SHALL be a separate, optional step.

#### Scenario: Disabled after use
- **WHEN** the flag is disabled, the key left in place and the workers restarted, while workflows are pending
- **THEN** those workflows are recovered and completed by the workers exactly as without Conductor
- **AND** no Conductor connection is attempted

### Requirement: Conductor is read-only

Every message received from Conductor SHALL be classified as read-only or refused. Read-only messages (executor information, workflow, step, event, notification, stream and aggregate reads, metrics, alerts, workflow export, and queue, schedule and application-version listings) SHALL be passed to DBOS's own handler. Every message type that would change workflow, schedule, application-version or history state SHALL be refused: cancel, resume, schedule pause, resume, trigger and backfill, delete, fork, fork-from-failure, rewind, latest-version promotion, recovery, retention and workflow import. A message type the system does not recognise SHALL be refused, never executed. A refusal's error response SHALL name the type as refused by curate.d and say that Conductor is read-only in curate.d, adding the type's own reason where it has one. A refused message SHALL leave workflow, schedule and version state unchanged, SHALL enqueue no workflow and write no audit entry, and SHALL be logged with its type and request identifier. Workflow writes stay available outside Conductor through DBOS's own client and CLI.

#### Scenario: Read-only command
- **WHEN** Conductor requests executor information, a workflow listing, workflow steps, metrics, queue state, schedules, application versions or a workflow export
- **THEN** the response is returned and no workflow or schedule state changes

#### Scenario: Workflow or schedule write
- **WHEN** Conductor requests a cancel, a resume, or a schedule pause, resume or trigger
- **THEN** the system responds with an error naming the type as refused by curate.d and saying Conductor is read-only in curate.d
- **AND** no workflow or schedule state changes, no workflow is enqueued, and no audit entry is written

#### Scenario: Write delivered to every executor
- **WHEN** Conductor delivers one Console write to each connected executor in turn, each copy under its own request identifier
- **THEN** every executor refuses its copy
- **AND** nothing changes, however many executors are connected

#### Scenario: Other writes
- **WHEN** Conductor requests a delete, fork, fork-from-failure, rewind, latest-version promotion or schedule backfill
- **THEN** the system responds with an error naming the type as refused by curate.d and saying Conductor is read-only in curate.d
- **AND** no workflow, schedule or version state changes, no workflow is enqueued, and no audit entry is written

#### Scenario: Recovery
- **WHEN** Conductor requests recovery of one or more executors
- **THEN** the system responds with an error naming the type as refused by curate.d
- **AND** none of those executors' pending workflows are re-enqueued, and no audit entry is written

#### Scenario: Recovery requested again
- **WHEN** Conductor requests recovery again of executors whose recovery the connection already refused
- **THEN** the system responds with the same refusal
- **AND** the refusal is logged at debug rather than as another warning

#### Scenario: Unknown command type
- **WHEN** Conductor sends a command type the system does not classify
- **THEN** the system responds with an error naming the type
- **AND** no workflow or schedule state changes

### Requirement: Retention stays application-owned

The system SHALL refuse Conductor retention and global-timeout requests. Workflow-history pruning SHALL follow only the configured retention period, and no workflow SHALL be cancelled by a Conductor-supplied age limit.

#### Scenario: Retention request refused
- **WHEN** Conductor sends a retention request with a cutoff or row threshold
- **THEN** no workflow history is deleted and no workflow is cancelled
- **AND** a warning names the refused request

### Requirement: Workflow import is refused

The system SHALL refuse Conductor workflow-import requests without decoding their payload. A refused import SHALL leave the workflow tables unchanged. The payload is a serialized Python object, and decoding it would execute code supplied from outside the cluster. Workflow export SHALL remain available as a read-only command.

#### Scenario: Import request refused
- **WHEN** Conductor sends a workflow-import request
- **THEN** Conductor receives an error response naming the refused command type
- **AND** the payload is never decoded, no workflow row is created, and a warning names the request
