"""`python -m curate_d.api`."""

import logging
import os
import sys

import uvicorn

from curate_d.config import get_settings
from curate_d.observability import configure_logging, configure_tracing
from curate_d.runtime.dbos_app import APPLICATION_VERSION

DEFAULT_PORT = 8000
SERVICE_NAME = "curate-d-api"

# Not CURATE_D_API_PORT: Kubernetes injects service-link variables for every Service in the
# namespace, and a Kubernetes Service named after the app yields CURATE_D_API_PORT=tcp://<ip>:<port>.
LISTEN_PORT_ENV = "CURATE_D_API_LISTEN_PORT"


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
