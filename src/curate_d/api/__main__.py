"""`python -m curate_d.api`."""

import ipaddress
import logging
import os
import sys

import uvicorn

from curate_d.config import get_settings
from curate_d.observability import configure_logging, configure_tracing
from curate_d.runtime.dbos_app import APPLICATION_VERSION

DEFAULT_PORT = 8000
# Loopback, so a bare run on a workstation does not serve the unauthenticated API to the
# network. The container image sets CURATE_D_API_LISTEN_HOST to all interfaces: inside a
# container, that is what port publishing and a Kubernetes Service need.
DEFAULT_HOST = "127.0.0.1"
SERVICE_NAME = "curate-d-api"

# Not CURATE_D_API_PORT: Kubernetes injects service-link variables for every Service in the
# namespace, and a Kubernetes Service named after the app yields CURATE_D_API_PORT=tcp://<ip>:<port>.
LISTEN_PORT_ENV = "CURATE_D_API_LISTEN_PORT"
LISTEN_HOST_ENV = "CURATE_D_API_LISTEN_HOST"


def listen_port() -> int:
    """Return the port the API listens on, from CURATE_D_API_LISTEN_PORT or the default.

    Raises:
        ValueError: The variable is set but is not a port number.
    """
    raw = os.environ.get(LISTEN_PORT_ENV)
    if raw is None:
        return DEFAULT_PORT
    try:
        port = int(raw)
    except ValueError:
        raise ValueError(f"{LISTEN_PORT_ENV}={raw!r} is not a port number") from None
    if not 0 < port < 65536:
        raise ValueError(f"{LISTEN_PORT_ENV}={raw!r} is outside 1-65535")
    return port


def listen_host() -> str:
    """Return the address the API listens on, from CURATE_D_API_LISTEN_HOST or loopback.

    Only an IP literal is accepted: a host name would make the bound interface depend on the
    resolver at startup.

    Raises:
        ValueError: The variable is set but is not an IPv4 or IPv6 address.
    """
    raw = os.environ.get(LISTEN_HOST_ENV)
    if raw is None:
        return DEFAULT_HOST
    try:
        return str(ipaddress.ip_address(raw))
    except ValueError:
        raise ValueError(f"{LISTEN_HOST_ENV}={raw!r} is not an IP address") from None


def main() -> int:
    """Configure logging and tracing, and run the control plane API under uvicorn.

    Returns:
        Always 0; uvicorn.run blocks until the process is signaled to stop.
    """
    configure_logging(level=logging.INFO, to_logfire=True)
    configure_tracing(
        service_name=SERVICE_NAME,
        service_version=APPLICATION_VERSION,
        environment=get_settings().environment,
    )
    uvicorn.run(
        "curate_d.api.app:create_traced_app",
        factory=True,
        host="0.0.0.0",
        port=listen_port(),
        access_log=False,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
