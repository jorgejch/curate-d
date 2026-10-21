"""Operator entry point: check that every stored configuration version reads with this build.

Reads parse a stored version as strictly as a submission, except for the taxonomy check
and retired fields, so a version in force that this build cannot read stops every read of
its kind. Run this from the image about to be deployed, against the target database,
before rollout: it parses every version of every kind with the read parsers and logs each
one that fails, with its kind, version and problems.

    python -m curate_d.configuration.verify

It opens one read-only connection and writes nothing. A failing version in force is fixed
by `PUT`ting a corrected document with the current build, then running this again.
"""

import argparse
import logging
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from pydantic import BaseModel
from sqlalchemy import create_engine, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import NullPool

from curate_d.config import Settings, get_settings
from curate_d.configuration.documents import parse_stored_policy, parse_taxonomy
from curate_d.configuration.store import ConfigurationError, check_document
from curate_d.domain import ConfigurationKind
from curate_d.ledger.tables import configurations
from curate_d.observability import configure_logging, get_logger
from curate_d.rendering.templates import parse_templates

logger = get_logger(__name__)

EXIT_IN_FORCE_UNREADABLE: Final = 1
"""A version in force does not read; this build would fail every read of its kind."""

EXIT_DATABASE_UNREADABLE: Final = 2
"""The configurations could not be read from the database; nothing was checked."""

_READERS: Final[Mapping[ConfigurationKind, Callable[[Mapping[str, object]], BaseModel]]] = {
    ConfigurationKind.TAXONOMY: parse_taxonomy,
    ConfigurationKind.TEMPLATES: parse_templates,
    ConfigurationKind.POLICY: parse_stored_policy,
}


@dataclass(frozen=True)
class StoredVersion:
    """One stored configuration version, as a `configurations` row holds it."""

    kind: str
    version: int
    active: bool
    document: object


@dataclass(frozen=True)
class Failure:
    """A stored version this build cannot read, and why."""

    kind: str
    version: int
    active: bool
    problems: tuple[str, ...]


@dataclass(frozen=True)
class Report:
    """The outcome of checking every stored version."""

    checked: int
    failures: tuple[Failure, ...]

    @property
    def exit_code(self) -> int:
        """Return 0 unless a version in force fails, which gives `EXIT_IN_FORCE_UNREADABLE`."""
        if any(failure.active for failure in self.failures):
            return EXIT_IN_FORCE_UNREADABLE
        return 0


def _problems(stored: StoredVersion) -> tuple[str, ...]:
    """Return why `stored` does not read with this build, or nothing when it does."""
    try:
        kind = ConfigurationKind(stored.kind)
    except ValueError:
        return (f"(root): unknown configuration kind {stored.kind!r}",)
    if not isinstance(stored.document, Mapping):
        return (f"(root): the document is a {type(stored.document).__name__}, not an object",)
    try:
        check_document(kind, stored.document, _READERS[kind])
    except ConfigurationError as unreadable:
        return tuple(unreadable.problems)
    return ()


def verify(rows: Iterable[StoredVersion]) -> Report:
    """Parse every stored version with the read parsers and report those that fail.

    No taxonomy is passed, as reads pass none, and a stored policy drops its retired
    retention fields, so the check fails exactly the versions a read would.
    """
    checked = 0
    failures: list[Failure] = []
    for stored in rows:
        checked += 1
        problems = _problems(stored)
        if problems:
            failures.append(Failure(stored.kind, stored.version, stored.active, problems))
    return Report(checked=checked, failures=tuple(failures))


def read_stored(settings: Settings) -> list[StoredVersion]:
    """Return every stored configuration version, read on a read-only connection.

    Raises:
        SQLAlchemyError: Connecting to or querying the ledger database failed.
    """
    engine = create_engine(str(settings.ledger_database_url), poolclass=NullPool)
    try:
        with engine.connect() as connection:
            readonly = connection.execution_options(postgresql_readonly=True)
            rows = readonly.execute(
                select(
                    configurations.c.kind,
                    configurations.c.version,
                    configurations.c.active,
                    configurations.c.document,
                ).order_by(configurations.c.kind, configurations.c.version)
            ).all()
    finally:
        engine.dispose()
    return [
        StoredVersion(kind=kind, version=version, active=active, document=document)
        for kind, version, active, document in rows
    ]


def main(argv: Sequence[str] | None = None) -> int:
    """Check every stored configuration version and return the process exit status.

    Returns:
        0 when every version in force reads, even if older versions fail (they are
        logged); `EXIT_IN_FORCE_UNREADABLE` when a version in force does not read;
        `EXIT_DATABASE_UNREADABLE` when the database could not be read.
    """
    parser = argparse.ArgumentParser(
        prog="python -m curate_d.configuration.verify",
        description="Check that every stored configuration version reads with this build.",
    )
    parser.parse_args(argv)
    configure_logging(level=logging.INFO)

    try:
        rows = read_stored(get_settings())
    except SQLAlchemyError as unreadable:
        logger.error("configuration_verify.database_unreadable", error=str(unreadable))
        return EXIT_DATABASE_UNREADABLE
    report = verify(rows)
    for failure in report.failures:
        log = logger.error if failure.active else logger.warning
        log(
            "configuration_verify.unreadable",
            kind=failure.kind,
            version=failure.version,
            in_force=failure.active,
            problems=list(failure.problems),
        )
    logger.info(
        "configuration_verify.checked",
        versions=report.checked,
        unreadable=len(report.failures),
        unreadable_in_force=sum(failure.active for failure in report.failures),
    )
    return report.exit_code


if __name__ == "__main__":
    sys.exit(main())
