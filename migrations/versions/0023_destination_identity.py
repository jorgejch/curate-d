"""Destination identity: placements belong to a provider, account and root, not a name.

Creates destinations, one row per (provider, account, root path), keyed by the identifier
derived from them. Adds placements.destination_id referencing it and moves both placement
uniqueness rules onto it: one placement per content object per destination identity, and
one content object per locator per destination identity. The destination name stays on
the placement as a label. Placement identifiers are left as they were: audit entries refer
to them and are never rewritten.

Each existing placement is assigned the identity it was made at, from the evidence its
latest `placement_started` audit entry left, first that applies:

1. the entry's `destination_provider`, `destination_account` and `destination_root`;
2. the policy version the entry's `policy_version` names;
3. the policy version in force when the entry was written, or when the placement was
   created if it has no entry.

A name missing from the version found is looked up in the versions before it, latest
first, for an attempt enqueued just before a policy change. The upgrade never guesses a
location: it fails, naming them, when any placement cannot be resolved, or when two would
land on one identity with one content object or one locator.

It also refuses, before changing anything:

- an active policy the application would no longer load, one whose destinations give one
  place two names, nest one root inside another in one account, or have a root that does
  not normalize: activate a corrected policy first;
- a placement still pending, copying or verifying: its workflow identifier and checkpoints
  are keyed by the name, so the deploy drains first (docs/recovery.md, Breaking deploys).

Rekeys destination_checks on destination_id. The table holds only each destination's
latest check, so its rows are dropped; the next scheduled check refills it. Adds a
nullable dry_run_items.destination_id, NULL for items recorded before, and moves the
locator index onto it.

The identifier derivation, root normalization and nesting test are frozen copies of the
application's, so this revision's result never changes with the application code.

The downgrade fails when two placements share a destination name with one content object
or one locator, which the name-keyed constraints cannot hold.

Revision ID: 0023_destination_identity
Revises: 0022_dry_run_drafts
Create Date: 2026-10-25
"""

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid5

import sqlalchemy as sa
from alembic import op

revision: str = "0023_destination_identity"
down_revision: str | None = "0022_dry_run_drafts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen: uuid5(CURATE_D_NAMESPACE, "destination") and the key separator, as in
# curate_d.domain.ids when this revision was written.
DESTINATION_NAMESPACE = UUID("f9acea28-472d-54ce-8863-30f3ad49f3c3")
_SEPARATOR = "\x1f"
_PROVIDERS = "'pcloud'"
_IN_FLIGHT = "'pending', 'copying', 'verifying'"
_REPORTED = 5

_DESTINATIONS_COMMENT = (
    "Every place a copy was ever started at. The policy's name for a destination is a label "
    "and is not kept here: a rename keeps the row, and a name pointed at another account or "
    "root is another row."
)
_PLACEMENT_NAME_COMMENT = (
    "The policy's name for the destination when this was last placed: a label."
)
_CHECK_ID_COMMENT = "The destination identity checked; see derive_destination_id."
_CHECK_NAME_COMMENT = "The policy's name for the destination when it was checked: a label."
_DRY_RUN_ID_COMMENT = (
    "The destination's identity; NULL for content that did not reach rendering. No foreign "
    "key: a dry run records no destination."
)

type Identity = tuple[str, str, str]


@dataclass(frozen=True)
class PolicyVersion:
    """One stored policy version: its number, activation time and destinations by name."""

    version: int
    created_at: datetime
    destinations: Mapping[str, Identity]


class UnresolvedPlacementsError(RuntimeError):
    """Placements whose destination identity no recorded evidence gives."""


class SharedIdentityError(RuntimeError):
    """Placements the backfill would put on one identity with one content or locator."""


class UnloadablePolicyError(RuntimeError):
    """An active policy whose destinations the application now refuses."""


class InFlightPlacementsError(RuntimeError):
    """Placements still being copied or verified under name-keyed workflows."""


def normalize_root(root_path: str) -> str:
    """Return a destination root in canonical form, as curate_d.domain.ids did at 0023.

    Raises:
        ValueError: the root is not absolute or has an empty, "." or ".." segment.
    """
    if not root_path.startswith("/"):
        raise ValueError(f"root path {root_path!r} is not absolute")
    trimmed = root_path.rstrip("/")
    if not trimmed:
        return "/"
    if any(segment in {"", ".", ".."} for segment in trimmed.split("/")[1:]):
        raise ValueError(f"root path {root_path!r} has an empty, '.' or '..' segment")
    return trimmed


def derive_destination_id(provider: str, account: str, root_path: str) -> UUID:
    """Return a destination identity's identifier, as curate_d.domain.ids did at 0023."""
    key = _SEPARATOR.join((provider, account, normalize_root(root_path)))
    return uuid5(DESTINATION_NAMESPACE, key)


