"""Scan progress: which scans are open, and which of their folders are still to list.

A scan's frontier is its unlisted folders. A folder is marked listed in the same
transaction that records its files, so "listed" always means "every file in it is in the
ledger". A scan stopped by a provider failure stays open as incomplete; the next scan of
the same root takes it over and continues from that frontier instead of starting again.
"""

from collections.abc import Sequence
from uuid import UUID, uuid4

from pydantic import AwareDatetime
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection

from curate_d.domain import PendingFolder, Provider, Scan, ScanStatus
from curate_d.ledger.mapping import Row, identifier, moment, opt_moment, opt_text, text
from curate_d.ledger.tables import scan_folders, scans


def to_scan(row: Row) -> Scan:
    """Translate a scans row into a `Scan`."""
    return Scan(
        id=identifier(row, "id"),
        provider=Provider(text(row, "provider")),
        account=text(row, "account"),
        root=text(row, "root"),
        status=ScanStatus(text(row, "status")),
        failure_reason=opt_text(row, "failure_reason"),
        started_at=moment(row, "started_at"),
        finished_at=opt_moment(row, "finished_at"),
    )


class ScanRepository:
    """Reads and writes scans and their frontiers on a caller-owned connection."""

    def __init__(self, connection: AsyncConnection) -> None:
        """Wrap a caller-owned connection; the repository never opens its own."""
        self._connection = connection

    async def open(
        self,
        provider: Provider,
        account: str,
        root: PendingFolder,
        *,
        now: AwareDatetime,
    ) -> tuple[Scan, bool]:
        """Resume the root's open scan, or start one whose frontier is the root folder.

        Returns:
            The scan, and whether it was resumed rather than started.
        """
        existing = (
            (
                await self._connection.execute(
                    select(scans)
                    .where(
                        scans.c.provider == provider.value,
                        scans.c.account == account,
                        scans.c.root == root.path,
                        scans.c.status != ScanStatus.COMPLETE.value,
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )
        if existing is not None:
            resumed = (
                (
                    await self._connection.execute(
                        scans.update()
                        .where(scans.c.id == identifier(existing, "id"))
                        .values(status=ScanStatus.RUNNING.value, failure_reason=None)
                        .returning(*scans.c)
                    )
                )
                .mappings()
                .one()
            )
            return to_scan(resumed), True
        scan_id = uuid4()
        created = (
            (
                await self._connection.execute(
                    scans.insert()
                    .values(
                        id=scan_id,
                        provider=provider.value,
                        account=account,
                        root=root.path,
                        status=ScanStatus.RUNNING.value,
                        started_at=now,
                    )
                    .returning(*scans.c)
                )
            )
            .mappings()
            .one()
        )
        await self._connection.execute(
            scan_folders.insert().values(scan_id=scan_id, folder_id=root.folder_id, path=root.path)
        )
        return to_scan(created), False

    async def get(self, scan_id: UUID) -> Scan | None:
        """Fetch a scan, or None."""
        row = (
            (await self._connection.execute(select(scans).where(scans.c.id == scan_id)))
            .mappings()
            .one_or_none()
        )
        return to_scan(row) if row is not None else None

    async def frontier(self, scan_id: UUID, *, limit: int) -> list[PendingFolder]:
        """Up to `limit` folders not yet listed, in a stable order."""
        result = await self._connection.execute(
            select(scan_folders.c.folder_id, scan_folders.c.path)
            .where(scan_folders.c.scan_id == scan_id, scan_folders.c.listed_at.is_(None))
            .order_by(scan_folders.c.path, scan_folders.c.folder_id)
            .limit(limit)
        )
        return [
            PendingFolder(folder_id=text(row, "folder_id"), path=text(row, "path"))
            for row in result.mappings()
        ]

    async def listed_count(self, scan_id: UUID) -> int:
        """How many folders of the scan have been listed."""
        result = await self._connection.execute(
            select(scan_folders.c.folder_id).where(
                scan_folders.c.scan_id == scan_id, scan_folders.c.listed_at.is_not(None)
            )
        )
        return len(result.all())

    async def mark_listed(
        self,
        scan_id: UUID,
        folder: PendingFolder,
        subfolders: Sequence[PendingFolder],
        *,
        now: AwareDatetime,
    ) -> None:
        """Confirm a folder, and add its subfolders to the frontier."""
        if subfolders:
            await self._connection.execute(
                insert(scan_folders)
                .values(
                    [
                        {"scan_id": scan_id, "folder_id": sub.folder_id, "path": sub.path}
                        for sub in subfolders
                    ]
                )
                .on_conflict_do_nothing(index_elements=["scan_id", "folder_id"])
            )
        await self._connection.execute(
            scan_folders.update()
            .where(scan_folders.c.scan_id == scan_id, scan_folders.c.folder_id == folder.folder_id)
            .values(listed_at=now)
        )

    async def retire(self, scan_id: UUID, folder: PendingFolder) -> None:
        """Drop a folder that no longer exists from the frontier, unlisted."""
        await self._connection.execute(
            scan_folders.delete().where(
                scan_folders.c.scan_id == scan_id,
                scan_folders.c.folder_id == folder.folder_id,
                scan_folders.c.listed_at.is_(None),
            )
        )

    async def mark_incomplete(self, scan_id: UUID, reason: str) -> None:
        """Stop the scan with a reason; its frontier waits for the next scan of the root."""
        await self._connection.execute(
            scans.update()
            .where(scans.c.id == scan_id)
            .values(status=ScanStatus.INCOMPLETE.value, failure_reason=reason)
        )

    async def complete(self, scan_id: UUID, *, now: AwareDatetime) -> None:
        """Close a scan whose frontier is empty."""
        await self._connection.execute(
            scans.update()
            .where(scans.c.id == scan_id)
            .values(status=ScanStatus.COMPLETE.value, failure_reason=None, finished_at=now)
        )
