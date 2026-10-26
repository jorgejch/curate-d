<!-- Target dev, not main. See CONTRIBUTING.md. -->

## What and why

<!-- What changes, and the reason. Link the issue: "Closes #123". -->

## OpenSpec change

<!-- The change under openspec/changes/<name>/, or "simple fix" for a typo, one-line bug fix or doc correction. -->

## How it was verified

<!-- The tests added, and the commands run with their outcome. -->

- [ ] `make check` passes locally
- [ ] Integration tests for the touched areas pass (`uv run pytest tests/integration/test_<area>.py --no-cov -m integration --staging-backend=rustfs`), or none apply

## Checklist

- [ ] Every new behaviour has an isolated unit test, including its failure branches
- [ ] Commits follow Conventional Commits, one logical change each
- [ ] Delta specs are synced into `openspec/specs/` and the change is archived, or that follows in a named PR
- [ ] A DBOS change was checked against the `dbos-python` skill; any new departure is recorded in `CLAUDE.md`
- [ ] A new drift ERROR event has a repair and an entry in `docs/recovery.md`'s alert list, or none was added
- [ ] A migration has its docstrings and is covered by `tests/integration/test_migrations.py`, or none was added

## Data safety

<!-- Can this change release an original, skip verification or alter a placement? If so, say what keeps it safe on crash, retry and replay. Otherwise write "No effect on placements or originals". -->
