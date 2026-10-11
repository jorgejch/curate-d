"""A refresh pass over an in-memory connection store and stubbed refreshers.

Covers the three outcomes that matter: a token renewed ahead of expiry and used from then
on, a refresh the provider rejects, and a provider that cannot be reached.
"""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import AwareDatetime, SecretStr

from curate_d.domain import ConnectionState, OAuthTokens, Provider, ProviderConnection
from curate_d.providers.connections import unseal_tokens
from curate_d.providers.crypto import CredentialCipher, generate_key
from curate_d.providers.errors import CredentialsRejectedError, ProviderUnavailableError
from curate_d.providers.refresh import RefreshDecision, TokenRefresher
from curate_d.workflows.connections import (
    EXPIRED_WITHOUT_REFRESH,
    AttemptOutcome,
    DueConnection,
    assess_connections,
    attempt_refresh,
    record_attempt,
    schedule,
)

_NOW = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
_WINDOW = timedelta(hours=1)


class _Store:
    """Connections held in memory, with the repository's semantics."""

    def __init__(self) -> None:
        """Start empty."""
        self.records: dict[tuple[Provider, str], ProviderConnection] = {}
        self.stored: dict[tuple[Provider, str], OAuthTokens] = {}

    def put(self, account: str, tokens: OAuthTokens, *, api_host: str | None = None) -> None:
        """Seed a healthy connection."""
        self.records[(Provider.PCLOUD, account)] = ProviderConnection(
            provider=Provider.PCLOUD, account=account, api_host=api_host
        )
        self.stored[(Provider.PCLOUD, account)] = tokens

    async def list_all(self) -> Sequence[ProviderConnection]:
        """Every connection, in key order."""
        return [self.records[key] for key in sorted(self.records)]

    async def get(self, provider: Provider, account: str) -> ProviderConnection | None:
        """One connection."""
        return self.records.get((provider, account))

    async def tokens(self, provider: Provider, account: str) -> OAuthTokens | None:
        """One connection's tokens."""
        return self.stored.get((provider, account))

    async def save(
        self,
        provider: Provider,
        account: str,
        tokens: OAuthTokens,
        *,
        api_host: str | None = None,
        refreshed_at: AwareDatetime | None = None,
    ) -> ProviderConnection:
        """Replace tokens and restore health."""
        record = ProviderConnection(
            provider=provider,
            account=account,
            api_host=api_host,
            expires_at=tokens.expires_at,
            last_refreshed_at=refreshed_at,
        )
        self.records[(provider, account)] = record
        self.stored[(provider, account)] = tokens
        return record

    async def mark_requires_reauthentication(
        self, provider: Provider, account: str, reason: str
    ) -> bool:
        """Flag for a person."""
        record = self.records[(provider, account)]
        self.records[(provider, account)] = record.model_copy(
            update={
                "state": ConnectionState.REQUIRES_REAUTHENTICATION,
                "failure_reason": reason,
            }
        )
        return True


class _Refresher:
    """A provider's refresh endpoint, answering with a fixed result."""

    def __init__(self, result: OAuthTokens | Exception) -> None:
        """Answer every refresh with `result`."""
        self.result = result
        self.calls: list[str] = []

    async def refresh(self, account: str, tokens: OAuthTokens) -> OAuthTokens:
        """Return or raise the configured result."""
        self.calls.append(account)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


@pytest.fixture
def cipher() -> CredentialCipher:
    """A cipher built from a freshly generated key."""
    return CredentialCipher(SecretStr(generate_key()))


def _expiring(*, refreshable: bool = True, minutes: int = 10) -> OAuthTokens:
    """Tokens inside the refresh window."""
    return OAuthTokens(
        access_token=SecretStr("old-access"),
        refresh_token=SecretStr("refresh") if refreshable else None,
        expires_at=_NOW + timedelta(minutes=minutes),
    )


def _renewed() -> OAuthTokens:
    """What a provider hands back on a successful refresh."""
    return OAuthTokens(access_token=SecretStr("new-access"), expires_at=_NOW + timedelta(hours=2))


async def _pass(
    store: _Store, refreshers: dict[Provider, TokenRefresher], cipher: CredentialCipher
) -> list[AttemptOutcome]:
    """Run one refresh pass the way the workflow does, returning each outcome."""
    outcomes: list[AttemptOutcome] = []
    for due in await assess_connections(store, now=_NOW, window=_WINDOW):
        attempt = await attempt_refresh(store, refreshers, cipher, due, now=_NOW, window=_WINDOW)
        await record_attempt(store, cipher, attempt, refreshed_at=_NOW)
        outcomes.append(attempt.outcome)
    return outcomes


async def test_a_token_inside_the_window_is_refreshed_and_used_afterwards(
    cipher: CredentialCipher,
) -> None:
    """The renewed token replaces the old one, keeps the refresh token, and stays healthy."""
    store = _Store()
    store.put("a", _expiring(), api_host="eapi.pcloud.com")

    outcomes = await _pass(store, {Provider.PCLOUD: _Refresher(_renewed())}, cipher)

    assert outcomes == [AttemptOutcome.REFRESHED]
    tokens = await store.tokens(Provider.PCLOUD, "a")
    assert tokens is not None
    assert tokens.access_token.get_secret_value() == "new-access"
    assert tokens.refresh_token is not None
    assert tokens.refresh_token.get_secret_value() == "refresh"
    record = await store.get(Provider.PCLOUD, "a")
    assert record is not None
    assert record.state is ConnectionState.HEALTHY
    assert record.api_host == "eapi.pcloud.com"
    assert record.last_refreshed_at == _NOW


