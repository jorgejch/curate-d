"""Connection endpoints over fake backends: health, attention, and the authorization flow."""

from dataclasses import replace
from datetime import UTC, datetime

from curate_d.api.backends import Backends
from curate_d.api.connections import NONCE_COOKIE
from curate_d.api.errors import UnavailableError
from curate_d.domain import ConnectionHealth, ConnectionState, Provider
from tests.unit.api.fakes import FakeConnections, backends, client


def _wired(fake: FakeConnections) -> Backends:
    """Backends with `fake` as the connections."""
    return replace(backends(), connections=fake)


def test_a_connection_needing_a_person_offers_the_way_to_restore_it() -> None:
    """Requires attention, with the authorization path to start again."""
    fake = FakeConnections(
        connections=[
            ConnectionHealth(
                provider=Provider.PCLOUD,
                account="42",
                state=ConnectionState.REQUIRES_REAUTHENTICATION,
                requires_attention=True,
                detail="re-authorize pcloud account 42: revoked",
            ),
            ConnectionHealth(
                provider=Provider.PCLOUD,
                account="43",
                state=ConnectionState.HEALTHY,
                expires_at=datetime(2027, 1, 1, tzinfo=UTC),
            ),
        ]
    )
    with client(_wired(fake)) as http:
        listed = http.get("/connections").json()
    assert [(c["account"], c["state"], c["requires_attention"]) for c in listed] == [
        ("42", "requires_reauthentication", True),
        ("43", "healthy", False),
    ]
    assert listed[0]["reauthorize"] == "/connections/pcloud/authorizations"
    assert listed[1]["reauthorize"] is None


def test_starting_an_authorization_binds_the_browser() -> None:
    """The consent URL comes back and the nonce is set as an HTTP-only cookie."""
    with client(_wired(FakeConnections(nonce="n-1"))) as http:
        response = http.post("/connections/pcloud/authorizations")
    assert response.status_code == 201
    assert response.json() == {
        "provider": "pcloud",
        "redirect_url": "https://consent.example/pcloud",
    }
    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f"{NONCE_COOKIE}=n-1")
    assert "HttpOnly" in cookie
    assert "Secure" in cookie
    assert "n-1" not in response.text


def test_the_callback_stores_the_connection_from_the_starting_browser() -> None:
    """With the nonce cookie, the account comes back healthy and the cookie is cleared."""
    fake = FakeConnections(nonce="n-1")
    with client(_wired(fake)) as http:
        http.cookies.set(NONCE_COOKIE, "n-1")
        response = http.get(
            "/connections/pcloud/callback",
            params={"code": "77", "state": "s", "hostname": "eapi.pcloud.com"},
        )
    assert response.status_code == 200
    assert response.json()["account"] == "77"
    assert response.json()["state"] == "healthy"
    assert f'{NONCE_COOKIE}=""' in response.headers["set-cookie"]
    assert [c.account for c in fake.connections] == ["77"]


def test_a_callback_without_the_browser_nonce_is_rejected() -> None:
    """Another browser cannot finish someone else's authorization."""
    fake = FakeConnections(nonce="n-1")
    with client(_wired(fake)) as http:
        response = http.get(
            "/connections/pcloud/callback",
            params={"code": "77", "state": "s", "hostname": "eapi.pcloud.com"},
        )
    assert response.status_code == 422
    assert "browser" in response.json()["detail"]
    assert fake.connections == []


def test_an_unconfigured_provider_is_unavailable_with_the_reason() -> None:
    """Missing OAuth settings answer 503 naming what is missing."""
    fake = FakeConnections(failure=UnavailableError("pCloud authorization needs X"))
    with client(_wired(fake)) as http:
        response = http.post("/connections/pcloud/authorizations")
    assert response.status_code == 503
    assert response.json() == {"detail": "pCloud authorization needs X"}


def test_an_unknown_provider_is_rejected() -> None:
    """Only providers the system knows can be authorized."""
    with client() as http:
        response = http.post("/connections/google_drive/authorizations")
    assert response.status_code == 422
