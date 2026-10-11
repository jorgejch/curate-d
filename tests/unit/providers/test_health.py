"""Connection health is reported as healthy, expiring, or requiring re-authentication."""

from datetime import UTC, datetime, timedelta

from curate_d.domain import ConnectionState, Provider, ProviderConnection
from curate_d.providers.health import assess_health, report

_NOW = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
_WINDOW = timedelta(hours=1)


def _record(
    *,
    expires_in: timedelta | None = None,
    refreshable: bool = True,
    state: ConnectionState = ConnectionState.HEALTHY,
) -> ProviderConnection:
    """A connection expiring `expires_in` from now, or never."""
    return ProviderConnection(
        provider=Provider.PCLOUD,
        account="acct-9",
        state=state,
        expires_at=None if expires_in is None else _NOW + expires_in,
        refreshable=refreshable,
        failure_reason="refresh rejected: invalid_grant"
        if state is ConnectionState.REQUIRES_REAUTHENTICATION
        else None,
    )


def test_a_token_that_never_expires_is_healthy() -> None:
    """pCloud's tokens are reported healthy with no expiry."""
    health = assess_health(_record(), now=_NOW, window=_WINDOW)
    assert health.state is ConnectionState.HEALTHY
    assert health.expires_at is None
    assert not health.requires_attention


def test_a_valid_refreshable_token_is_healthy_with_its_expiry() -> None:
    """A token well outside the window reports its expiry time."""
    health = assess_health(_record(expires_in=timedelta(days=1)), now=_NOW, window=_WINDOW)
    assert health.state is ConnectionState.HEALTHY
    assert health.expires_at == _NOW + timedelta(days=1)


def test_a_refreshable_token_inside_the_window_is_expiring_without_attention() -> None:
    """Renewal is automatic, so nobody needs to act."""
    health = assess_health(_record(expires_in=timedelta(minutes=5)), now=_NOW, window=_WINDOW)
    assert health.state is ConnectionState.EXPIRING
    assert not health.requires_attention


def test_an_unrenewable_token_inside_the_window_asks_for_attention() -> None:
    """A token nothing can renew is flagged before it lapses."""
    health = assess_health(
        _record(expires_in=timedelta(minutes=5), refreshable=False), now=_NOW, window=_WINDOW
    )
    assert health.state is ConnectionState.EXPIRING
    assert health.requires_attention
    assert health.detail is not None
    assert "acct-9" in health.detail


def test_an_expired_unrenewable_token_requires_reauthentication() -> None:
    """Reported at once, not only after the refresh pass flags it."""
    health = assess_health(
        _record(expires_in=timedelta(minutes=-1), refreshable=False), now=_NOW, window=_WINDOW
    )
    assert health.state is ConnectionState.REQUIRES_REAUTHENTICATION
    assert health.requires_attention


def test_a_flagged_connection_names_the_provider_account_and_reason() -> None:
    """The report says which account needs a person and why."""
    health = assess_health(
        _record(state=ConnectionState.REQUIRES_REAUTHENTICATION), now=_NOW, window=_WINDOW
    )
    assert health.state is ConnectionState.REQUIRES_REAUTHENTICATION
    assert health.requires_attention
    assert health.detail == "re-authorize pcloud account acct-9: refresh rejected: invalid_grant"


def test_the_three_states_are_reported_distinctly() -> None:
    """Healthy, expiring and re-authentication-required are never conflated."""
    states = [
        h.state
        for h in report(
            [
                _record(),
                _record(expires_in=timedelta(minutes=5)),
                _record(state=ConnectionState.REQUIRES_REAUTHENTICATION),
            ],
            now=_NOW,
            window=_WINDOW,
        )
    ]
    assert states == [
        ConnectionState.HEALTHY,
        ConnectionState.EXPIRING,
        ConnectionState.REQUIRES_REAUTHENTICATION,
    ]
