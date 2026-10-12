"""The remote tier against a stubbed OpenRouter."""

import json
from decimal import Decimal
from uuid import uuid4

import httpx2
import pytest
from openai import AsyncOpenAI
from pydantic import SecretStr

from curate_d.classification import remote
from curate_d.classification.remote import (
    DEFAULT_MAX_OUTPUT_TOKENS,
    OpenRouterClassifier,
    RemoteOutcome,
    RemoteRequest,
    RemoteResult,
    build_client,
    classify_remote,
    response_schema,
)
from curate_d.domain import (
    SEED_TAXONOMY,
    ClassificationTier,
    ContentObjectId,
    Facts,
    GeoPoint,
    MediaKind,
    TaxonomyField,
)

_MODEL = "anthropic/claude-haiku-4-5"


class _OpenRouter:
    """Answers chat completions with a fixed message content."""

    def __init__(
        self,
        content: object,
        *,
        status: int = 200,
        model: str = _MODEL,
        finish_reason: str = "stop",
    ) -> None:
        """Answer every call with `content` as the message."""
        self.content = content
        self.status = status
        self.model = model
        self.finish_reason = finish_reason
        self.requests: list[dict[str, object]] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        """Record the request body and answer."""
        self.requests.append(json.loads(request.content))
        if self.status != 200:
            return httpx2.Response(self.status, json={"error": {"message": "nope"}})
        message = self.content if isinstance(self.content, str) else json.dumps(self.content)
        return httpx2.Response(
            200,
            json={
                "id": "gen-1",
                "object": "chat.completion",
                "created": 1,
                "model": self.model,
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": self.finish_reason,
                        "message": {"role": "assistant", "content": message},
                    }
                ],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 20,
                    "total_tokens": 120,
                    "cost": 0.00042,
                },
            },
        )


def _request() -> RemoteRequest:
    """A document the cheaper tiers could not settle."""
    return RemoteRequest(
        content_object_id=ContentObjectId(uuid4()),
        media_type="application/pdf",
        paths=("/docs/scan.pdf",),
        hints={"media_kind": "document"},
        text_excerpt="Dear patient, your appointment...",
    )


_BASE = Facts(media_kind=MediaKind.DOCUMENT, place=GeoPoint(latitude=1.0, longitude=2.0))


async def _call(openrouter: _OpenRouter) -> RemoteResult:
    """Run the tier against the stub."""
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(openrouter)) as http:
        client = build_client(SecretStr("sk-test"), http=http)
        return await classify_remote(client, _MODEL, _request(), SEED_TAXONOMY, _BASE)


def _answer(**facts: object) -> dict[str, object]:
    """A schema-conforming answer."""
    return {
        "facts": {
            "media_kind": "document",
            "category": None,
            "subcategory": None,
            "title": None,
            "captured_at": None,
            "correspondent": None,
            "language": None,
        }
        | facts,
        "confidence": 0.9,
    }


async def test_a_conforming_answer_is_accepted_with_the_model_recorded() -> None:
    """The configured model is called, and the model that answered is recorded."""
    openrouter = _OpenRouter(
        _answer(category="Documents", subcategory="Medical", language="en"),
        model="anthropic/claude-haiku-4-5-20251001",
    )
    result = await _call(openrouter)

    assert result.outcome is RemoteOutcome.ANSWERED
    assert openrouter.requests[0]["model"] == _MODEL
    tier = result.result
    assert tier is not None
    assert tier.tier is ClassificationTier.REMOTE_MODEL
    assert tier.model_id == "anthropic/claude-haiku-4-5-20251001"
    assert (tier.facts.category, tier.facts.subcategory) == ("Documents", "Medical")
    assert tier.facts.place == _BASE.place
    assert result.cost_usd == Decimal("0.00042")


