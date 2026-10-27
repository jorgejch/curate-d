# Design

## Context

Sonar uses explicit source/test paths, mirrored text inclusions and exclusions that keep coverage limited to `src/`.

## Goals / Non-Goals

- Include `compose.yaml` in source and secret scanning, without affecting coverage.
- Do not add wildcard root scope, the lifecycle override, or claim Dockerfile-specific rules apply to Compose.

## Decisions

Append the literal filename to sources, text inclusions and coverage exclusions. Keep automatic configuration disabled and preserve all existing scope boundaries.

Work streams A (properties and tests) and B (`CONTRIBUTING.md`) touch disjoint files and can start together after proposal completion. Final validation, spec sync and archive depend on both. The change is small enough to execute these streams locally without delegation.

## Risks / Trade-offs

Indexing and analyzer support require a subsequent Cloud scan; local tests validate configuration only. YAML inclusion does not guarantee Compose-specific rules.

## Migration Plan

Update the static settings and tests; run the unit suite and targeted lint checks. Revert the filename additions to roll back.

## Open Questions

None.