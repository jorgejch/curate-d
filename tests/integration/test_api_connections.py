"""Connection endpoints over the ledger, with pCloud's token exchange stubbed."""

import random
from collections.abc import AsyncGenerator, AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import replace
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from pydantic import HttpUrl, SecretStr
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine

from curate_d.api.app import create_app
from curate_d.api.backends import Backends, ledger_backends
from curate_d.api.connections import NONCE_COOKIE, LedgerConnections
from curate_d.config import Settings
from curate_d.domain import Provider
from curate_d.ledger.tables import provider_connections
from curate_d.providers.connections import ConnectionRepository
from curate_d.providers.crypto import CredentialCipher, generate_key
from tests.stubs.pcloud import Failure, StubPCloud

pytestmark = pytest.mark.integration


@pytest.fixture
def account() -> int:
    """A pCloud user id no other test uses."""
    return random.randrange(10**8, 10**9)


@pytest.fixture
def pcloud(account: int) -> StubPCloud:
    """The stub pCloud answering the token exchange for `account`."""
    stub = StubPCloud(uid=account)
    stub.access_token = f"secret-token-{account}"
    return stub


@pytest.fixture
def configured(settings: Settings) -> Settings:
    """Settings with pCloud authorization and a credential key."""
    return settings.model_copy(
        update={
            "credential_encryption_key": SecretStr(generate_key()),
            "pcloud_client_id": "client-1",
            "pcloud_client_secret": SecretStr("client-secret"),
            "pcloud_redirect_uri": HttpUrl("https://curate-d.example/connections/pcloud/callback"),
        }
    )


@pytest.fixture
async def cleanup(ledger_engine: AsyncEngine, account: int) -> AsyncIterator[None]:
    """Remove the test's connection afterwards."""
    yield
    async with ledger_engine.begin() as connection:
        await connection.execute(
            delete(provider_connections).where(provider_connections.c.account == str(account))
        )


@contextmanager
def _client(settings: Settings, pcloud: StubPCloud) -> Iterator[TestClient]:
    """The app over the ledger, exchanging codes with `pcloud`."""

    @asynccontextmanager
    async def factory(chosen: Settings, engine: AsyncEngine) -> AsyncGenerator[Backends, None]:
        """Ledger connections whose HTTP client reaches the stub."""
        async with ledger_backends(chosen, engine) as wired, pcloud.client() as http:
            yield replace(wired, connections=LedgerConnections(chosen, engine, http))

    with TestClient(create_app(settings, backends_factory=factory)) as http:
        yield http


def _authorize(http: TestClient) -> tuple[str, str]:
    """Start an authorization; return its state and the browser nonce."""
    started = http.post("/connections/pcloud/authorizations")
    assert started.status_code == 201
    state = parse_qs(urlparse(started.json()["redirect_url"]).query)["state"][0]
    nonce = started.cookies[NONCE_COOKIE]
    return state, nonce


async def test_an_authorized_account_is_listed_healthy_without_its_token(
    configured: Settings, pcloud: StubPCloud, account: int, cleanup: None
) -> None:
    """Start, call back from the same browser, and find the account healthy."""
    with _client(configured, pcloud) as http:
        state, nonce = _authorize(http)
        http.cookies.set(NONCE_COOKIE, nonce)
        finished = http.get(
            "/connections/pcloud/callback",
            params={"code": "c-1", "state": state, "hostname": "eapi.pcloud.com"},
        )
        listed = http.get("/connections")

    assert finished.status_code == 200
    assert finished.json()["account"] == str(account)
    assert finished.json()["state"] == "healthy"
    [ours] = [c for c in listed.json() if c["account"] == str(account)]
    assert (ours["state"], ours["requires_attention"]) == ("healthy", False)
    for response in (finished, listed):
        assert pcloud.access_token not in response.text


async def test_a_revoked_account_requires_attention_and_offers_reauthorization(
    configured: Settings,
    pcloud: StubPCloud,
    account: int,
    ledger_engine: AsyncEngine,
    cleanup: None,
) -> None:
    """Flagged for a person, with the path that restores it; re-authorizing heals it."""
    with _client(configured, pcloud) as http:
        state, nonce = _authorize(http)
        http.cookies.set(NONCE_COOKIE, nonce)
        http.get(
            "/connections/pcloud/callback",
            params={"code": "c-1", "state": state, "hostname": "eapi.pcloud.com"},
        )
        async with ledger_engine.begin() as connection:
            await ConnectionRepository(
                connection, CredentialCipher.from_settings(configured)
            ).mark_requires_reauthentication(Provider.PCLOUD, str(account), "token revoked")

        [flagged] = [c for c in http.get("/connections").json() if c["account"] == str(account)]
        assert flagged["state"] == "requires_reauthentication"
        assert flagged["requires_attention"] is True
        assert "token revoked" in flagged["detail"]
        assert flagged["reauthorize"] == "/connections/pcloud/authorizations"

        state, nonce = _authorize(http)
        http.cookies.set(NONCE_COOKIE, nonce)
        restored = http.get(
            "/connections/pcloud/callback",
            params={"code": "c-2", "state": state, "hostname": "eapi.pcloud.com"},
        )
        [healed] = [c for c in http.get("/connections").json() if c["account"] == str(account)]
    assert restored.status_code == 200
    assert (healed["state"], healed["requires_attention"]) == ("healthy", False)
    assert pcloud.access_token not in restored.text


async def test_a_refused_code_names_the_provider_account_and_operation(
    configured: Settings, pcloud: StubPCloud, account: int, cleanup: None
) -> None:
    """pCloud's refusal comes back as 503 with what failed, and nothing is stored."""
    pcloud.fail("oauth2_token", "", Failure(times=1, body={"result": 2012, "error": "Bad code."}))
    with _client(configured, pcloud) as http:
        state, nonce = _authorize(http)
        http.cookies.set(NONCE_COOKIE, nonce)
        refused = http.get(
            "/connections/pcloud/callback",
            params={"code": "bad", "state": state, "hostname": "eapi.pcloud.com"},
        )
        listed = http.get("/connections").json()
    assert refused.status_code == 503
    detail = refused.json()["detail"]
    assert "pcloud account" in detail
    assert "exchange authorization code failed" in detail
    assert "Bad code" in detail
    assert all(c["account"] != str(account) for c in listed)


async def test_a_callback_from_another_browser_is_rejected(
    configured: Settings, pcloud: StubPCloud, cleanup: None
) -> None:
    """Without the starting browser's nonce, nothing is exchanged."""
    with _client(configured, pcloud) as http:
        state, _ = _authorize(http)
        http.cookies.clear()
        refused = http.get(
            "/connections/pcloud/callback",
            params={"code": "c-1", "state": state, "hostname": "eapi.pcloud.com"},
        )
    assert refused.status_code == 422
    assert pcloud.requests["oauth2_token"] == 0