def contains(outer: Identity, inner: Identity) -> bool:
    """Return whether `inner` is `outer` or lies inside it, as the application did at 0023."""
    if outer[:2] != inner[:2]:
        return False
    return outer[2] == "/" or inner[2] == outer[2] or inner[2].startswith(outer[2] + "/")


def _identity(destination: Mapping[str, object]) -> Identity:
    """Return a policy destination's identity, its root in canonical form.

    Raises:
        ValueError: the root does not normalize.
    """
    return (
        str(destination["provider"]),
        str(destination["account"]),
        normalize_root(str(destination["root_path"])),
    )


def _listed(document: object) -> list[Mapping[str, object]]:
    """Return a policy document's destinations."""
    listed = document.get("destinations", []) if isinstance(document, Mapping) else []
    return [d for d in listed if isinstance(d, Mapping)]


def policy_conflicts(destinations: Sequence[Mapping[str, object]]) -> list[str]:
    """Return why the application would refuse these destinations, as it decided at 0023.

    Empty when it would load them: every root normalizes, and no two names share a place
    or nest one root inside another in one account.
    """
    conflicts: list[str] = []
    identities: list[tuple[str, Identity]] = []
    for destination in destinations:
        try:
            identities.append((str(destination["name"]), _identity(destination)))
        except ValueError as error:
            conflicts.append(f"{destination.get('name')!r}: {error}")
    for index, (first, at_first) in enumerate(identities):
        for second, at_second in identities[index + 1 :]:
            if contains(at_first, at_second) or contains(at_second, at_first):
                conflicts.append(f"{first!r} and {second!r} share files ({at_first}, {at_second})")
    return conflicts


def _recorded_location(details: Mapping[str, object]) -> Identity | None:
    """Return the location a `placement_started` entry recorded, if it recorded one."""
    parts = (
        details.get("destination_provider"),
        details.get("destination_account"),
        details.get("destination_root"),
    )
    if not all(isinstance(part, str) and part for part in parts):
        return None
    provider, account, root = (str(part) for part in parts)
    try:
        return (provider, account, normalize_root(root))
    except ValueError:
        return None


def _latest_defining(versions: Sequence[PolicyVersion], name: str) -> Identity | None:
    """Return the identity `name` has in the latest of `versions` that defines it."""
    return next((v.destinations[name] for v in reversed(versions) if name in v.destinations), None)


def resolve(
    versions: Sequence[PolicyVersion],
    name: str,
    started: datetime,
    details: Mapping[str, object],
) -> Identity | None:
    """Return the identity a placement under `name` was made at, or None if none is recorded.

    Args:
        versions: Every stored policy version, oldest first.
        name: The destination name the placement was made under.
        started: When its latest copy started, or when it was created without one.
        details: That start's audit details; empty when it has none.
    """
    recorded = _recorded_location(details)
    if recorded is not None:
        return recorded
    named = details.get("policy_version")
    # A named version that exists is the evidence, even when it does not resolve: falling
    # through to the time-based version could pick one activated after the attempt left.
    if type(named) is int and any(v.version == named for v in versions):
        return _latest_defining([v for v in versions if v.version <= named], name)
    return _latest_defining([v for v in versions if v.created_at <= started], name)


def _policies(bind: sa.Connection) -> list[PolicyVersion]:
    """Return every stored policy version, oldest first.

    A destination whose root does not normalize is left out, so a placement under it is
    reported unresolved rather than given a guessed identity.
    """
    rows = bind.execute(
        sa.text(
            "SELECT version, created_at, document FROM configurations "
            "WHERE kind = 'policy' ORDER BY version"
        )
    )
    versions: list[PolicyVersion] = []
    for version, created_at, document in rows:
        destinations: dict[str, Identity] = {}
        for destination in _listed(document):
            try:
                destinations[str(destination["name"])] = _identity(destination)
            except ValueError:
                continue
        versions.append(PolicyVersion(version, created_at, destinations))
    return versions


def _refuse_unloadable_active_policy(bind: sa.Connection) -> None:
    """Fail when the active policy is one the application would no longer load.

    Raises:
        UnloadablePolicyError: its destinations share a place or a root does not normalize.
    """
    row = bind.execute(
        sa.text("SELECT version, document FROM configurations WHERE kind = 'policy' AND active")
    ).one_or_none()
    if row is None:
        return
    conflicts = policy_conflicts(_listed(row[1]))
    if conflicts:
        raise UnloadablePolicyError(
            f"active policy version {row[0]} would no longer load: {'; '.join(conflicts)}; "
            "activate a corrected policy before upgrading; nothing was changed"
        )