async def test_the_request_is_constrained_to_the_facts_schema() -> None:
    """Structured output is requested, with the taxonomy as enums and no path field."""
    openrouter = _OpenRouter(_answer())
    await _call(openrouter)
    response_format = openrouter.requests[0]["response_format"]
    assert response_format["type"] == "json_schema"  # type: ignore[index]
    schema = response_format["json_schema"]["schema"]  # type: ignore[index]
    facts = schema["properties"]["facts"]
    assert facts["additionalProperties"] is False
    assert "Documents" in facts["properties"]["category"]["enum"]
    assert "place" not in facts["properties"]
    assert "path" not in json.dumps(schema).lower()


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        json.dumps({"facts": {"media_kind": "document"}}),
        json.dumps({"facts": {"media_kind": "document"}, "confidence": "high"}),
        json.dumps({"facts": {"media_kind": "document"}, "confidence": 1.5}),
        json.dumps({"facts": {"media_kind": "document", "folder": "/x"}, "confidence": 0.9}),
        json.dumps({"facts": {"media_kind": "spreadsheet"}, "confidence": 0.9}),
        json.dumps({"facts": {"media_kind": "document", "place": None}, "confidence": 0.9}),
        json.dumps(["facts"]),
    ],
)
async def test_malformed_output_is_not_accepted(content: str) -> None:
    """Anything that does not conform is refused, never partially trusted."""
    result = await _call(_OpenRouter(content))
    assert result.outcome is RemoteOutcome.MALFORMED
    assert result.result is None
    assert result.detail


async def test_an_out_of_taxonomy_value_is_rejected_with_the_value() -> None:
    """A model ignoring the enum proposes a value that is recorded for review."""
    result = await _call(_OpenRouter(_answer(category="Receipts")))
    assert result.outcome is RemoteOutcome.REJECTED_VALUE
    [rejected] = result.rejected
    assert (rejected.field, rejected.value) == (TaxonomyField.CATEGORY, "Receipts")


@pytest.mark.parametrize("status", [429, 500, 503])
async def test_an_unavailable_service_is_reported_as_such(status: int) -> None:
    """A failed call is not a classification either way."""
    result = await _call(_OpenRouter(_answer(), status=status))
    assert result.outcome is RemoteOutcome.UNAVAILABLE


def test_the_schema_lists_every_taxonomy_value() -> None:
    """Categories and subcategories come from the active taxonomy."""
    schema = response_schema(SEED_TAXONOMY)
    facts = schema["properties"]["facts"]  # type: ignore[index]
    assert set(facts["properties"]["subcategory"]["enum"]) >= {"Finance", "Camera", None}


async def test_the_answer_is_bounded_in_tokens() -> None:
    """Every call names an output ceiling, the default unless one is given."""
    openrouter = _OpenRouter(_answer())
    await _call(openrouter)
    assert openrouter.requests[0]["max_tokens"] == DEFAULT_MAX_OUTPUT_TOKENS
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(openrouter)) as http:
        client = build_client(SecretStr("sk-test"), http=http)
        await classify_remote(client, _MODEL, _request(), SEED_TAXONOMY, _BASE, max_tokens=200)
    assert openrouter.requests[1]["max_tokens"] == 200


async def test_an_answer_cut_off_at_the_token_ceiling_is_malformed() -> None:
    """Even an answer that parses is unusable once the model was stopped mid-way."""
    result = await _call(_OpenRouter(_answer(), finish_reason="length"))
    assert result.outcome is RemoteOutcome.MALFORMED
    assert result.detail == f"output truncated at max_tokens ({DEFAULT_MAX_OUTPUT_TOKENS})"
    assert result.cost_usd == Decimal("0.00042")


async def test_the_classifier_passes_its_configured_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`remote_max_output_tokens` reaches the request through the classifier."""
    openrouter = _OpenRouter(_answer())
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(openrouter)) as http:

        def stubbed(api_key: SecretStr) -> AsyncOpenAI:
            """Build a client over the stub."""
            return build_client(api_key, http=http)

        monkeypatch.setattr(remote, "build_client", stubbed)
        classifier = OpenRouterClassifier(SecretStr("sk-test"), max_output_tokens=321)
        await classifier.classify(_MODEL, _request(), SEED_TAXONOMY, _BASE)
    assert openrouter.requests[0]["max_tokens"] == 321
