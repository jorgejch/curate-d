"""API entry point: listen-port and listen-address resolution.

Isolated: uvicorn.run is substituted, because what is under test is which port and address
reach it.
"""

import pytest

from curate_d.api import __main__ as entry
from curate_d.config import get_settings


@pytest.fixture(autouse=True)
def _clean_port_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start each test with neither port variable nor the listen-host variable set."""
    monkeypatch.delenv(entry.LISTEN_PORT_ENV, raising=False)
    monkeypatch.delenv("CURATE_D_API_PORT", raising=False)
    monkeypatch.delenv(entry.LISTEN_HOST_ENV, raising=False)


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


def test_default_host_when_unset() -> None:
    """Listen on loopback when CURATE_D_API_LISTEN_HOST is unset."""
    assert entry.listen_host() == "127.0.0.1"


@pytest.mark.parametrize("raw", ["0.0.0.0", "::"])
def test_explicit_listen_host(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    """Listen on the address CURATE_D_API_LISTEN_HOST names."""
    monkeypatch.setenv(entry.LISTEN_HOST_ENV, raw)
    assert entry.listen_host() == raw


@pytest.mark.parametrize("raw", ["localhost", "", "0.0.0.0:8000"])
def test_non_ip_host_fails_naming_the_variable(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    """Reject a CURATE_D_API_LISTEN_HOST that is not an IP literal, naming variable and value."""
    monkeypatch.setenv(entry.LISTEN_HOST_ENV, raw)
    with pytest.raises(ValueError, match=f"{entry.LISTEN_HOST_ENV}={raw!r} is not an IP address"):
        entry.listen_host()


@pytest.mark.parametrize("environment", ["local", "dev", "prod"])
def test_environment_label_does_not_choose_the_host(
    monkeypatch: pytest.MonkeyPatch, environment: str
) -> None:
    """Listen on loopback whatever CURATE_D_ENVIRONMENT names, when the host is unset."""
    monkeypatch.setenv("CURATE_D_ENVIRONMENT", environment)
    assert entry.listen_host() == "127.0.0.1"


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


def test_main_passes_the_resolved_host_to_uvicorn(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hand uvicorn the address CURATE_D_API_LISTEN_HOST names."""
    hosts: list[object] = []
    monkeypatch.setattr(
        "curate_d.api.__main__.uvicorn.run",
        lambda *_args, **kwargs: hosts.append(kwargs["host"]),
    )
    monkeypatch.setattr(entry, "configure_logging", lambda **_kwargs: None)
    monkeypatch.setattr(entry, "configure_tracing", lambda **_kwargs: None)
    monkeypatch.setenv(entry.LISTEN_HOST_ENV, "0.0.0.0")

    assert entry.main() == 0
    assert hosts == ["0.0.0.0"]


def test_invalid_host_stops_main_before_uvicorn(monkeypatch: pytest.MonkeyPatch) -> None:
    """An invalid CURATE_D_API_LISTEN_HOST never reaches uvicorn."""
    runs: list[object] = []
    monkeypatch.setattr(
        "curate_d.api.__main__.uvicorn.run", lambda *args, **kwargs: runs.append(kwargs)
    )
    monkeypatch.setattr(entry, "configure_logging", lambda **_kwargs: None)
    monkeypatch.setattr(entry, "configure_tracing", lambda **_kwargs: None)
    monkeypatch.setenv(entry.LISTEN_HOST_ENV, "localhost")

    with pytest.raises(ValueError, match=entry.LISTEN_HOST_ENV):
        entry.main()
    assert runs == []


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
