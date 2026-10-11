"""Scheduled renewal of provider access tokens.

One pass assesses every connection, renews the ones inside the refresh window, and
records each outcome. The provider call is a step; what it learns is written by a ledger
datasource transaction, so a replayed pass neither calls the provider twice nor writes
twice. Renewed tokens cross from the step to the transaction sealed, because a step's
result is checkpointed in the DBOS system tables.

A rejected refresh flags the connection for re-authentication with the provider's reason,
and work that needs the connection stops there rather than retrying. An unreachable
provider changes nothing: the token is still inside its window, and the next pass tries
again.
"""

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Final, Protocol

from dbos import DBOS, ScheduleInput
from pydantic import AwareDatetime

from curate_d.domain import DomainModel, OAuthTokens, Provider, ProviderConnection
from curate_d.observability import get_logger
from curate_d.providers.connections import ConnectionRepository, seal_tokens, unseal_tokens
from curate_d.providers.crypto import CredentialCipher
from curate_d.providers.errors import CredentialsRejectedError, ProviderUnavailableError
from curate_d.providers.refresh import (
    RefreshDecision,
    TokenRefresher,
    carry_refresh_token,
    decide_refresh,
)
from curate_d.runtime.queues import QueueName
from curate_d.runtime.services import current

logger = get_logger(__name__)

SCHEDULE_NAME: Final = "connections-refresh"
# Every ten minutes: well inside the default one-hour window, so a pass that finds the
# provider unreachable leaves several more before the token lapses.
SCHEDULE: Final = "*/10 * * * *"

EXPIRED_WITHOUT_REFRESH: Final = (
    "access token expired and the provider issued no refresh credential"
)


class ConnectionStore(Protocol):
    """The connection reads and writes a refresh needs; `ConnectionRepository` in production."""

    async def list_all(self) -> Sequence[ProviderConnection]:
        """Every connection."""
        ...

    async def get(self, provider: Provider, account: str) -> ProviderConnection | None:
        """One connection's record, or None."""
        ...

    async def tokens(self, provider: Provider, account: str) -> OAuthTokens | None:
        """Decrypted tokens for an account, or None."""
        ...

    async def save(
        self,
        provider: Provider,
        account: str,
        tokens: OAuthTokens,
        *,
        api_host: str | None = None,
        refreshed_at: AwareDatetime | None = None,
    ) -> ProviderConnection:
        """Store tokens for an account, leaving it healthy."""
        ...

    async def mark_requires_reauthentication(
        self, provider: Provider, account: str, reason: str
    ) -> bool:
        """Flag an account for a person to re-authorize."""
        ...


class DueConnection(DomainModel):
    """A connection a refresh pass must act on."""

    provider: Provider
    account: str
    decision: RefreshDecision


class AttemptOutcome(StrEnum):
    """What trying to renew one connection's token produced."""

    REFRESHED = "refreshed"
    REJECTED = "rejected"
    UNAVAILABLE = "unavailable"
    EXPIRED = "expired"
    NOT_DUE = "not_due"


class RefreshAttempt(DomainModel):
    """One connection's refresh outcome, safe to checkpoint: tokens only ever sealed."""

    provider: Provider
    account: str
    outcome: AttemptOutcome
    sealed_tokens: bytes | None = None
    reason: str | None = None


async def assess_connections(
    store: ConnectionStore, *, now: AwareDatetime, window: timedelta
) -> list[DueConnection]:
    """Return the connections needing a refresh or a person, in a stable order."""
    due: list[DueConnection] = []
    for record in await store.list_all():
        tokens = await store.tokens(record.provider, record.account)
        if tokens is None:
            continue
        decision = decide_refresh(record, tokens, now=now, window=window)
        if decision in (RefreshDecision.REFRESH, RefreshDecision.REAUTHENTICATE):
            due.append(
                DueConnection(provider=record.provider, account=record.account, decision=decision)
            )
    return due


