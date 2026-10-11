"""Starting an authorization needs settings and the credential key, not the ledger."""

from typing import cast

import httpx
import pytest
from pydantic import HttpUrl, SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine

from curate_d.api.connections import LedgerConnections
from curate_d.api.errors import UnavailableError
from curate_d.config import Settings
from curate_d.domain import Provider
from curate_d.providers.crypto import generate_key


def _backend(**settings: object) -> LedgerConnections:
    """A backend over `settings`, with no ledger or HTTP client behind it."""
    return LedgerConnections(
        Settings().model_copy(update=settings),
        cast(AsyncEngine, None),
        cast(httpx.AsyncClient, None),
    )


_PCLOUD = {
    "pcloud_client_id": "client-1",
    "pcloud_client_secret": SecretStr("client-secret"),
    "pcloud_redirect_uri": HttpUrl("https://curate-d.example/connections/pcloud/callback"),
}


def test_without_a_credential_key_authorization_is_unavailable() -> None:
    """States are sealed under the key, so none can be issued without it."""
    with pytest.raises(UnavailableError):
        _backend(credential_encryption_key=None, **_PCLOUD).begin(Provider.PCLOUD)


def test_without_a_pcloud_client_authorization_is_unavailable() -> None:
    """The message names the settings to supply."""
    backend = _backend(
        credential_encryption_key=SecretStr(generate_key()),
        pcloud_client_id=None,
        pcloud_client_secret=None,
        pcloud_redirect_uri=None,
    )
    with pytest.raises(UnavailableError, match="CURATE_D_PCLOUD_CLIENT_ID"):
        backend.begin(Provider.PCLOUD)


def test_a_configured_backend_hands_out_a_consent_url_and_a_nonce() -> None:
    """The consent URL names our client and carries a state; the nonce is separate."""
    started = _backend(credential_encryption_key=SecretStr(generate_key()), **_PCLOUD).begin(
        Provider.PCLOUD
    )
    url = httpx.URL(started.redirect_url)
    assert url.params["client_id"] == "client-1"
    assert url.params["state"]
    nonce = started.browser_nonce.get_secret_value()
    assert nonce
    assert nonce not in started.redirect_url
