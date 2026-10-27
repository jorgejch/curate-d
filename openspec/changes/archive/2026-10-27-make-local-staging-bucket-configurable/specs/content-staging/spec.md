## RENAMED Requirements

- FROM: `### Requirement: The local staging bucket and prefix are fixed by the RustFS policies`
- TO: `### Requirement: The local staging bucket and prefix are configurable and rendered into the RustFS policies`

## MODIFIED Requirements

### Requirement: The local staging bucket and prefix are configurable and rendered into the RustFS policies
The local stack SHALL stage in the bucket and prefix named by `CURATE_D_STAGING_S3_BUCKET` and
`CURATE_D_STAGING_S3_CONTENT_PREFIX`, defaulting to `curate-d-staging` and `content/`. The
workers and local provisioning MUST read both variables with the same defaults, and
provisioning MUST render the RustFS policies for them, granting each identity exactly the
workers' prefix. An unusable bucket or prefix MUST fail provisioning before a bucket is
created. OVH environments set their own.

#### Scenario: A shell variable names another bucket or prefix
- **WHEN** `CURATE_D_STAGING_S3_BUCKET` or `CURATE_D_STAGING_S3_CONTENT_PREFIX` is exported and the local stack is provisioned
- **THEN** provisioning creates that bucket, grants both identities that prefix, passes its access-matrix check, and the workers stage under it

#### Scenario: The variables are unset
- **WHEN** neither variable is set
- **THEN** the stack uses `curate-d-staging` and `content/`

#### Scenario: A value cannot be rendered safely
- **WHEN** the bucket is outside S3 naming rules, or the prefix is empty, rooted, does not end in `/` or uses characters outside `A-Za-z0-9._/-`
- **THEN** provisioning fails naming the value and creates no bucket

#### Scenario: Compose drifts between workers and provisioning
- **WHEN** a worker's or `rustfs-init`'s bucket or prefix input differs from the shared variable and default
- **THEN** the unit suite fails naming the setting
