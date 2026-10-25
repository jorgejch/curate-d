# Tasks

One work stream: the policy, its `CONTRIBUTING.md` pointer and the Sonar exclusion land together
(design.md, Parallelism).

## 1. Add the auto-apply policy

- [x] 1.1 Add `.gitar/**` to `sonar.coverage.exclusions` and `sonar.cpd.exclusions` in `sonar-project.properties`; verify `uv run pytest tests/unit/infrastructure/test_sonar_properties.py` fails on the new tracked directory without the entries and passes with them
- [x] 1.2 Write `.gitar/config/apply.md`: scope and mechanics (read from `dev`, enables nothing, code-review findings only), the organization criteria as binding, what "verified by inspection" means here, eligible fixes, the curate.d safeguards that need human review, behaviour on uncertainty, reporting, and an allow/skip list covering a local edge-case fix, a regression test, a documentation correction, a ledger/DBOS semantic change, a migration/CI/dependency change and a finding whose correctness can't be established; checked against Gitar's "Auto-apply" and "Repository customization" documentation. Verify every repository path it names exists: each backticked path in the file passes `ls -d` from the repository root (only `asyncio.CancelledError`, a Python name, is not a path)
- [x] 1.3 Point at the policy from `CONTRIBUTING.md`'s "Branches, commits and pull requests"; verify `make check` passes
