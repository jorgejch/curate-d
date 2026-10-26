"""Act on a deployment from inside its I/O worker pod, for what has no endpoint.

`dev_api.sh` pipes this file to the worker container's `python -`, where the rclone
sidecar, the ledger and the workflow system database are reachable:

    seed ACCOUNT SOURCE ORGANIZED   upload the library staged under /scratch to SOURCE,
                                    and create the empty folder ORGANIZED
    tree ACCOUNT PATH               list every file under PATH
    scan ACCOUNT ROOT               enqueue one scan of ROOT
    trigger SCHEDULE                run a persisted schedule now, e.g. `reconcile`
    report ACCOUNT ROOT             each file under ROOT as the ledger sees it: its latest
                                    classification, review item and placements, as JSON lines
"""

import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

from dbos import EnqueueOptions
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from curate_d.config import Settings
from curate_d.domain import Provider
from curate_d.placement.rclone import RcloneClient
from curate_d.placement.remotes import ConnectedRemotes, RcloneAccess
from curate_d.providers.crypto import CredentialCipher
from curate_d.runtime.dbos_app import client
from curate_d.runtime.queues import QueueName
from curate_d.runtime.workflow_ids import scan_workflow_id

STAGED = Path("/scratch/curate-d-seed")
SCAN_WORKFLOW = "scan_root"
REPORT = text(
    """
    SELECT s.path, s.state, s.content_object_id::text AS content,
           c.tier, c.model_id, c.media_kind, c.category, c.subcategory,
           r.reason AS review_reason, r.status AS review_status, r.detail AS review_detail,
           coalesce(p.placements, '[]'::json) AS placements
    FROM source_objects s
    LEFT JOIN LATERAL (
        SELECT * FROM classifications c WHERE c.content_object_id = s.content_object_id
        ORDER BY c.produced_at DESC LIMIT 1
    ) c ON true
    LEFT JOIN LATERAL (
        SELECT * FROM review_items r WHERE r.content_object_id = s.content_object_id
        ORDER BY r.created_at DESC LIMIT 1
    ) r ON true
    LEFT JOIN LATERAL (
        SELECT json_agg(json_build_object(
            'destination', p.destination_name, 'destination_id', p.destination_id,
            'locator', p.locator, 'state', p.state,
            'level', p.verification_level, 'failure', p.failure_reason
        ) ORDER BY p.destination_name, p.destination_id) AS placements
        FROM placements p WHERE p.content_object_id = s.content_object_id
    ) p ON true
    WHERE s.provider = 'pcloud' AND s.account = :account
      AND (s.path = :root OR s.path LIKE :under)
    ORDER BY s.path
    """
)


async def _remote(settings: Settings, account: str) -> tuple[RcloneClient, str]:
    """An rclone client and the account's `remote:`, configured from its stored tokens."""
    if settings.rclone_rc_url is None:
        raise SystemExit("CURATE_D_RCLONE_RC_URL is unset: run this in the I/O worker")
    rclone = RcloneAccess(str(settings.rclone_rc_url))
    engine = create_async_engine(str(settings.ledger_database_url))
    try:
        remotes = ConnectedRemotes(rclone, engine, CredentialCipher.from_settings(settings))
        fs = await remotes.fs_for(Provider.PCLOUD, account)
    finally:
        await engine.dispose()
    return rclone.client(), fs


async def seed(settings: Settings, account: str, source: str, organized: str) -> None:
    """Upload every staged file under `source`, and create `organized`."""
    rclone, fs = await _remote(settings, account)
    # macOS tar adds `._` AppleDouble files unless told not to; they are not the library.
    for path in sorted(p for p in STAGED.rglob("*") if p.is_file() and not p.name.startswith("._")):
        relative = path.relative_to(STAGED).as_posix()
        destination = f"{source.strip('/')}/{relative}"
        await rclone.call(
            "operations/copyfile",
            {"srcFs": str(STAGED), "srcRemote": relative, "dstFs": fs, "dstRemote": destination},
        )
        print(f"uploaded /{destination}")
    await rclone.call("operations/mkdir", {"fs": fs, "remote": organized.strip("/")})
    print(f"created {organized}")


async def tree(settings: Settings, account: str, path: str) -> None:
    """Print every file under `path` with its size."""
    rclone, fs = await _remote(settings, account)
    body = await rclone.call(
        "operations/list",
        {"fs": fs, "remote": path.strip("/"), "opt": {"recurse": True, "filesOnly": True}},
    )
    listed = body.get("list")
    for item in listed if isinstance(listed, list) else []:
        print(f"{item['Size']:>10}  /{item['Path']}")


async def scan(settings: Settings, account: str, root: str) -> None:
    """Enqueue one scan of `root` under a fresh cycle, as the hourly schedule would."""
    cycle = f"manual-{datetime.now(UTC):%Y%m%dT%H%M%SZ}"
    workflow_id = scan_workflow_id(
        Provider.PCLOUD.value, account, quote(root, safe="/"), f"{cycle}+0"
    )
    options: EnqueueOptions = {
        "workflow_name": SCAN_WORKFLOW,
        "queue_name": QueueName.SCAN.value,
        "workflow_id": workflow_id,
        "queue_partition_key": Provider.PCLOUD.value,
    }
    await client(settings).enqueue_async(options, Provider.PCLOUD.value, account, root, cycle, 0)
    print(workflow_id)


async def report(settings: Settings, account: str, root: str) -> None:
    """Print each source file under `root` with what the ledger decided for its content."""
    engine = create_async_engine(str(settings.ledger_database_url))
    try:
        async with engine.connect() as connection:
            rows = await connection.execute(
                REPORT, {"account": account, "root": root, "under": f"{root.rstrip('/')}/%"}
            )
            for row in rows.mappings():
                print(json.dumps(dict(row), default=str))
    finally:
        await engine.dispose()


def trigger(settings: Settings, schedule: str) -> None:
    """Run a persisted schedule once, now."""
    print(client(settings).trigger_schedule(schedule).get_workflow_id())


def main(argv: list[str]) -> None:
    """Dispatch a command."""
    settings = Settings()
    match argv:
        case ["seed", account, source, organized]:
            asyncio.run(seed(settings, account, source, organized))
        case ["tree", account, path]:
            asyncio.run(tree(settings, account, path))
        case ["scan", account, root]:
            asyncio.run(scan(settings, account, root))
        case ["report", account, root]:
            asyncio.run(report(settings, account, root))
        case ["trigger", schedule]:
            trigger(settings, schedule)
        case _:
            raise SystemExit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