def _refuse_in_flight_placements(bind: sa.Connection) -> None:
    """Fail while any placement is pending, copying or verifying.

    Raises:
        InFlightPlacementsError: such placements exist; the message names up to five.
    """
    rows = bind.execute(
        sa.text(
            "SELECT id, destination_name, state FROM placements "
            f"WHERE state IN ({_IN_FLIGHT}) ORDER BY id"
        )
    ).all()
    if rows:
        named = ", ".join(f"{row[0]} ({row[1]}, {row[2]})" for row in rows[:_REPORTED])
        raise InFlightPlacementsError(
            f"{len(rows)} placements are still pending, copying or verifying, e.g. {named}; "
            "their workflows are keyed by the destination name: drain them first "
            "(docs/recovery.md, Breaking deploys); nothing was changed"
        )


def _backfill(bind: sa.Connection) -> None:
    """Record each placement's destination identity from the evidence it left.

    Raises:
        UnresolvedPlacementsError: no evidence gives a placement's identity.
        SharedIdentityError: two placements would share an identity and a content object
            or a locator.
    """
    versions = _policies(bind)
    placements = bind.execute(
        sa.text(
            "SELECT p.id, p.content_object_id, p.locator, p.destination_name, "
            "  COALESCE(s.occurred_at, p.created_at) AS started, s.details "
            "FROM placements p LEFT JOIN LATERAL ("
            "  SELECT a.occurred_at, a.details FROM audit_entries a"
            "  WHERE a.placement_id = p.id AND a.entry_type = 'placement_started'"
            "  ORDER BY a.occurred_at DESC, a.id DESC LIMIT 1"
            ") AS s ON true "
            "ORDER BY p.id"
        )
    ).all()
    identities: dict[UUID, Identity] = {}
    assignments: list[dict[str, object]] = []
    unresolved: list[str] = []
    claimed: dict[tuple[UUID, str], list[str]] = defaultdict(list)
    for placement_id, content_object_id, locator, name, started, details in placements:
        recorded = details if isinstance(details, Mapping) else {}
        identity = resolve(versions, name, started, recorded)
        label = f"{placement_id} ({name})"
        if identity is None:
            unresolved.append(label)
            continue
        destination_id = derive_destination_id(*identity)
        identities[destination_id] = identity
        assignments.append({"placement_id": placement_id, "destination_id": destination_id})
        claimed[(destination_id, f"content {content_object_id}")].append(label)
        claimed[(destination_id, f"locator {locator}")].append(label)
    if unresolved:
        raise UnresolvedPlacementsError(
            f"{len(unresolved)} placements have no recorded location and name a destination "
            "no policy version in force when they were started defines, e.g. "
            f"{', '.join(unresolved[:_REPORTED])}; nothing was changed"
        )
    shared = [
        f"{what} at {identities[destination_id]}: {', '.join(labels)}"
        for (destination_id, what), labels in claimed.items()
        if len(labels) > 1
    ]
    if shared:
        raise SharedIdentityError(
            f"{len(shared)} clashes: placements under different names resolve to one place "
            f"with one content object or locator, e.g. {'; '.join(shared[:_REPORTED])}; "
            "remove the duplicates first; nothing was changed"
        )
    if identities:
        bind.execute(
            sa.text(
                "INSERT INTO destinations (id, provider, account, root_path) "
                "VALUES (:id, :provider, :account, :root_path)"
            ),
            [
                {"id": key, "provider": provider, "account": account, "root_path": root}
                for key, (provider, account, root) in identities.items()
            ],
        )
    if assignments:
        bind.execute(
            sa.text(
                "UPDATE placements SET destination_id = :destination_id WHERE id = :placement_id"
            ),
            assignments,
        )


