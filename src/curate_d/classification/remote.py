"""The remote model tier, through OpenRouter.

Handles only the residue the cheaper tiers could not settle. The model is the one named in
configuration — never automatic routing — so the same file gets the same answer, and the
model that actually answered is recorded on the result. The request asks for structured
output against a schema built from the facts schema, with the taxonomy's values as enums;
the answer is still validated here, because a model that ignores the schema must end in
the review queue, not in the ledger.

What the model may assert is narrower than the facts schema: coordinates, place labels
and event groups come from metadata, clustering and people, never from a model looking at
a picture.
"""

import asyncio
import json
from collections.abc import Mapping
from decimal import Decimal
from enum import StrEnum
from typing import Final, Protocol

import httpx2
from openai import APIConnectionError, APIStatusError, AsyncOpenAI, RateLimitError
from openai.types.chat import ChatCompletionContentPartParam, ChatCompletionMessageParam
from pydantic import Field, SecretStr

from curate_d.classification.deterministic import TierResult
from curate_d.classification.validation import validate_facts
from curate_d.domain import (
    AuditValue,
    ClassificationTier,
    ContentObjectId,
    DomainModel,
    Facts,
    MediaKind,
    RejectedValue,
    Taxonomy,
)

OPENROUTER_BASE_URL: Final = "https://openrouter.ai/api/v1"
# The fields the model is asked for; the rest of the facts are never its to assert.
REMOTE_FIELDS: Final = (
    "media_kind",
    "category",
    "subcategory",
    "title",
    "captured_at",
    "correspondent",
    "language",
)

SYSTEM_PROMPT: Final = (
    "You classify one file from a personal archive. Answer with JSON matching the schema: "
    "facts describing what the file is, and your confidence from 0 to 1 that the facts are "
    "right. Use only the category and subcategory values the schema lists; use null for "
    "anything you cannot determine. Never propose a file name, folder or path. "
    "captured_at is an ISO 8601 timestamp with a UTC offset, or null."
)


class RemoteRequest(DomainModel):
    """What the remote tier is shown about one content object."""

    content_object_id: ContentObjectId
    media_type: str
    paths: tuple[str, ...]
    hints: Mapping[str, AuditValue] = Field(
        default_factory=dict, description="What the cheaper tiers established."
    )
    text_excerpt: str | None = None
    image_jpeg_base64: str | None = Field(
        default=None, description="A downscaled JPEG, prepared on the CPU profile."
    )


class RemoteOutcome(StrEnum):
    """How a remote call ended."""

    ANSWERED = "answered"
    MALFORMED = "malformed"
    REJECTED_VALUE = "rejected_value"
    UNAVAILABLE = "unavailable"


class RemoteResult(DomainModel):
    """A remote call's result, its cost, and why it could not be used if it cannot."""

    outcome: RemoteOutcome
    model_id: str
    result: TierResult | None = None
    rejected: tuple[RejectedValue, ...] = ()
    detail: str | None = None
    cost_usd: Decimal = Decimal(0)


def response_schema(taxonomy: Taxonomy) -> dict[str, object]:
    """The JSON schema the model's answer must follow, closed over the taxonomy."""
    subcategories = sorted({sub for subs in taxonomy.categories.values() for sub in subs})
    nullable_string: dict[str, object] = {"type": ["string", "null"]}
    facts: dict[str, object] = {
        "type": "object",
        "additionalProperties": False,
        "required": list(REMOTE_FIELDS),
        "properties": {
            "media_kind": {"type": "string", "enum": [kind.value for kind in MediaKind]},
            "category": {"type": ["string", "null"], "enum": [*sorted(taxonomy.categories), None]},
            "subcategory": {"type": ["string", "null"], "enum": [*subcategories, None]},
            "title": nullable_string,
            "captured_at": nullable_string,
            "correspondent": nullable_string,
            "language": nullable_string,
        },
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["facts", "confidence"],
        "properties": {
            "facts": facts,
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        },
    }


def _messages(request: RemoteRequest) -> list[ChatCompletionMessageParam]:
    """The conversation: the instructions, then what is known about the file."""
    described = {
        "media_type": request.media_type,
        "file_names": list(request.paths),
        "established_by_cheaper_tiers": dict(request.hints),
        "text_excerpt": request.text_excerpt,
    }
    content: list[ChatCompletionContentPartParam] = [
        {"type": "text", "text": json.dumps(described, ensure_ascii=False, default=str)}
    ]
    if request.image_jpeg_base64 is not None:
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{request.image_jpeg_base64}"},
            }
        )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": content},
    ]


