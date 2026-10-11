"""API entry point: listen-port resolution.

Isolated: uvicorn.run is substituted, because what is under test is which port reaches it.
"""

import pytest

from curate_d.api import __main__ as entry
from curate_d.config import get_settings


@pytest.fixture(autouse=True)
def _clean_port_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start each test with neither port variable set."""
    monkeypatch.delenv(entry.LISTEN_PORT_ENV, raising=False)
    monkeypatch.delenv("CURATE_D_API_PORT", raising=False)


def test_default_port_when_unset() -> None:
    """Listen on 8000 when CURATE_D_API_LISTEN_PORT is unset."""
    assert entry.listen_port() == 8000


def test_explicit_listen_port(monkeypatch: pytest.MonkeyPatch) -> None:
    """Listen on the port CURATE_D_API_LISTEN_PORT names."""
    monkeypatch.setenv(entry.LISTEN_PORT_ENV, "9001")
    assert entry.listen_port() == 9001


def test_kubernetes_service_link_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ignore the CURATE_D_API_PORT service link Kubernetes injects for the API Service."""
    monkeypatch.setenv("CURATE_D_API_PORT", "tcp://10.0.0.1:80")
    assert entry.listen_port() == 8000


@pytest.mark.parametrize("raw", ["tcp://10.0.0.1:80", "", "eighty"])
def test_non_numeric_port_fails_naming_the_variable(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    """Reject a non-numeric CURATE_D_API_LISTEN_PORT with the variable and value named."""
    monkeypatch.setenv(entry.LISTEN_PORT_ENV, raw)
    with pytest.raises(ValueError, match=f"{entry.LISTEN_PORT_ENV}=.*not a port number"):
        entry.listen_port()


@pytest.mark.parametrize("raw", ["0", "65536", "-1"])
def test_out_of_range_port_fails(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    """Reject a CURATE_D_API_LISTEN_PORT outside 1-65535."""
    monkeypatch.setenv(entry.LISTEN_PORT_ENV, raw)
    with pytest.raises(ValueError, match="outside 1-65535"):
        entry.listen_port()


def test_main_passes_the_resolved_port_to_uvicorn(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hand uvicorn the resolved listen port, not the service-link value."""
    ports: list[object] = []
    monkeypatch.setattr(
        "curate_d.api.__main__.uvicorn.run",
        lambda *_args, **kwargs: ports.append(kwargs["port"]),
    )
    monkeypatch.setattr(entry, "configure_logging", lambda **_kwargs: None)
    monkeypatch.setattr(entry, "configure_tracing", lambda **_kwargs: None)
    monkeypatch.setenv("CURATE_D_API_PORT", "tcp://10.0.0.1:80")
    monkeypatch.setenv(entry.LISTEN_PORT_ENV, "8080")

    assert entry.main() == 0
    assert ports == [8080]


def test_main_labels_telemetry_with_the_configured_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hand tracing the environment named by CURATE_D_ENVIRONMENT."""
    calls: list[dict[str, object]] = []
    monkeypatch.setattr("curate_d.api.__main__.uvicorn.run", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(entry, "configure_logging", lambda **_kwargs: None)
    monkeypatch.setattr(entry, "configure_tracing", lambda **kwargs: calls.append(kwargs))
    monkeypatch.setenv("CURATE_D_ENVIRONMENT", "prod")
    get_settings.cache_clear()
    try:
        entry.main()
    finally:
        get_settings.cache_clear()

    assert [call["environment"] for call in calls] == ["prod"]
