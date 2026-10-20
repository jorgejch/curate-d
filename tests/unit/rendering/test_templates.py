"""Template conditions refuse values that could never match."""

import pytest
from pydantic import ValidationError

from curate_d.domain import SEED_TAXONOMY, Facts, MediaKind
from curate_d.rendering.templates import SEED_TEMPLATES, parse_templates


def _document(when: dict[str, str]) -> dict[str, object]:
    """Return a one-template set selecting `when`."""
    return {"version": "v1", "templates": [{"name": "t", "path": "{original_name}", "when": when}]}


def test_a_template_selecting_an_unknown_media_kind_is_refused() -> None:
    """The error locates the template and names the value."""
    document = _document({"media_kind": "photos"})
    with pytest.raises(ValidationError) as refused:
        parse_templates(document)
    error, *_ = refused.value.errors()
    assert error["loc"] == ("templates", 0)
    assert "template 't' selects media_kind 'photos'" in error["msg"]


@pytest.mark.parametrize("kind", list(MediaKind))
def test_a_template_can_select_every_media_kind(kind: MediaKind) -> None:
    """Each `MediaKind` value selects a template."""
    templates = parse_templates(_document({"media_kind": kind.value}))
    assert templates.select(Facts(media_kind=kind)) is templates.templates[0]


def test_the_seed_templates_pass_against_the_seed_taxonomy() -> None:
    """Every value the seeds name is listed."""
    document = SEED_TEMPLATES.model_dump(mode="json")
    assert parse_templates(document, taxonomy=SEED_TAXONOMY) == SEED_TEMPLATES


def test_an_unlisted_category_is_refused_only_against_a_taxonomy() -> None:
    """Reads stay tolerant; activation, which passes the taxonomy, refuses."""
    document = _document({"category": "Fotos"})
    assert parse_templates(document).templates[0].when == {"category": "Fotos"}
    with pytest.raises(ValidationError, match="template 't' selects category 'Fotos'"):
        parse_templates(document, taxonomy=SEED_TAXONOMY)


def test_an_unlisted_subcategory_under_its_category_is_refused_against_a_taxonomy() -> None:
    """A subcategory must be listed under the category the template names."""
    document = _document({"category": "Documents", "subcategory": "Camera"})
    with pytest.raises(ValidationError, match="subcategory 'Camera'"):
        parse_templates(document, taxonomy=SEED_TAXONOMY)
