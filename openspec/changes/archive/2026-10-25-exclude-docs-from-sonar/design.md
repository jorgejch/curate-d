# Design

## Context

`sonar-project.properties` sets no `sonar.sources`, so SonarQube Cloud analyses every tracked
file. Coverage and duplication are already narrowed through `sonar.coverage.exclusions` and
`sonar.cpd.exclusions`, both pinned by `tests/unit/infrastructure/test_sonar_properties.py`. Issue
analysis has no exclusion at all. `docs/` holds four Markdown runbooks and two generated
diagram pages with their JSON sources; every `docs/` issue is on the two HTML pages. `openspec/`
holds the planning workflow's Markdown and YAML: specs, change artifacts and the archive.

## Goals / Non-Goals

**Goals:**
- No SonarQube analysis of `docs/` or `openspec/`, pinned by the unit suite.

**Non-Goals:**
- Excluding other directories (`.agents/`, `.claude/`) from analysis. Neither carries open issues
  today; widening the exclusion is its own decision.
- Enabling GitHub secret scanning (see Risks).
- Changing SonarQube for IDE's local exclusions. They live in each developer's IDE settings, not
  in the repository.

## Decisions

- **`docs/**` over `docs/diagrams/**`.** The narrower pattern would also remove every current
  `docs/` issue and keep the runbooks under secret detection. The whole directory was chosen so
  `docs/` stays out of every Sonar measure alike (it is already out of coverage and duplication),
  and a new generated page under another `docs/` subdirectory needs no follow-up.
- **Exclude `openspec/` too.** It holds prose and YAML for the planning workflow, not code, and is
  already out of coverage and duplication. It carries no issue today, so the exclusion is
  preventive: a change artifact quoting code or a config snippet never becomes an issue to triage.
- **`sonar.exclusions` over issue-ignore rules.** `sonar.issue.ignore.multicriteria` on `docs/**`
  for the `javascript`, `css` and `Web` rules would keep secret detection, but it still analyses
  the files and must be extended for every new language a page uses.
- **Pin the property to exactly `docs/**` and `openspec/**`.** An exact pin, like the coverage and
  duplication tests, turns any later exclusion into a deliberate edit of the test, so `src/` or
  `tests/` can't fall out of analysis by accident.
- **Keep both directories in the coverage and duplication exclusions.** The entries become
  redundant, but the existing tests derive those lists from the tracked top-level directories.
  Special-casing analysed-out directories there would make the tests harder to follow for no
  change in behaviour.

## Risks / Trade-offs

- [Secrets written into a runbook or a change artifact go undetected: Sonar is the repository's
  only secret scanner, GitHub secret scanning is disabled] → Accepted for this change. GitHub
  secret scanning, free for public repositories, would cover the whole repository and its
  history; enabling it is a separate repository-settings decision.
- [Open `docs/` issues linger until `dev` is analysed again] → `coverage-report` analyses `dev`
  on the merge push; SonarQube closes issues on excluded files in that analysis.

## Migration Plan

Merge into `dev`. Rollback is reverting the commit; the next `dev` analysis reopens the issues.

## Parallelism

One work stream. The property and its test touch two files and land in one commit, so the change
can't be split.
