# Tasks

One stream: every task touches `compose.yaml` or reads it, so the change does not split.

## 1. Pin the local bucket and prefix

- [x] 1.1 Add `tests/unit/infrastructure/test_rustfs_provisioning.py`. It pins the workers'
  compose bucket and prefix as literals equal to the policies' grants, and fails if
  `rustfs-init` takes a bucket or prefix input. Verify it fails against the current
  `compose.yaml` (`uv run pytest tests/unit/infrastructure/test_rustfs_provisioning.py`).
- [x] 1.2 In `compose.yaml`, set `CURATE_D_STAGING_S3_BUCKET` and
  `CURATE_D_STAGING_S3_CONTENT_PREFIX` to literals, remove `RUSTFS_STAGING_BUCKET` and
  `RUSTFS_STAGING_PREFIX` from `rustfs-init`, and add the signing region to the `rustfs`
  service comment. Make `initialize.sh` derive `BUCKET` and `PREFIX` from the policies and
  fail on a missing or mismatched grant. Verify that 1.1's test passes and that
  `make test-stack` runs `rustfs-init` to its "ready" line.
- [x] 1.3 Update `infrastructure/README.md`: say the local bucket and prefix are fixed by the
  policies, and keep the OVH example's `$STAGING_BUCKET`. Verify the RustFS smoke example's
  values match `compose.yaml`.

## 2. Verification

- [x] 2.1 `make check` is green.
- [x] 2.2 With the stack lock held: `make test-stack`, `make staging-init`, and
  `CURATE_D_STAGING_S3_BUCKET=other CURATE_D_STAGING_S3_CONTENT_PREFIX=x/ make staging-init`
  all pass the access-matrix check. Then
  `uv run pytest tests/integration/test_staging_backend_checks.py tests/integration/test_staging_contract.py --no-cov -m integration --staging-backend=rustfs`
  is green.

## Workflow follow-up

- Sync the delta specs into `openspec/specs/`, archive the change, and run
  `uv run openspec validate --all --strict`.
