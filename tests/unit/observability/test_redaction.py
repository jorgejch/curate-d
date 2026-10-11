"""Credential redaction.

Every case here is a way a token has actually reached a log in a real system: bound as a
field, nested in a payload, concatenated into an exception message, or sitting in a URL.
"""

import logging

import pytest
import structlog

from curate_d.observability import MASK, configure_logging, get_logger
from curate_d.observability.redaction import redact, redact_text

TOKEN = "s3cr3t-refresh-token-value"


@pytest.fixture(autouse=True)
def _configured() -> None:
    """Install the JSON-rendering processor chain before each test."""
    configure_logging(json_output=True, level=logging.DEBUG)


def _emit(**fields: object) -> str:
    """Emit one record through the real processor chain and return what was rendered.

    Rendering matters: a processor that masks the event dict but is ordered after the
    renderer would pass a test that only inspected the dict.
    """
    sink = structlog.testing.CapturingLogger()
    logger = structlog.wrap_logger(
        sink,
        processors=structlog.get_config()["processors"],
        wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG),
    )
    logger.bind(**fields).info("provider call failed")
    return str(sink.calls[0].args[0])


def test_a_token_bound_as_a_field_is_masked() -> None:
    """A token bound directly as a log field is masked in the rendered output."""
    rendered = _emit(refresh_token=TOKEN)
    assert TOKEN not in rendered
    assert MASK in rendered


def test_a_token_nested_in_a_payload_is_masked() -> None:
    """A token nested inside a structured payload field is masked."""
    rendered = _emit(response={"data": {"access_token": TOKEN}})
    assert TOKEN not in rendered


def test_a_token_inside_a_message_field_is_masked() -> None:
    """Providers put credentials in error strings; the key name gives no warning."""
    rendered = _emit(detail=f"request failed: Authorization: Bearer {TOKEN}")
    assert TOKEN not in rendered


def test_the_operation_context_survives_redaction() -> None:
    """Redaction must not make a failure unattributable."""
    rendered = _emit(provider="pcloud", account="acct-1", operation="listfolder", token=TOKEN)
    assert "pcloud" in rendered
    assert "acct-1" in rendered
    assert "listfolder" in rendered
    assert TOKEN not in rendered


@pytest.mark.parametrize(
    "text",
    [
        f"Authorization: Bearer {TOKEN}",
        f"token={TOKEN}",
        f"client_secret: {TOKEN}",
        f"--password {TOKEN}",
    ],
)
def test_credential_shapes_in_free_text_are_masked(text: str) -> None:
    """Each credential-shaped substring pattern is masked in free text."""
    assert TOKEN not in redact_text(text)


def test_credentials_in_a_url_userinfo_are_masked() -> None:
    """A password embedded in a URL's userinfo is masked, leaving user and host intact."""
    masked = redact_text("postgresql://curate_d:hunter2@db:5432/curate_d")
    assert "hunter2" not in masked
    assert "curate_d" in masked, "the user and database must remain identifiable"


def test_ordinary_text_is_left_alone() -> None:
    """Text with no credential shape passes through unmodified."""
    assert (
        redact_text("copied 412 files to /Organized/2026") == "copied 412 files to /Organized/2026"
    )


def test_sequences_are_traversed() -> None:
    """A token inside a list element is masked."""
    assert redact([{"api_key": TOKEN}]) == [{"api_key": MASK}]


def test_recursion_is_bounded() -> None:
    """A cycle in a payload must not take the process down."""
    cycle: dict[str, object] = {}
    cycle["self"] = cycle
    assert MASK in str(redact(cycle))  # type: ignore[arg-type]


def test_non_string_scalars_pass_through_unchanged() -> None:
    """Non-string scalar values are returned unchanged."""
    assert redact({"size_bytes": 4096, "verified": True, "ratio": 0.5, "reason": None}) == {
        "size_bytes": 4096,
        "verified": True,
        "ratio": 0.5,
        "reason": None,
    }


def test_console_rendering_also_redacts() -> None:
    """Local development output is a log too."""
    configure_logging(json_output=False, level=logging.DEBUG)
    assert TOKEN not in _emit(access_token=TOKEN)


def test_get_logger_returns_a_bound_logger() -> None:
    """get_logger returns a logger that supports binding fields."""
    assert get_logger("curate_d.test").bind(provider="pcloud") is not None
