"""Starting and completing a pCloud authorization against a stubbed provider."""

import json
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from pydantic import AwareDatetime, HttpUrl, SecretStr

from curate_d.config import Settings
from curate_d.domain import OAuthTokens, Provider, ProviderConnection
from curate_d.providers.authorization import (
    AuthorizationNotConfiguredError,
    AuthorizationStateError,
    AuthorizationStates,
    begin_pcloud_authorization,
    complete_pcloud_authorization,
    pcloud_oauth_config,
)
from curate_d.providers.crypto import CredentialCipher, generate_key
from curate_d.providers.errors import ProviderRequestError, ProviderUnavailableError
from curate_d.providers.pcloud.oauth import PCloudOAuthConfig, exchange_code

_SECRET = "client-secret-value"


@pytest.fixture
def cipher() -> CredentialCipher:
    """A cipher built from a freshly generated key."""
    return CredentialCipher(SecretStr(generate_key()))


@pytest.fixture
def states(cipher: CredentialCipher) -> AuthorizationStates:
    """States redeemable for ten minutes."""
    return AuthorizationStates(cipher, max_age_seconds=600)


@pytest.fixture
def config() -> PCloudOAuthConfig:
    """A pCloud app registration pointing at the callback."""
    return PCloudOAuthConfig(
        client_id="client-1",
        client_secret=SecretStr(_SECRET),
        redirect_uri=HttpUrl("https://curate-d.example/connections/pcloud/callback"),
    )


class _Saver:
    """Records what a completed authorization persists."""

    def __init__(self) -> None:
        """Start with nothing saved."""
        self.saved: list[tuple[str, OAuthTokens, str | None]] = []

    async def save(
        self,
        provider: Provider,
        account: str,
        tokens: OAuthTokens,
        *,
        api_host: str | None = None,
        refreshed_at: AwareDatetime | None = None,
    ) -> ProviderConnection:
        """Record the save and return the record a repository would."""
        self.saved.append((account, tokens, api_host))
        return ProviderConnection(provider=provider, account=account, api_host=api_host)


class _PCloud:
    """A stub of pCloud's token and userinfo endpoints."""

    def __init__(self, body: dict[str, object], userinfo: dict[str, object] | None = None) -> None:
        """Answer every exchange with `body`, and userinfo with `userinfo`."""
        self.body = body
        self.userinfo = userinfo if userinfo is not None else {"result": 0, "email": "e"}
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        """Record the request and answer it."""
        self.requests.append(request)
        if request.url.path == "/userinfo":
            return httpx.Response(200, json=self.userinfo)
        return httpx.Response(200, json=self.body)


def _ok() -> dict[str, object]:
    """A successful token exchange body."""
    return {"result": 0, "access_token": "issued-token", "token_type": "bearer", "uid": 4242}


def test_the_consent_url_carries_client_redirect_and_state(
    config: PCloudOAuthConfig, states: AuthorizationStates
) -> None:
    """The browser is sent to pCloud with everything the callback needs."""
    pending = begin_pcloud_authorization(config, states)
    url = urlparse(pending.redirect_url)
    query = parse_qs(url.query)
    assert url.netloc == "my.pcloud.com"
    assert query["client_id"] == ["client-1"]
    assert query["response_type"] == ["code"]
    assert query["redirect_uri"] == [str(config.redirect_uri)]
    assert query["state"] == [pending.state]
    assert _SECRET not in pending.redirect_url


def test_a_state_is_redeemed_by_the_browser_that_started_it(
    states: AuthorizationStates,
) -> None:
    """The issued state and nonce are accepted together."""
    state, nonce = states.issue(Provider.PCLOUD)
    states.redeem(state, provider=Provider.PCLOUD, browser_nonce=nonce.get_secret_value())


def test_a_state_without_the_browser_nonce_is_refused(states: AuthorizationStates) -> None:
    """A callback from another browser cannot finish the authorization."""
    state, _ = states.issue(Provider.PCLOUD)
    with pytest.raises(AuthorizationStateError, match="browser"):
        states.redeem(state, provider=Provider.PCLOUD, browser_nonce=None)
    with pytest.raises(AuthorizationStateError, match="browser"):
        states.redeem(state, provider=Provider.PCLOUD, browser_nonce="another")


