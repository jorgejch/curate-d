# Proposal

## Why

The RustFS reader and writer policies (`infrastructure/rustfs/policies/*.json`) grant the
literal bucket and prefix `curate-d-staging/content`, but `compose.yaml` lets
`CURATE_D_STAGING_S3_BUCKET` and `CURATE_D_STAGING_S3_CONTENT_PREFIX` override both, in the
workers' shared staging block and in `rustfs-init`'s `RUSTFS_STAGING_BUCKET` and
`RUSTFS_STAGING_PREFIX`. Exporting either variable in the shell makes `rustfs-init` create a
bucket the policies do not grant, so its access-matrix check fails and the stack never comes
up. PR #109's review found this and left it unfixed.

## What Changes

- The local stack always stages in bucket `curate-d-staging` under prefix `content/`.
  `compose.yaml` sets both to literals for the workers, not to `${VAR:-default}`, so a shell
  variable can no longer move them away from the policies.
- `rustfs-init` no longer takes the bucket and prefix as inputs. `initialize.sh` reads them
  from the policies it attaches, and fails naming the cause when the two policies disagree
  or carry no single prefix grant.
- A unit test fails when the workers' compose bucket or prefix differs from what the policies
  grant.
- The comment on the `rustfs` service lists the signing region among the settings that differ
  from OVH (`us-east-1` locally, `us-east-va` on OVH).
- `infrastructure/README.md` states that the local bucket and prefix are fixed. The OVH smoke
  example keeps `$STAGING_BUCKET`.

Not changed: `staging_s3_bucket` and `staging_s3_content_prefix` in `src/curate_d/config.py`
stay configurable, because each OVH environment sets its own. Only the local compose stack and
its RustFS provisioning are pinned.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `content-staging`: ADDED "The local staging bucket and prefix are fixed by the RustFS
  policies". No existing requirement's text changes.

## Impact

- `compose.yaml`, `infrastructure/rustfs/initialize.sh`, `infrastructure/README.md`,
  `openspec/specs/content-staging/spec.md` (append only).
- New `tests/unit/infrastructure/test_rustfs_provisioning.py`.
- Developers who exported `CURATE_D_STAGING_S3_BUCKET` or `CURATE_D_STAGING_S3_CONTENT_PREFIX`
  for the local stack (which never passed provisioning) now get the fixed values. The
  integration fixtures' `CURATE_D_TEST_RUSTFS_BUCKET` defaults already match.
