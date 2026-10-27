# Tasks

One stream: the compose file, `initialize.sh`, the templates and the test all depend on the
same variable and placeholder names, so the change does not split.

## 1. Make the bucket and prefix configurable

- [x] 1.1 Template the two policies and add `infrastructure/rustfs/render_policy.sh`.
- [x] 1.2 Restore the `${VAR:-default}` bucket and prefix in `compose.yaml` for the workers and
  `rustfs-init`; mount `render_policy.sh`; render the policies in `initialize.sh`.
- [x] 1.3 Rewrite `tests/unit/infrastructure/test_rustfs_provisioning.py`.
- [x] 1.4 Update `infrastructure/README.md`.

## 2. Verification

- [ ] 2.1 `make check` is green. (Not run by the agent: command execution was not permitted.)
- [ ] 2.2 `make test-stack`, `make staging-init`, and
  `CURATE_D_STAGING_S3_BUCKET=other CURATE_D_STAGING_S3_CONTENT_PREFIX=x/ make staging-init`
  pass the access-matrix check. (Not run by the agent.)

## Workflow follow-up

- [x] Sync the delta spec into `openspec/specs/` and archive the change.
- [ ] `uv run openspec validate --all --strict`. (Not run by the agent.)
