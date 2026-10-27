## ADDED Requirements

### Requirement: The local staging bucket and prefix are fixed by the RustFS policies
The local stack SHALL stage in the one bucket and prefix both RustFS policies grant,
`curate-d-staging` and `content/`. Local provisioning MUST read both from the policies and
take neither as input, and the workers' compose values MUST be literals equal to that grant.
A policy without exactly one prefix grant, or two policies granting different ones, MUST fail
provisioning before it creates a bucket. OVH environments keep setting their own.

#### Scenario: A shell variable names another bucket or prefix
- **WHEN** `CURATE_D_STAGING_S3_BUCKET` or `CURATE_D_STAGING_S3_CONTENT_PREFIX` is exported and the local stack is provisioned
- **THEN** provisioning creates `curate-d-staging`, passes its access-matrix check, and the workers stage under `content/`

#### Scenario: The policies disagree
- **WHEN** the reader and writer policies grant different prefixes, or one grants none or several
- **THEN** provisioning fails naming the policy files and creates no bucket

#### Scenario: Compose drifts from the policies
- **WHEN** a worker's compose bucket or prefix is interpolated or differs from the policies' grant, or provisioning is given a bucket or prefix input
- **THEN** the unit suite fails naming the setting
