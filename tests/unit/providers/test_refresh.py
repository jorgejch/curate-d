"""When a token is due for renewal, and what renewal keeps."""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import SecretStr

from curate_d.domain import ConnectionState, OAuthTokens, Provider, ProviderConnection
from curate_d.providers.refresh import RefreshDecision, carry_refresh_token, decide_refresh

_NOW = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
_WINDOW = timedelta(hours=1)


def _record(state: ConnectionState = ConnectionState.HEALTHY) -> ProviderConnection:
    """A connection in `state`."""
    return ProviderConnection(
        provider=Provider.PCLOUD,
        account="a",
        state=state,
        failure_reason=None if state is ConnectionState.HEALTHY else "revoked",
    )


def _tokens(*, expires_in: timedelta | None, refreshable: bool = True) -> OAuthTokens:
    """Tokens expiring `expires_in` from now, or never."""
    return OAuthTokens(
        access_token=SecretStr("a"),
        refresh_token=SecretStr("r") if refreshable else None,
        expires_at=None if expires_in is None else _NOW + expires_in,
    )


@pytest.mark.parametrize(
    ("expires_in", "refreshable", "expected"),
    [
        (None, True, RefreshDecision.NOT_DUE),
        (None, False, RefreshDecision.NOT_DUE),
        (timedelta(hours=2), True, RefreshDecision.NOT_DUE),
        (timedelta(minutes=30), True, RefreshDecision.REFRESH),
        (timedelta(hours=1), True, RefreshDecision.REFRESH),
        (timedelta(minutes=-5), True, RefreshDecision.REFRESH),
        (timedelta(minutes=30), False, RefreshDecision.NOT_DUE),
        (timedelta(0), False, RefreshDecision.REAUTHENTICATE),
        (timedelta(minutes=-5), False, RefreshDecision.REAUTHENTICATE),
    ],
)
def test_the_refresh_decision_follows_expiry_and_refreshability(
    expires_in: timedelta | None, refreshable: bool, expected: RefreshDecision
) -> None:
    """A token is renewed inside the window, and needs a person only once it has expired."""
    decision = decide_refresh(
        _record(), _tokens(expires_in=expires_in, refreshable=refreshable), now=_NOW, window=_WINDOW
    )
    assert decision is expected


def test_a_flagged_connection_is_not_retried() -> None:
    """A connection awaiting re-authentication is left for the person."""
    decision = decide_refresh(
        _record(ConnectionState.REQUIRES_REAUTHENTICATION),
        _tokens(expires_in=timedelta(minutes=1)),
        now=_NOW,
        window=_WINDOW,
    )
    assert decision is RefreshDecision.BLOCKED


def test_a_renewal_without_rotation_keeps_the_old_refresh_token() -> None:
    """The refresh token survives a response that carries only an access token."""
    previous = _tokens(expires_in=timedelta(minutes=1))
    renewed = OAuthTokens(access_token=SecretStr("new"), expires_at=_NOW + timedelta(hours=1))
    kept = carry_refresh_token(previous, renewed)
    assert kept.refresh_token is not None
    assert kept.refresh_token.get_secret_value() == "r"
    assert kept.access_token.get_secret_value() == "new"


def test_a_rotated_refresh_token_replaces_the_old_one() -> None:
    """A provider that rotates refresh tokens has its new one kept."""
    previous = _tokens(expires_in=timedelta(minutes=1))
    renewed = OAuthTokens(access_token=SecretStr("new"), refresh_token=SecretStr("r2"))
    kept = carry_refresh_token(previous, renewed)
    assert kept.refresh_token is not None
    assert kept.refresh_token.get_secret_value() == "r2"


def test_nothing_is_carried_when_there_was_nothing_to_carry() -> None:
    """Renewing a token that had no refresh token leaves the renewal as it is."""
    previous = _tokens(expires_in=timedelta(minutes=1), refreshable=False)
    renewed = OAuthTokens(access_token=SecretStr("new"))
    assert carry_refresh_token(previous, renewed) is renewed
