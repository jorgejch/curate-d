## Decisions

Use `ops/`: the directory contains local/CI provisioning and operator resets, while
Kubernetes manifests are maintained elsewhere. Retain the broad infrastructure test
grouping because it also covers CI and packaging, not just these operational tools.

First attempt the IDE's directory rename refactoring. If it cannot rename directories,
record the limitation and perform a filesystem directory move, preserving modes and
contents, then explicitly update path literals. Code-symbol renames use IDE refactoring.
Do not rewrite historical OpenSpec archives, generic infrastructure prose, or vendor skills.

Source roots become `src,ops,migrations`; coverage exclusions become
`ops/**,migrations/**`. Duplication counts all sources by default. No global inclusion,
exclusion or duplicate-code filter lists are introduced.

## Work streams and ownership

1. Shared parent task: attempt directory refactor and review its changed-file scope.
   All other streams wait because the IDE could update references across the repository.
2. After that join, independent streams may run concurrently:
   - Parent owns `ops/reset/reset-environment.sh`, Compose, `.dockerignore`, CI workflow,
     Sonar properties, all tests and current OpenSpec specs/artifacts.
   - Documentation stream owns `README.md`, `CONTRIBUTING.md`,
     `docs/reset.md`, `docs/recovery.md`, `docs/staging.md`,
     `docs/diagrams/architecture/curate-d-architecture.{json,html}`,
     `ops/README.md`, `ops/staging-compatibility.md`, and `.gitar/config/{apply,approve}.md`.
3. Parent joins documentation before full validation, spec sync, archive and PR.

## Validation and safety

Update the Sonar contract tests and show failure before changing settings. Existing
provisioning/CI tests verify live paths; add a focused repository contract test for the
new operational entry points and absence of stale root-path references. Run targeted
tests, shell syntax and Compose validation, then the full pre-push Checks workload.
Use a clean committed worktree when unrelated untracked skills prevent formatting checks.
Run affected integration tests if their services are available; otherwise explicitly
record the limitation and leave end-to-end integration proof to CI. Never execute reset
commands or provision remote resources to verify a path-only rename.

No application behavior, DBOS calls or migration bodies change. The DBOS skill was
loaded as a precaution; there is no new departure. Old operator commands deliberately
stop resolving; docs provide the new `ops/` paths, without compatibility aliases.