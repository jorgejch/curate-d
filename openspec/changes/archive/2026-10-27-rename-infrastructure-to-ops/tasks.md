## Shared prerequisite

- [x] 1.1 Attempt IDE directory rename from `infrastructure/` to `ops/`, record its result and preserve all tracked operational files and executable modes. Other streams wait for this task.

## Parent implementation stream

- [x] 2.1 Update Sonar scope tests for `src,ops,migrations` with src-only coverage; run red before settings change. Update live operational paths in Compose, dockerignore, CI, the reset usage comment and affected tests. Add a path-contract regression test; preserve the broader test-directory name and historical archives.
- [x] 2.2 Update Sonar properties, run targeted tests green, validate shell syntax and Compose configuration without executing resets, and run integration checks if the required services are available.

## Documentation stream — starts after 1.1, parallel with 2

- [x] 3.1 Update the live documentation paths and Sonar scope table in the exact documentation files listed in design.md; explain the ops/ naming and migration metrics. Preserve unrelated content and historical evidence.

## Parent finish — waits for 2 and 3

- [x] 4.1 Run the full pre-push Checks workload, sync release-pipeline delta specs and validate this change. Record any external validation limits.

## Workflow follow-up

- Archive the completed change, commit and open the requested PR against dev.
- Confirm the full integration gate and Cloud-side scope/metrics in CI.

## Verification notes

- IDE directory refactoring returned `Cannot read file: infrastructure`; a filesystem
  move preserved all operational files and permissions. IDE symbol refactoring renamed
  the duplication test; its incidental historical archive edit was restored.
- Sonar contract tests failed twice against the previous scope, then passed with the
  new settings. All 148 infrastructure-group tests passed before the final extra mode
  and diagram regression cases were added.
- Shell syntax and Compose configuration validation passed. RustFS staging integration:
  17 passed, two normal long-duration lifecycle observations skipped; no new skips added.
  The existing services were used without resetting their data. The CI boot will verify
  fresh container mounts; no local container recreation was performed.
- Final unit run: 2035 passed, coverage 84.88%; mypy passed for 389 files. Dependency
  sync, full lint/format checks and workflow audit passed in the clean verification
  worktree. The release-pipeline delta is synced and strict validations passed.
- The complete integration gate, fresh-container boot and authenticated Cloud scan
  remain CI verification; no migration body, workflow or data-safety behavior changed.