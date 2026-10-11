"""pCloud OAuth 2.0 authorization-code flow.

pCloud issues long-lived access tokens with no expiry and no refresh token. A connection
therefore stays healthy until the user revokes the app, at which point the first call
rejected with a credential error flags it for re-authentication.

The callback carries `code`, `state`, `locationid` and `hostname`. The hostname selects
the API host for the account's region and is used for the token exchange; it arrives
through the browser, so it is checked against pCloud's own hosts before our client
secret is sent to it.

pCloud documents the token response as carrying the user id as `uid`; live responses
have been seen without it. The id is therefore read from `uid` or `userid`, as a number
or a numeric string, and failing that asked of `userinfo` with the new token.
"""

from collections.abc import Mapping
from typing import Final
from urllib.parse import urlencode

import httpx
from pydantic import Field, HttpUrl, SecretStr

from curate_d.domain import DomainModel, OAuthTokens, Provider
from curate_d.providers.errors import ProviderUnavailableError
from curate_d.providers.pcloud.api import call, require_api_host

AUTHORIZE_URL: Final = "https://my.pcloud.com/oauth2/authorize"
_OPERATION_EXCHANGE: Final = "exchange authorization code"
_USER_ID_FIELDS: Final = ("uid", "userid")


class PCloudOAuthConfig(DomainModel):
    """The pCloud app registration this deployment authorizes against."""

    client_id: str = Field(min_length=1)
    client_secret: SecretStr
    redirect_uri: HttpUrl


class PCloudAuthorization(DomainModel):
    """The outcome of a completed authorization: which account, where, and its tokens."""

    account: str = Field(min_length=1, description="pCloud user id.")
    api_host: str = Field(min_length=1)
    tokens: OAuthTokens


def authorization_url(config: PCloudOAuthConfig, state: str) -> str:
    """Return the pCloud consent URL the user's browser is sent to."""
    query = urlencode(
        {
            "client_id": config.client_id,
            "response_type": "code",
            "redirect_uri": str(config.redirect_uri),
            "state": state,
        }
    )
    return f"{AUTHORIZE_URL}?{query}"


async def exchange_code(
    config: PCloudOAuthConfig, http: httpx.AsyncClient, *, code: str, hostname: str
) -> PCloudAuthorization:
    """Trade an authorization code for an access token on the account's regional host.

    Raises:
        ProviderRequestError: `hostname` is not a pCloud API host, or pCloud rejected
            the code.
        ProviderUnavailableError: pCloud could not be reached, or answered without the
            fields a successful exchange carries.
    """
    host = require_api_host(hostname, account=None, operation=_OPERATION_EXCHANGE)
    # POSTed as a form rather than query parameters so the secret never appears in a URL,
    # which is where proxies and access logs look.
    body = await call(
        http,
        "POST",
        f"https://{host}/oauth2_token",
        account=None,
        operation=_OPERATION_EXCHANGE,
        data={
            "client_id": config.client_id,
            "client_secret": config.client_secret.get_secret_value(),
            "code": code,
        },
    )
    access_token = body.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise ProviderUnavailableError(
            Provider.PCLOUD, None, _OPERATION_EXCHANGE, "response carries no access_token"
        )
    token = SecretStr(access_token)
    account = user_id(body) or await _user_id_from_userinfo(http, host, token, exchanged=body)
    token_type = body.get("token_type")
    return PCloudAuthorization(
        account=account,
        api_host=host,
        tokens=OAuthTokens(
            access_token=token,
            token_type=token_type if isinstance(token_type, str) else "bearer",
        ),
    )


def user_id(body: Mapping[str, object]) -> str | None:
    """The positive user id in `body`, under either name pCloud uses, if there is one."""
    for field in _USER_ID_FIELDS:
        value = body.get(field)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return str(value)
        if isinstance(value, str) and value.isdecimal() and int(value) > 0:
            return value
    return None


async def _user_id_from_userinfo(
    http: httpx.AsyncClient, host: str, token: SecretStr, *, exchanged: Mapping[str, object]
) -> str:
    """Ask pCloud whose `token` this is, the token response `exchanged` having not said.

    Raises:
        ProviderUnavailableError: the answer carries no user id either; the reason names
            the fields both responses carried, never their values.
    """
    body = await call(
        http,
        "GET",
        f"https://{host}/userinfo",
        account=None,
        operation=_OPERATION_EXCHANGE,
        headers={"Authorization": f"Bearer {token.get_secret_value()}"},
    )
    found = user_id(body)
    if found is None:
        raise ProviderUnavailableError(
            Provider.PCLOUD,
            None,
            _OPERATION_EXCHANGE,
            "neither the token response nor userinfo carries a user id "
            f"(token response fields: {_fields(exchanged)}; userinfo fields: {_fields(body)})",
        )
    return found


def _fields(body: Mapping[str, object]) -> str:
    """The field names of a response, for a diagnosis that must not carry its values."""
    return ", ".join(sorted(body)) or "none"