def upgrade() -> None:
    """Create destinations; key placements, destination_checks and dry_run_items on it."""
    bind = op.get_bind()
    _refuse_unloadable_active_policy(bind)
    _refuse_in_flight_placements(bind)
    op.create_table(
        "destinations",
        sa.Column(
            "id",
            sa.UUID(),
            nullable=False,
            comment="Derived from provider, account and root path; see derive_destination_id.",
        ),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("account", sa.Text(), nullable=False),
        sa.Column(
            "root_path", sa.Text(), nullable=False, comment="Canonical form: see normalize_root."
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(f"provider IN ({_PROVIDERS})", name="ck_destinations_provider_known"),
        sa.PrimaryKeyConstraint("id", name="pk_destinations"),
        sa.UniqueConstraint(
            "provider", "account", "root_path", name="uq_destinations_provider_account_root_path"
        ),
        comment=_DESTINATIONS_COMMENT,
    )
    op.add_column(
        "placements",
        sa.Column(
            "destination_id",
            sa.UUID(),
            nullable=True,
            comment="Where the copy is. Part of the placement's identity.",
        ),
    )
    _backfill(bind)
    op.alter_column("placements", "destination_id", existing_type=sa.UUID(), nullable=False)
    op.create_foreign_key(
        "fk_placements_destination_id_destinations",
        "placements",
        "destinations",
        ["destination_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.drop_constraint(
        "uq_placements_content_object_id_destination_name", "placements", type_="unique"
    )
    op.drop_constraint("uq_placements_destination_name_locator", "placements", type_="unique")
    op.create_unique_constraint(
        "uq_placements_content_object_id_destination_id",
        "placements",
        ["content_object_id", "destination_id"],
    )
    op.create_unique_constraint(
        "uq_placements_destination_id_locator", "placements", ["destination_id", "locator"]
    )
    op.alter_column(
        "placements",
        "destination_name",
        existing_type=sa.Text(),
        existing_nullable=False,
        comment=_PLACEMENT_NAME_COMMENT,
    )

    op.execute("DELETE FROM destination_checks")
    op.drop_constraint("pk_destination_checks", "destination_checks", type_="primary")
    op.add_column(
        "destination_checks",
        sa.Column("destination_id", sa.UUID(), nullable=False, comment=_CHECK_ID_COMMENT),
    )
    op.create_primary_key("pk_destination_checks", "destination_checks", ["destination_id"])
    op.alter_column(
        "destination_checks",
        "destination_name",
        existing_type=sa.Text(),
        existing_nullable=False,
        comment=_CHECK_NAME_COMMENT,
    )

    op.add_column(
        "dry_run_items",
        sa.Column("destination_id", sa.UUID(), nullable=True, comment=_DRY_RUN_ID_COMMENT),
    )
    op.drop_index("ix_dry_run_items_run_id_destination_name_locator", table_name="dry_run_items")
    op.create_index(
        "ix_dry_run_items_run_id_destination_id_locator",
        "dry_run_items",
        ["run_id", "destination_id", "locator"],
    )


def _refuse_name_collisions(bind: sa.Connection) -> None:
    """Fail when the name-keyed placement constraints cannot hold the rows.

    Raises:
        RuntimeError: two placements share a name with one content object or one locator.
    """
    for columns in (("content_object_id", "destination_name"), ("destination_name", "locator")):
        listed = ", ".join(columns)
        clashes = bind.scalar(
            sa.text(
                f"SELECT count(*) FROM (SELECT 1 FROM placements GROUP BY {listed} "
                "HAVING count(*) > 1) AS clashes"
            )
        )
        if clashes:
            raise RuntimeError(
                f"{clashes} ({listed}) pairs hold more than one placement; the name-keyed "
                "constraint cannot be restored over them; nothing was changed"
            )


def downgrade() -> None:
    """Key placements, destination_checks and dry_run_items on the name; drop destinations."""
    _refuse_name_collisions(op.get_bind())
    op.drop_index("ix_dry_run_items_run_id_destination_id_locator", table_name="dry_run_items")
    op.create_index(
        "ix_dry_run_items_run_id_destination_name_locator",
        "dry_run_items",
        ["run_id", "destination_name", "locator"],
    )
    op.drop_column("dry_run_items", "destination_id")

    op.execute("DELETE FROM destination_checks")
    op.drop_constraint("pk_destination_checks", "destination_checks", type_="primary")
    op.drop_column("destination_checks", "destination_id")
    op.create_primary_key("pk_destination_checks", "destination_checks", ["destination_name"])
    op.alter_column(
        "destination_checks",
        "destination_name",
        existing_type=sa.Text(),
        existing_nullable=False,
        comment=None,
        existing_comment=_CHECK_NAME_COMMENT,
    )

    op.alter_column(
        "placements",
        "destination_name",
        existing_type=sa.Text(),
        existing_nullable=False,
        comment=None,
        existing_comment=_PLACEMENT_NAME_COMMENT,
    )
    op.drop_constraint("uq_placements_destination_id_locator", "placements", type_="unique")
    op.drop_constraint(
        "uq_placements_content_object_id_destination_id", "placements", type_="unique"
    )
    op.create_unique_constraint(
        "uq_placements_content_object_id_destination_name",
        "placements",
        ["content_object_id", "destination_name"],
    )
    op.create_unique_constraint(
        "uq_placements_destination_name_locator", "placements", ["destination_name", "locator"]
    )
    op.drop_constraint(
        "fk_placements_destination_id_destinations", "placements", type_="foreignkey"
    )
    op.drop_column("placements", "destination_id")
    op.drop_table("destinations")
