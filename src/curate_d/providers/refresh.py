"""Refreshing access tokens ahead of expiry.

The decision (is a refresh due, and can one happen at all) is a pure function of the
stored record, its tokens and the clock. Carrying it out needs a provider call and a
ledger write, which in a workflow are a step and a datasource transaction; the functions
here keep those two apart so the caller can place each correctly.

pCloud issues tokens that never expire and no refresh token, so it registers no
refresher: its connections are never due, and a revoked token is discovered when a call
is rejected. The mechanism exists for the providers that follow it.
"""

from datetime import timedelta
from enum import StrEnum
from typing import Protocol

from pydantic import AwareDatetime

from curate_d.domain import ConnectionState, OAuthTokens, ProviderConnection


class RefreshDecision(StrEnum):
    """What to do about one connection's access token right now."""

    NOT_DUE = "not_due"
    """The token outlives the refresh window, or never expires."""

    REFRESH = "refresh"
    """Inside the window and a refresh token exists: renew it now."""

    REAUTHENTICATE = "reauthenticate"
    """Expired, and nothing can renew it without a person."""

    BLOCKED = "blocked"
    """Already flagged for re-authentication; trying again cannot help."""


def decide_refresh(
    record: ProviderConnection,
    tokens: OAuthTokens,
    *,
    now: AwareDatetime,
    window: timedelta,
) -> RefreshDecision:
    """Decide whether a connection's token needs renewing.

    A token inside the window without a refresh token is left alone until it actually
    expires: it still works, and health reporting already shows it as expiring.
    """
    if record.state is ConnectionState.REQUIRES_REAUTHENTICATION:
        return RefreshDecision.BLOCKED
    if tokens.expires_at is None or tokens.expires_at - now > window:
        return RefreshDecision.NOT_DUE
    if tokens.is_refreshable:
        return RefreshDecision.REFRESH
    if tokens.expires_at <= now:
        return RefreshDecision.REAUTHENTICATE
    return RefreshDecision.NOT_DUE


class TokenRefresher(Protocol):
    """Renews an access token with a provider."""

    async def refresh(self, account: str, tokens: OAuthTokens) -> OAuthTokens:
        """Return renewed tokens.

        Raises:
            CredentialsRejectedError: the provider refused the refresh token; only a
                person can restore the connection.
            ProviderUnavailableError: the provider could not be reached; try later.
        """
        ...


def carry_refresh_token(previous: OAuthTokens, renewed: OAuthTokens) -> OAuthTokens:
    """Keep the old refresh token when a provider renews without rotating it.

    Several providers answer a refresh with a new access token only. Dropping the refresh
    token then would turn the next expiry into a re-authentication for no reason.
    """
    if renewed.refresh_token is not None or previous.refresh_token is None:
        return renewed
    return renewed.model_copy(update={"refresh_token": previous.refresh_token})
