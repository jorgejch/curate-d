"""Review endpoints over a fake queue: listing, each resolution, and refusals."""

from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

from curate_d.api.review import Entry
from curate_d.domain import (
    ContentObjectId,
    RejectedValue,
    ReviewReason,
    ReviewStatus,
    TaxonomyField,
)
from tests.unit.api.fakes import FakeReview, backends, client

_REJECTED = RejectedValue(field=TaxonomyField.SUBCATEGORY, value="Receipts", category="Documents")


def _queue() -> tuple[FakeReview, Entry]:
    """A queue holding one open item that rejected a subcategory."""
    entry = Entry(
        id=uuid4(),
        content_object_id=ContentObjectId(uuid4()),
        reason=ReviewReason.REJECTED_VALUE,
        detail="subcategory outside the taxonomy",
        rejected_values=[_REJECTED],
        status=ReviewStatus.OPEN,
        created_at=datetime(2026, 9, 30, tzinfo=UTC),
    )
    return FakeReview(entries={entry.id: entry}), entry


def test_open_items_are_listed_with_their_rejected_values() -> None:
    """What was rejected is there to inform the decision."""
    fake, entry = _queue()
    with client(replace(backends(), review=fake)) as http:
        [listed] = http.get("/review").json()
    assert listed["id"] == str(entry.id)
    assert listed["rejected_values"] == [_REJECTED.model_dump(mode="json")]


def test_an_item_is_resolved_with_facts_given_as_json() -> None:
    """Enum values and timestamps arrive as strings and are parsed."""
    fake, entry = _queue()
    with client(replace(backends(), review=fake)) as http:
        response = http.post(
            f"/review/{entry.id}/facts",
            json={
                "media_kind": "document",
                "category": "Documents",
                "subcategory": "Finance",
                "captured_at": "2024-05-01T09:30:00+02:00",
            },
            headers={"Tailscale-User-Login": "tester"},
        )
    assert response.status_code == 200
    assert response.json()["status"] == "resolved"
    assert response.json()["facts"]["subcategory"] == "Finance"
    assert fake.decided_by == ["tester"]


def test_facts_that_break_the_schema_are_refused_and_the_item_stays() -> None:
    """A malformed timestamp is located; nothing is resolved."""
    fake, entry = _queue()
    with client(replace(backends(), review=fake)) as http:
        response = http.post(
            f"/review/{entry.id}/facts",
            json={"media_kind": "document", "captured_at": "last tuesday"},
        )
    assert response.status_code == 422
    assert any(e["loc"][-1] == "captured_at" for e in response.json()["detail"])
    assert fake.entries[entry.id].status is ReviewStatus.OPEN


def test_a_refused_resolution_answers_with_its_reason() -> None:
    """The backend's refusal, such as a value outside the taxonomy, is reported."""
    fake, entry = _queue()
    fake.refusal = "outside the taxonomy: subcategory 'Receipts'"
    with client(replace(backends(), review=fake)) as http:
        response = http.post(f"/review/{entry.id}/facts", json={"media_kind": "document"})
    assert response.status_code == 422
    assert response.json() == {"detail": "outside the taxonomy: subcategory 'Receipts'"}


def test_promoting_leaves_the_item_open_for_resolution() -> None:
    """The taxonomy grows; the item is resolved in a second step."""
    fake, entry = _queue()
    with client(replace(backends(), review=fake)) as http:
        response = http.post(f"/review/{entry.id}/taxonomy", json={"values": []})
    assert response.status_code == 200
    assert response.json()["taxonomy_version"] == 2
    assert response.json()["entry"]["status"] == "open"


def test_an_item_is_excluded_with_a_reason() -> None:
    """Excluded, with the reason recorded."""
    fake, entry = _queue()
    with client(replace(backends(), review=fake)) as http:
        response = http.post(f"/review/{entry.id}/exclusion", json={"reason": "duplicate scan"})
    assert response.status_code == 200
    assert response.json()["status"] == "excluded"
    assert response.json()["resolution"] == "excluded: duplicate scan"


def test_an_exclusion_without_a_reason_is_refused() -> None:
    """Excluding for good needs a stated reason."""
    fake, entry = _queue()
    with client(replace(backends(), review=fake)) as http:
        response = http.post(f"/review/{entry.id}/exclusion", json={"reason": ""})
    assert response.status_code == 422
    assert fake.entries[entry.id].status is ReviewStatus.OPEN


def test_an_unknown_item_is_not_found() -> None:
    """Every item endpoint says so plainly."""
    missing = uuid4()
    with client() as http:
        responses = [
            http.get(f"/review/{missing}"),
            http.post(f"/review/{missing}/facts", json={"media_kind": "photo"}),
            http.post(f"/review/{missing}/taxonomy", json={}),
            http.post(f"/review/{missing}/exclusion", json={"reason": "x"}),
        ]
    assert [r.status_code for r in responses] == [404, 404, 404, 404]
    assert responses[0].json() == {"detail": f"no review item {missing}"}


def test_selected_values_are_promoted_from_their_json_form() -> None:
    """The field arrives as a string and is parsed; the backend gets the value itself."""
    fake, entry = _queue()
    with client(replace(backends(), review=fake)) as http:
        response = http.post(
            f"/review/{entry.id}/taxonomy",
            json={"values": [_REJECTED.model_dump(mode="json")]},
        )
    assert response.status_code == 200
    assert fake.promoted == [[_REJECTED]]


def test_a_value_with_an_unknown_field_is_refused() -> None:
    """Only taxonomy fields can be promoted."""
    fake, entry = _queue()
    with client(replace(backends(), review=fake)) as http:
        response = http.post(
            f"/review/{entry.id}/taxonomy",
            json={"values": [{"field": "colour", "value": "Blue"}]},
        )
    assert response.status_code == 422
    assert any(e["loc"][-1] == "field" for e in response.json()["detail"])