@pytest.mark.parametrize("state", ["forged", "", "✓"])
def test_a_forged_state_is_refused(states: AuthorizationStates, state: str) -> None:
    """A state not issued under this key is refused."""
    with pytest.raises(AuthorizationStateError, match="invalid or expired"):
        states.redeem(state, provider=Provider.PCLOUD, browser_nonce="n")


def test_a_state_from_another_deployment_is_refused(states: AuthorizationStates) -> None:
    """A state sealed under another key is refused."""
    other = AuthorizationStates(CredentialCipher(SecretStr(generate_key())), max_age_seconds=600)
    state, nonce = other.issue(Provider.PCLOUD)
    with pytest.raises(AuthorizationStateError):
        states.redeem(state, provider=Provider.PCLOUD, browser_nonce=nonce.get_secret_value())


def test_an_expired_state_is_refused(
    cipher: CredentialCipher, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A state older than the configured age is refused."""
    states = AuthorizationStates(cipher, max_age_seconds=60)
    state, nonce = states.issue(Provider.PCLOUD)
    real_time = __import__("time").time
    monkeypatch.setattr("time.time", lambda: real_time() + 120)
    with pytest.raises(AuthorizationStateError, match="expired"):
        states.redeem(state, provider=Provider.PCLOUD, browser_nonce=nonce.get_secret_value())


def test_a_state_whose_payload_is_not_a_state_is_refused(
    cipher: CredentialCipher, states: AuthorizationStates
) -> None:
    """A validly sealed value of the wrong shape is still refused."""
    state = cipher.encrypt(SecretStr(json.dumps({"unexpected": 1}))).decode()
    with pytest.raises(AuthorizationStateError):
        states.redeem(state, provider=Provider.PCLOUD, browser_nonce="n")


async def test_completing_authorization_persists_a_usable_connection(
    config: PCloudOAuthConfig, states: AuthorizationStates
) -> None:
    """A good callback exchanges the code and saves the tokens for the pCloud account."""
    pcloud = _PCloud(_ok())
    saver = _Saver()
    pending = begin_pcloud_authorization(config, states)

    async with httpx.AsyncClient(transport=httpx.MockTransport(pcloud)) as http:
        connection = await complete_pcloud_authorization(
            config,
            states,
            http,
            saver,
            code="code-1",
            state=pending.state,
            hostname="eapi.pcloud.com",
            browser_nonce=pending.browser_nonce.get_secret_value(),
        )

    assert connection.account == "4242"
    assert connection.api_host == "eapi.pcloud.com"
    [(account, tokens, api_host)] = saver.saved
    assert (account, api_host) == ("4242", "eapi.pcloud.com")
    assert tokens.access_token.get_secret_value() == "issued-token"
    [request] = pcloud.requests
    assert request.url.host == "eapi.pcloud.com"
    assert _SECRET not in str(request.url)
    assert parse_qs(request.content.decode())["client_secret"] == [_SECRET]


async def test_a_forged_callback_never_sends_the_client_secret(
    config: PCloudOAuthConfig, states: AuthorizationStates
) -> None:
    """The state is checked before any request is made."""
    pcloud = _PCloud(_ok())
    async with httpx.AsyncClient(transport=httpx.MockTransport(pcloud)) as http:
        with pytest.raises(AuthorizationStateError):
            await complete_pcloud_authorization(
                config,
                states,
                http,
                _Saver(),
                code="c",
                state="forged",
                hostname="eapi.pcloud.com",
                browser_nonce="n",
            )
    assert pcloud.requests == []


async def test_a_foreign_hostname_never_receives_the_client_secret(
    config: PCloudOAuthConfig,
) -> None:
    """A callback naming a host that is not pCloud's makes no request."""
    pcloud = _PCloud(_ok())
    async with httpx.AsyncClient(transport=httpx.MockTransport(pcloud)) as http:
        with pytest.raises(ProviderRequestError):
            await exchange_code(config, http, code="c", hostname="collector.example")
    assert pcloud.requests == []


async def test_a_rejected_code_is_a_request_error(config: PCloudOAuthConfig) -> None:
    """pCloud refusing the code is reported, and nothing is saved."""
    pcloud = _PCloud({"result": 2012, "error": "Invalid 'code' provided."})
    async with httpx.AsyncClient(transport=httpx.MockTransport(pcloud)) as http:
        with pytest.raises(ProviderRequestError, match="2012"):
            await exchange_code(config, http, code="c", hostname="api.pcloud.com")


@pytest.mark.parametrize(
    "body",
    [
        {"result": 0, "uid": 1},
        {"result": 0, "access_token": "", "uid": 1},
        {"result": 0, "access_token": "t"},
        {"result": 0, "access_token": "t", "uid": True},
        {"result": 0, "access_token": "t", "uid": 0},
        {"result": 0, "access_token": "t", "userid": "4x"},
    ],
)
async def test_an_incomplete_exchange_is_not_accepted(
    config: PCloudOAuthConfig, body: dict[str, object]
) -> None:
    """A success without a token or an account is not turned into a connection."""
    async with httpx.AsyncClient(transport=httpx.MockTransport(_PCloud(body))) as http:
        with pytest.raises(ProviderUnavailableError):
            await exchange_code(config, http, code="c", hostname="api.pcloud.com")


@pytest.mark.parametrize(
    "identity", [{"uid": 4242}, {"uid": "4242"}, {"userid": 4242}, {"userid": "4242"}]
)
async def test_the_account_is_read_under_either_name(
    config: PCloudOAuthConfig, identity: dict[str, object]
) -> None:
    """`uid` or `userid`, as a number or a numeric string."""
    pcloud = _PCloud({"result": 0, "access_token": "t", **identity})
    async with httpx.AsyncClient(transport=httpx.MockTransport(pcloud)) as http:
        authorization = await exchange_code(config, http, code="c", hostname="api.pcloud.com")
    assert authorization.account == "4242"
    assert [r.url.path for r in pcloud.requests] == ["/oauth2_token"]


async def test_an_exchange_without_the_account_asks_userinfo(config: PCloudOAuthConfig) -> None:
    """With the new token, on the same regional host."""
    pcloud = _PCloud({"result": 0, "access_token": "t"}, {"result": 0, "userid": 4242})
    async with httpx.AsyncClient(transport=httpx.MockTransport(pcloud)) as http:
        authorization = await exchange_code(config, http, code="c", hostname="eapi.pcloud.com")
    assert authorization.account == "4242"
    userinfo = pcloud.requests[-1]
    assert (userinfo.url.host, userinfo.url.path) == ("eapi.pcloud.com", "/userinfo")
    assert userinfo.headers["authorization"] == "Bearer t"


async def test_an_unidentified_account_names_the_fields_not_their_values(
    config: PCloudOAuthConfig,
) -> None:
    """The diagnosis lists what both answers carried, and never the token."""
    pcloud = _PCloud({"result": 0, "access_token": "secret-token", "locationid": 2})
    async with httpx.AsyncClient(transport=httpx.MockTransport(pcloud)) as http:
        with pytest.raises(ProviderUnavailableError) as raised:
            await exchange_code(config, http, code="c", hostname="api.pcloud.com")
    message = str(raised.value)
    assert "access_token, locationid, result" in message
    assert "email, result" in message
    assert "secret-token" not in message


async def test_a_missing_token_type_defaults_to_bearer(config: PCloudOAuthConfig) -> None:
    """pCloud's tokens are bearer tokens whether or not it says so."""
    body = {"result": 0, "access_token": "t", "uid": 1}
    async with httpx.AsyncClient(transport=httpx.MockTransport(_PCloud(body))) as http:
        authorization = await exchange_code(config, http, code="c", hostname="api.pcloud.com")
    assert authorization.tokens.token_type == "bearer"
    assert authorization.tokens.refresh_token is None
    assert authorization.tokens.expires_at is None


def test_the_config_is_read_from_settings() -> None:
    """A fully configured deployment yields the registration."""
    settings = Settings(
        pcloud_client_id="id",
        pcloud_client_secret=SecretStr("s"),
        pcloud_redirect_uri=HttpUrl("https://x.example/cb"),
    )
    assert pcloud_oauth_config(settings).client_id == "id"


def test_an_unconfigured_deployment_refuses_to_authorize() -> None:
    """Missing client settings are named rather than failing at pCloud."""
    with pytest.raises(AuthorizationNotConfiguredError, match="CURATE_D_PCLOUD_CLIENT_ID"):
        pcloud_oauth_config(Settings())
