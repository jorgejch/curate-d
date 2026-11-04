# Spec Delta

## ADDED Requirements

### Requirement: The integration suite runs locally across parallel isolated stacks
`make test-integration-parallel` MUST run `SPLITS` shards at once. Each shard MUST have its own
Compose project, loopback ports and data, and run CI's shard command serially against its own
stack. Every shard MUST run to completion, and the target MUST fail if any shard fails, naming
it. It MUST remove every shard stack it started, MUST NOT touch the default project, and MUST
refuse an invalid `SPLITS` before any stack starts.

#### Scenario: Every shard passes
- **WHEN** a developer runs `make test-integration-parallel` with `SPLITS` unset
- **THEN** four shard stacks start, each runs the tests CI's shard of the same number runs, and the target exits 0
- **AND** no shard stack, volume or network remains afterwards

#### Scenario: One shard fails
- **WHEN** a test fails in shard 2 and shards 1, 3 and 4 pass
- **THEN** shards 1, 3 and 4 still run to completion
- **AND** the target exits non-zero, naming shard 2 and the path of its log and JUnit report

#### Scenario: A shard stack does not start
- **WHEN** a shard's PostgreSQL, RustFS or rclone does not reach healthy, or its port is already bound
- **THEN** that shard fails before its migrations, naming the shard and the failed step
- **AND** the other shards still run, and the target exits non-zero

#### Scenario: The default stack is left alone
- **WHEN** a `make stack-up` stack is running and the developer runs `make test-integration-parallel`
- **THEN** the default stack's containers, data and published ports are the same afterwards

#### Scenario: Shard count set
- **WHEN** the developer runs `make test-integration-parallel SPLITS=2`
- **THEN** two shard stacks start, with `--splits 2` and groups 1 and 2

#### Scenario: Shard count invalid
- **WHEN** `SPLITS` is `0`, `abc`, `2.5` or `33`
- **THEN** the target fails naming `SPLITS` and the accepted range 1 to 32, and no stack starts

#### Scenario: Interrupted run
- **WHEN** the developer interrupts the target while shards run
- **THEN** the running test processes stop, every shard stack it started is removed, and the target exits non-zero
