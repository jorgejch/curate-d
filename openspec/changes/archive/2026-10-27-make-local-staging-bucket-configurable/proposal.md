# Proposal

## Why

`fix-rustfs-staging-fixed-bucket` (same PR, #114) pinned the local stack to bucket
`curate-d-staging` and prefix `content/`, because the RustFS policies granted those literally
while `compose.yaml` let `CURATE_D_STAGING_S3_BUCKET` and
`CURATE_D_STAGING_S3_CONTENT_PREFIX` override the workers and `rustfs-init` only. The
maintainer has reversed that decision: the local bucket and prefix are to be configurable by
environment variable again. The original defect, provisioning one bucket while the policies
grant another, must stay impossible.

## What Changes

- `compose.yaml`: the workers' bucket and prefix are `${VAR:-default}` again, and
  `rustfs-init` gets `RUSTFS_STAGING_BUCKET` and `RUSTFS_STAGING_PREFIX` from the same two
  variables with the same defaults.
- The RustFS policies in `infrastructure/rustfs/policies/` become templates
  (`@BUCKET@`, `@PREFIX@`, `@PREFIX_KEY@`). New `infrastructure/rustfs/render_policy.sh`
  renders one for a bucket and prefix, after validating both. `initialize.sh` renders both
  policies before it creates anything and attaches the rendered files. The `jq` derivation
  from the previous change is removed.
- `tests/unit/infrastructure/test_rustfs_provisioning.py` pins the shared variables and
  defaults, runs the render script, and checks rendered grants and the rejection of unusable
  values.
- `infrastructure/README.md` describes the variables.

Not changed: `staging_s3_bucket` and `staging_s3_content_prefix` in `config.py`, and OVH.

## Capabilities

### Modified Capabilities

- `content-staging`: MODIFIED "The local staging bucket and prefix are fixed by the RustFS
  policies" becomes "The local staging bucket and prefix are configurable and rendered into the
  RustFS policies".

## Impact

- `compose.yaml`, `infrastructure/rustfs/initialize.sh`, new `render_policy.sh`, the two
  policy files (now templates), `infrastructure/README.md`,
  `openspec/specs/content-staging/spec.md`, the unit test.
- The policy files are no longer valid for a direct `rc admin policy create`; only
  `rustfs-init` consumes them.
- Supersedes the previous change's requirement; both are archived in the same PR.
- This change cannot be split across agents: every task touches the same few files.
