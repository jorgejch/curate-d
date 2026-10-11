"""Credential redaction for log output.

Two independent defences, because either one alone fails in practice:

1. Sensitive *keys* are masked wherever they appear in a log event's structure, which
   catches `token="..."` however deeply nested.
2. Sensitive *shapes* are masked wherever they appear in free text, which catches the
   common case of a secret arriving inside an exception message or a request URL that
   nobody thought to treat as structured.

A leaked token is not recoverable by fixing the logger afterwards, so this errs towards
over-masking.
"""

import re
from collections.abc import Mapping, Sequence

MASK = "[REDACTED]"

# Substring match, lowercased: `refresh_token`, `Authorization`, `client_secret` and
# anything else a provider decides to call its credentials all contain one of these.
_SENSITIVE_KEY_FRAGMENTS = (
    "authorization",
    "api_key",
    "apikey",
    "auth",
    "credential",
    "passwd",
    "password",
    "secret",
    "token",
)

# Each pattern is paired with the replacement that keeps its non-secret context intact:
# masking must not cost us the ability to tell which call failed.
_SENSITIVE_TEXT_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    # Authorization headers, however the scheme is spelled.
    (re.compile(r"(?i)\b(bearer|basic|token)\s+[A-Za-z0-9\-._~+/=]{8,}"), rf"\1 {MASK}"),
    # `token=...`, `access_token: ...` inside free text.
    (
        re.compile(
            r"(?i)\b([a-z0-9_-]*(?:token|secret|password|passwd|api[_-]?key|credential)"
            r"[a-z0-9_-]*)\s*[=:]\s*[\"']?[^\s\"'&,}]{4,}"
        ),
        rf"\1={MASK}",
    ),
    # Credentials embedded in a URL's userinfo.
    (re.compile(r"(?i)\b([a-z][a-z0-9+.-]*)://([^/\s:@]+):([^/\s@]+)@"), rf"\1://\2:{MASK}@"),
    # Command-line form, where the separator is a space rather than = or :. Restricted to
    # long flags so ordinary prose ("password reset requested") is not mangled.
    (
        re.compile(
            r"(?i)(--[a-z0-9-]*(?:token|secret|password|passwd|api-?key|credential)"
            r"[a-z0-9-]*)\s+\S{4,}"
        ),
        rf"\1 {MASK}",
    ),
)

_MAX_DEPTH = 12

type Redactable = (
    str | int | float | bool | Mapping[str, "Redactable"] | Sequence["Redactable"] | None
)


def is_sensitive_key(key: str) -> bool:
    """Whether a field name suggests its value is a credential."""
    lowered = key.lower()
    return any(fragment in lowered for fragment in _SENSITIVE_KEY_FRAGMENTS)


def redact_text(text: str) -> str:
    """Mask credential-shaped substrings in free text."""
    for pattern, replacement in _SENSITIVE_TEXT_RULES:
        text = pattern.sub(replacement, text)
    return text


def redact(value: Redactable, *, _depth: int = 0) -> Redactable:
    """Recursively mask credentials in a log event's payload.

    Recursion is bounded: a self-referential structure must not take the logger, and
    therefore the process, down with it.
    """
    if _depth >= _MAX_DEPTH:
        return MASK
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Mapping):
        return {
            key: MASK if is_sensitive_key(key) else redact(item, _depth=_depth + 1)
            for key, item in value.items()
        }
    # str is a Sequence, so it must be handled above this branch.
    if isinstance(value, Sequence):
        return [redact(item, _depth=_depth + 1) for item in value]
    return value