async def attempt_refresh(
    store: ConnectionStore,
    refreshers: Mapping[Provider, TokenRefresher],
    cipher: CredentialCipher,
    due: DueConnection,
    *,
    now: AwareDatetime,
    window: timedelta,
) -> RefreshAttempt:
    """Try to renew one connection's token, without writing anything.

    The decision is taken again against current state: another pass may have renewed the
    token, or a person re-authorized, since the assessment.
    """

    def attempt(
        outcome: AttemptOutcome, *, sealed: bytes | None = None, reason: str | None = None
    ) -> RefreshAttempt:
        """Build this connection's attempt record."""
        return RefreshAttempt(
            provider=due.provider,
            account=due.account,
            outcome=outcome,
            sealed_tokens=sealed,
            reason=reason,
        )

    record = await store.get(due.provider, due.account)
    tokens = await store.tokens(due.provider, due.account)
    if record is None or tokens is None:
        return attempt(AttemptOutcome.NOT_DUE)
    decision = decide_refresh(record, tokens, now=now, window=window)
    if decision is RefreshDecision.REAUTHENTICATE:
        return attempt(AttemptOutcome.EXPIRED, reason=EXPIRED_WITHOUT_REFRESH)
    if decision is not RefreshDecision.REFRESH:
        return attempt(AttemptOutcome.NOT_DUE)
    refresher = refreshers.get(due.provider)
    if refresher is None:
        # A token that is refreshable but has no refresher here is a deployment gap, not a
        # provider refusal: leave the connection alone until it actually expires.
        return attempt(
            AttemptOutcome.UNAVAILABLE,
            reason=f"no token refresher is configured for {due.provider.value}",
        )
    try:
        renewed = await refresher.refresh(due.account, tokens)
    except CredentialsRejectedError as rejected:
        return attempt(AttemptOutcome.REJECTED, reason=rejected.reason)
    except ProviderUnavailableError as unavailable:
        return attempt(AttemptOutcome.UNAVAILABLE, reason=unavailable.reason)
    return attempt(
        AttemptOutcome.REFRESHED, sealed=seal_tokens(carry_refresh_token(tokens, renewed), cipher)
    )


async def record_attempt(
    store: ConnectionStore,
    cipher: CredentialCipher,
    attempt: RefreshAttempt,
    *,
    refreshed_at: AwareDatetime,
) -> None:
    """Persist what an attempt learned. Unavailable and not-due attempts change nothing."""
    if attempt.outcome is AttemptOutcome.REFRESHED and attempt.sealed_tokens is not None:
        existing = await store.get(attempt.provider, attempt.account)
        await store.save(
            attempt.provider,
            attempt.account,
            unseal_tokens(attempt.sealed_tokens, cipher),
            api_host=None if existing is None else existing.api_host,
            refreshed_at=refreshed_at,
        )
    elif attempt.outcome in (AttemptOutcome.REJECTED, AttemptOutcome.EXPIRED):
        await store.mark_requires_reauthentication(
            attempt.provider,
            attempt.account,
            attempt.reason or "the provider rejected the refresh",
        )


@DBOS.step()
async def _assess_step() -> list[DueConnection]:
    """List the connections due for action, as of now."""
    services = current()
    async with services.datasource.engine.connect() as connection:
        return await assess_connections(
            ConnectionRepository(connection, services.require_cipher()),
            now=services.clock(),
            window=timedelta(minutes=services.settings.connection_refresh_window_minutes),
        )


@DBOS.step()
async def _attempt_step(due: DueConnection) -> RefreshAttempt:
    """Call the provider to renew one token; the outcome is checkpointed, sealed."""
    services = current()
    async with services.datasource.engine.connect() as connection:
        return await attempt_refresh(
            ConnectionRepository(connection, services.require_cipher()),
            services.refreshers,
            services.require_cipher(),
            due,
            now=services.clock(),
            window=timedelta(minutes=services.settings.connection_refresh_window_minutes),
        )


@DBOS.step()
async def _connections_now_step() -> datetime:
    """Read the clock once, so a replay records the same refresh time."""
    return current().clock()


async def _record_transaction(attempt: RefreshAttempt, refreshed_at: datetime) -> None:
    """Write an attempt's outcome inside the ledger datasource transaction."""
    services = current()
    session = services.datasource.sql_session()
    await record_attempt(
        ConnectionRepository(await session.connection(), services.require_cipher()),
        services.require_cipher(),
        attempt,
        refreshed_at=refreshed_at,
    )


@DBOS.workflow()
async def refresh_connections(scheduled_time: datetime, context: object) -> None:
    """Renew every token inside its refresh window; flag the ones only a person can fix."""
    counts: dict[str, int] = {}
    for due in await _assess_step():
        attempt = await _attempt_step(due)
        if attempt.outcome is not AttemptOutcome.NOT_DUE:
            refreshed_at = await _connections_now_step()
            await current().datasource.run_tx_step_async(
                {"name": "record_refresh"}, _record_transaction, attempt, refreshed_at
            )
        counts[attempt.outcome.value] = counts.get(attempt.outcome.value, 0) + 1
        if attempt.outcome in (AttemptOutcome.REJECTED, AttemptOutcome.EXPIRED):
            logger.warning(
                "connection.requires_reauthentication",
                provider=attempt.provider.value,
                account=attempt.account,
                reason=attempt.reason,
            )
    logger.info("connections.refresh_pass", **counts)


def schedule() -> ScheduleInput:
    """The persisted schedule that runs `refresh_connections`, routed to the I/O profile."""
    return {
        "schedule_name": SCHEDULE_NAME,
        "workflow_fn": refresh_connections,
        "schedule": SCHEDULE,
        "context": None,
        "queue_name": QueueName.MAINTENANCE.value,
    }
