"""Connection health, as the control plane reports it.

Derived from the stored record and the clock rather than stored, because "expiring" is a
statement about now. Three states are distinguished: healthy, expiring (still usable, and
renewed automatically when a refresh credential exists), and requiring a person.
"""

from collections.abc import Sequence
from datetime import timedelta

from pydantic import AwareDatetime

from curate_d.domain import ConnectionHealth, ConnectionState, ProviderConnection


def assess_health(
    record: ProviderConnection, *, now: AwareDatetime, window: timedelta
) -> ConnectionHealth:
    """Report one connection's health at `now`.

    An expired token that cannot be refreshed is reported as needing re-authentication
    straight away, without waiting for the next refresh pass to flag it.
    """

    def health(
        state: ConnectionState, *, attention: bool = False, detail: str | None = None
    ) -> ConnectionHealth:
        """Build the report for this record."""
        return ConnectionHealth(
            provider=record.provider,
            account=record.account,
            state=state,
            expires_at=record.expires_at,
            requires_attention=attention,
            detail=detail,
        )

    who = f"{record.provider.value} account {record.account}"
    if record.state is ConnectionState.REQUIRES_REAUTHENTICATION:
        return health(
            ConnectionState.REQUIRES_REAUTHENTICATION,
            attention=True,
            detail=f"re-authorize {who}: {record.failure_reason}",
        )
    expires_at = record.expires_at
    if expires_at is None or expires_at - now > window:
        return health(ConnectionState.HEALTHY)
    if record.refreshable:
        return health(ConnectionState.EXPIRING, detail="renewal is due and automatic")
    if expires_at <= now:
        return health(
            ConnectionState.REQUIRES_REAUTHENTICATION,
            attention=True,
            detail=f"re-authorize {who}: access token expired and cannot be renewed",
        )
    return health(
        ConnectionState.EXPIRING,
        attention=True,
        detail=f"re-authorize {who} before {expires_at.isoformat()}: it cannot be renewed "
        "automatically",
    )


def report(
    records: Sequence[ProviderConnection], *, now: AwareDatetime, window: timedelta
) -> list[ConnectionHealth]:
    """Health of every connection, in the order given."""
    return [assess_health(record, now=now, window=window) for record in records]
