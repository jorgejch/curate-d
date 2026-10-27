# Tasks

One stream: the compose file, `initialize.sh`, the templates and the test all depend on the
same variable and placeholder names, so the change does not split.

## 1. Make the bucket and prefix configurable

- [x] 1.1 Template the two policies and add `infrastructure/rustfs/render_policy.sh`.
- [x] 1.2 Restore the `${VAR:-default}` bucket and prefix in `compose.yaml` for the workers and
  `rustfs-init`; mount `render_policy.sh`; render the policies in `initialize.sh`.
- [x] 1.3 Rewrite `tests/unit/infrastructure/test_rustfs_provisioning.py`.
- [x] 1.4 Update `infrastructure/README.md`.
- [x] 1.5 Point the RustFS integration tests at the provisioned bucket and prefix: the same
  variables and defaults as compose, `CURATE_D_TEST_RUSTFS_*` over them, and an INFO
  `integration.rustfs_location_overridden` log when they are not the defaults
  (`tests/integration/rustfs_staging.py`).

## 2. Verification

- [x] 2.1 `make check` is green.
- [x] 2.2 `make test-stack` and `rustfs-init` pass the access-matrix check on the defaults and
  with `CURATE_D_STAGING_S3_BUCKET`/`CURATE_D_STAGING_S3_CONTENT_PREFIX` overridden; an
  unusable bucket or prefix fails before a bucket is created. The RustFS integration tests
  pass on both.

## Workflow follow-up

- [x] Sync the delta spec into `openspec/specs/` and archive the change.
- [x] `uv run openspec validate --all --strict`.