async def test_a_rejected_refresh_requires_reauthentication_with_the_reason(
    cipher: CredentialCipher,
) -> None:
    """The provider's refusal flags the connection and records why."""
    store = _Store()
    store.put("a", _expiring())
    rejected = CredentialsRejectedError(Provider.PCLOUD, "a", "refresh", "invalid_grant")

    outcomes = await _pass(store, {Provider.PCLOUD: _Refresher(rejected)}, cipher)

    assert outcomes == [AttemptOutcome.REJECTED]
    record = await store.get(Provider.PCLOUD, "a")
    assert record is not None
    assert record.state is ConnectionState.REQUIRES_REAUTHENTICATION
    assert record.failure_reason == "invalid_grant"
    assert not record.is_usable


async def test_a_flagged_connection_is_not_retried_on_the_next_pass(
    cipher: CredentialCipher,
) -> None:
    """Work stops rather than retrying the refresh indefinitely."""
    store = _Store()
    store.put("a", _expiring())
    refresher = _Refresher(CredentialsRejectedError(Provider.PCLOUD, "a", "refresh", "x"))

    await _pass(store, {Provider.PCLOUD: refresher}, cipher)
    assert await _pass(store, {Provider.PCLOUD: refresher}, cipher) == []
    assert refresher.calls == ["a"]


async def test_an_unreachable_provider_changes_nothing(cipher: CredentialCipher) -> None:
    """A transient failure leaves the still-valid token for the next pass."""
    store = _Store()
    store.put("a", _expiring())
    down = ProviderUnavailableError(Provider.PCLOUD, "a", "refresh", "HTTP 503")

    outcomes = await _pass(store, {Provider.PCLOUD: _Refresher(down)}, cipher)

    assert outcomes == [AttemptOutcome.UNAVAILABLE]
    record = await store.get(Provider.PCLOUD, "a")
    assert record is not None
    assert record.state is ConnectionState.HEALTHY
    tokens = await store.tokens(Provider.PCLOUD, "a")
    assert tokens is not None
    assert tokens.access_token.get_secret_value() == "old-access"


async def test_an_expired_token_without_a_refresh_credential_needs_a_person(
    cipher: CredentialCipher,
) -> None:
    """Nothing can renew it, so the connection is flagged."""
    store = _Store()
    store.put("a", _expiring(refreshable=False, minutes=-1))

    assert await _pass(store, {}, cipher) == [AttemptOutcome.EXPIRED]
    record = await store.get(Provider.PCLOUD, "a")
    assert record is not None
    assert record.failure_reason == EXPIRED_WITHOUT_REFRESH


async def test_tokens_that_never_expire_are_left_alone(cipher: CredentialCipher) -> None:
    """pCloud's non-expiring tokens are never due."""
    store = _Store()
    store.put("a", OAuthTokens(access_token=SecretStr("forever")))
    refresher = _Refresher(_renewed())

    assert await _pass(store, {Provider.PCLOUD: refresher}, cipher) == []
    assert refresher.calls == []


async def test_a_missing_refresher_leaves_the_connection_alone(cipher: CredentialCipher) -> None:
    """A refreshable token with no refresher deployed is not flagged as the user's problem."""
    store = _Store()
    store.put("a", _expiring())

    assert await _pass(store, {}, cipher) == [AttemptOutcome.UNAVAILABLE]
    record = await store.get(Provider.PCLOUD, "a")
    assert record is not None
    assert record.state is ConnectionState.HEALTHY


async def test_an_attempt_rechecks_state_after_the_assessment(cipher: CredentialCipher) -> None:
    """A connection renewed or removed since the assessment is not refreshed again."""
    store = _Store()
    due = DueConnection(provider=Provider.PCLOUD, account="gone", decision=RefreshDecision.REFRESH)
    attempt = await attempt_refresh(store, {}, cipher, due, now=_NOW, window=_WINDOW)
    assert attempt.outcome is AttemptOutcome.NOT_DUE

    store.put("a", OAuthTokens(access_token=SecretStr("fresh"), refresh_token=SecretStr("r")))
    due = DueConnection(provider=Provider.PCLOUD, account="a", decision=RefreshDecision.REFRESH)
    attempt = await attempt_refresh(store, {}, cipher, due, now=_NOW, window=_WINDOW)
    assert attempt.outcome is AttemptOutcome.NOT_DUE


async def test_a_renewed_token_crosses_the_checkpoint_sealed(cipher: CredentialCipher) -> None:
    """The attempt carries ciphertext, never the token, and unseals to the renewal."""
    store = _Store()
    store.put("a", _expiring())
    [due] = await assess_connections(store, now=_NOW, window=_WINDOW)

    attempt = await attempt_refresh(
        store, {Provider.PCLOUD: _Refresher(_renewed())}, cipher, due, now=_NOW, window=_WINDOW
    )

    assert attempt.sealed_tokens is not None
    assert b"new-access" not in attempt.sealed_tokens
    assert "new-access" not in attempt.model_dump_json()
    unsealed = unseal_tokens(attempt.sealed_tokens, cipher)
    assert unsealed.access_token.get_secret_value() == "new-access"


def test_the_schedule_runs_on_the_io_maintenance_queue() -> None:
    """Refresh calls providers, so it must not land on the internal queue every worker reads."""
    assert schedule()["queue_name"] == "maintenance"