def build_client(api_key: SecretStr, *, http: httpx2.AsyncClient | None = None) -> AsyncOpenAI:
    """An OpenRouter client; OpenRouter speaks the OpenAI chat completions API."""
    return AsyncOpenAI(
        base_url=OPENROUTER_BASE_URL,
        api_key=api_key.get_secret_value(),
        http_client=http,
        max_retries=0,
    )


def _cost(usage: object) -> Decimal:
    """The cost OpenRouter reports in `usage.cost`, or zero where it reports none."""
    value = getattr(usage, "cost", None)
    if value is None and isinstance(getattr(usage, "model_extra", None), Mapping):
        value = usage.model_extra.get("cost")  # type: ignore[attr-defined]
    try:
        return Decimal(str(value)) if value is not None else Decimal(0)
    except ArithmeticError:
        return Decimal(0)


def merge(base: Facts, proposed: Facts) -> Facts:
    """The remote tier's facts over the base, keeping what only metadata may assert."""
    return Facts.model_validate(
        base.model_dump() | {field: getattr(proposed, field) for field in REMOTE_FIELDS}
    )


async def classify_remote(
    client: AsyncOpenAI,
    model: str,
    request: RemoteRequest,
    taxonomy: Taxonomy,
    base: Facts,
) -> RemoteResult:
    """Ask the configured model, and validate what it answers."""
    try:
        completion = await client.chat.completions.create(
            model=model,
            messages=_messages(request),
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "classification",
                    "strict": True,
                    "schema": response_schema(taxonomy),
                },
            },
            extra_body={"usage": {"include": True}},
        )
    except (APIConnectionError, RateLimitError) as error:
        return RemoteResult(
            outcome=RemoteOutcome.UNAVAILABLE, model_id=model, detail=type(error).__name__
        )
    except APIStatusError as error:
        return RemoteResult(
            outcome=RemoteOutcome.UNAVAILABLE,
            model_id=model,
            detail=f"HTTP {error.status_code}",
        )
    answered_by = completion.model or model
    cost = _cost(completion.usage)

    def malformed(detail: str) -> RemoteResult:
        """The answer could not be used."""
        return RemoteResult(
            outcome=RemoteOutcome.MALFORMED, model_id=answered_by, detail=detail, cost_usd=cost
        )

    if not completion.choices or completion.choices[0].message.content is None:
        return malformed("the model returned no content")
    try:
        answer = json.loads(completion.choices[0].message.content)
    except json.JSONDecodeError:
        return malformed("the model's answer is not JSON")
    if not isinstance(answer, Mapping) or set(answer) != {"facts", "confidence"}:
        return malformed("the answer is not an object of facts and confidence")
    confidence = answer["confidence"]
    if isinstance(confidence, bool) or not isinstance(confidence, int | float):
        return malformed("confidence is not a number")
    if not 0 <= confidence <= 1:
        return malformed("confidence is outside 0 to 1")
    raw_facts = answer["facts"]
    if not isinstance(raw_facts, Mapping) or not set(raw_facts) <= set(REMOTE_FIELDS):
        return malformed("facts contain fields outside those the model may assert")
    validation = validate_facts(dict(raw_facts), taxonomy)
    if validation.facts is None:
        return malformed("; ".join(validation.errors))
    if validation.rejected:
        return RemoteResult(
            outcome=RemoteOutcome.REJECTED_VALUE,
            model_id=answered_by,
            rejected=validation.rejected,
            detail="the model proposed values outside the taxonomy",
            cost_usd=cost,
        )
    return RemoteResult(
        outcome=RemoteOutcome.ANSWERED,
        model_id=answered_by,
        cost_usd=cost,
        result=TierResult(
            tier=ClassificationTier.REMOTE_MODEL,
            facts=merge(base, validation.facts),
            confidence=float(confidence),
            evidence={"model": answered_by, "requested_model": model},
            model_id=answered_by,
        ),
    )


class RemoteClassifier(Protocol):
    """Calls the remote tier; `OpenRouterClassifier` in production."""

    async def classify(
        self, model: str, request: RemoteRequest, taxonomy: Taxonomy, base: Facts
    ) -> RemoteResult:
        """Ask `model` about one content object."""
        ...


class OpenRouterClassifier:
    """The remote tier over OpenRouter, with one client per event loop."""

    def __init__(self, api_key: SecretStr) -> None:
        """Hold the key; clients are made on the loop that uses them."""
        self._api_key = api_key
        self._clients: dict[asyncio.AbstractEventLoop, AsyncOpenAI] = {}

    async def classify(
        self, model: str, request: RemoteRequest, taxonomy: Taxonomy, base: Facts
    ) -> RemoteResult:
        """Ask `model`, through this loop's client."""
        loop = asyncio.get_running_loop()
        client = self._clients.get(loop)
        if client is None:
            client = build_client(self._api_key)
            self._clients[loop] = client
        return await classify_remote(client, model, request, taxonomy, base)
