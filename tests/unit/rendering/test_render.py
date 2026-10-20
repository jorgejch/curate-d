"""Rendering facts into locators: templates, determinism, normalization, collisions."""

import ast
import hashlib
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from curate_d.domain import (
    ContentObjectId,
    Facts,
    HashAlgorithm,
    MediaKind,
    Placement,
    StrongFingerprint,
    derive_destination_id,
)
from curate_d.rendering.collision import disambiguate, resolve
from curate_d.rendering.render import RenderError, normalize, render
from curate_d.rendering.templates import (
    PCLOUD_PROFILE,
    PORTABLE_PROFILE,
    SEED_TEMPLATES,
    CapabilityProfile,
    Template,
    TemplateSet,
    parse_templates,
)

_CONTENT = ContentObjectId(UUID("12345678-1234-5678-1234-567812345678"))
_TAKEN = datetime(2026, 7, 14, 10, 11, tzinfo=timezone(timedelta(hours=1)))


def _render(
    facts: Facts,
    *,
    name: str = "IMG_0001.JPG",
    profile: CapabilityProfile = PCLOUD_PROFILE,
    templates: TemplateSet = SEED_TEMPLATES,
) -> str:
    """The locator for `facts`."""
    return render(
        templates,
        facts,
        original_name=name,
        content_object_id=_CONTENT,
        profile=profile,
        root="/Organized",
    ).locator


def test_a_photo_renders_under_its_year_and_month() -> None:
    """The seed photo template files by capture date, in the capture's own offset."""
    facts = Facts(media_kind=MediaKind.PHOTO, category="Photos", captured_at=_TAKEN)
    assert _render(facts) == "/Organized/Photos/2026/2026-07/IMG_0001.JPG"


def test_a_document_renders_under_its_subcategory() -> None:
    """Templates are chosen by their conditions, in order."""
    facts = Facts(media_kind=MediaKind.DOCUMENT, category="Documents", subcategory="Finance")
    assert _render(facts, name="bill.pdf") == "/Organized/Documents/Finance/bill.pdf"


def test_a_missing_required_fact_fails_naming_it() -> None:
    """No default or placeholder is substituted."""
    facts = Facts(media_kind=MediaKind.PHOTO, category="Photos")
    with pytest.raises(RenderError, match="captured_at") as caught:
        _render(facts)
    assert caught.value.missing == ("captured_at",)


def test_an_absent_optional_fact_drops_its_segment() -> None:
    """An optional placeholder with no value leaves no empty folder behind."""
    templates = TemplateSet(
        version="t",
        templates=(Template(name="t", path="A/{event_group}/{original_name}"),),
    )
    facts = Facts(media_kind=MediaKind.PHOTO)
    assert _render(facts, templates=templates) == "/Organized/A/IMG_0001.JPG"


def test_nothing_applying_is_an_error() -> None:
    """A template set with no match refuses rather than inventing a path."""
    templates = TemplateSet(
        version="t",
        templates=(Template(name="t", path="x/{original_name}", when={"media_kind": "audio"}),),
    )
    with pytest.raises(RenderError, match="no template"):
        _render(Facts(media_kind=MediaKind.PHOTO), templates=templates)


@pytest.mark.parametrize(
    "template",
    [
        {"name": "t", "path": "{destination}/{original_name}"},
        {"name": "t", "path": "{original_name}", "required": ["colour"]},
        {"name": "t", "path": "{category:%Y}"},
        {"name": "t", "path": "/abs/{original_name}"},
        {"name": "t", "path": "a/../{original_name}"},
        {"name": "t", "path": "{original_name}", "when": {"title": "x"}},
    ],
)
def test_a_template_naming_unknown_facts_or_escaping_is_rejected(
    template: dict[str, object],
) -> None:
    """Configuration errors are caught at load time."""
    with pytest.raises(ValidationError):
        parse_templates({"version": "v1", "templates": [template]})


def test_the_seed_templates_round_trip_through_json() -> None:
    """The stored form parses back to the same set."""
    assert parse_templates(SEED_TEMPLATES.model_dump(mode="json")) == SEED_TEMPLATES


def test_duplicate_template_names_are_rejected() -> None:
    """Each template is addressable by name."""
    with pytest.raises(ValidationError, match="unique"):
        parse_templates(
            {
                "version": "v",
                "templates": [
                    {"name": "a", "path": "{original_name}"},
                    {"name": "a", "path": "x/{original_name}"},
                ],
            }
        )


def test_disallowed_characters_are_escaped_and_recorded() -> None:
    """A colon becomes %3A on a portable destination; the original is kept."""
    templates = TemplateSet(
        version="t", templates=(Template(name="t", path="{title}/{original_name}"),)
    )
    facts = Facts(media_kind=MediaKind.DOCUMENT, title="Re: invoice/March")
    rendered = render(
        templates,
        facts,
        original_name="a.pdf",
        content_object_id=_CONTENT,
        profile=PORTABLE_PROFILE,
        root="/r",
    )
    assert rendered.locator == "/r/Re%3A invoice%2FMarch/a.pdf"
    [change] = rendered.transformations
    assert change.original == "Re: invoice/March"
    assert change.rule == "escaped"


def test_an_over_long_component_is_shortened_deterministically() -> None:
    """The extension survives, a digest of the original marks it, and it fits."""
    long_name = "x" * 400 + ".pdf"
    shortened, rules = normalize(long_name, PCLOUD_PROFILE)
    assert rules == ["shortened"]
    assert len(shortened.encode()) <= 255
    assert shortened.endswith(f"~{hashlib.sha256(long_name.encode()).hexdigest()[:8]}.pdf")
    assert normalize(long_name, PCLOUD_PROFILE)[0] == shortened


def test_shortening_never_splits_a_multibyte_character() -> None:
    """A name of accented characters is cut at a character boundary."""
    shortened, _ = normalize("é" * 300 + ".jpg", PCLOUD_PROFILE)
    shortened.encode("utf-8").decode("utf-8")
    assert len(shortened.encode()) <= 255


def test_reserved_and_dot_names_are_suffixed() -> None:
    """A folder named '..' or 'CON' cannot be created as given."""
    assert normalize("..", PCLOUD_PROFILE)[0] == ".._"
    assert normalize("con", PORTABLE_PROFILE)[0] == "con_"


def test_unicode_is_composed() -> None:
    """Decomposed and composed spellings render to one name."""
    decomposed = "Café"
    assert normalize(decomposed, PCLOUD_PROFILE) == ("Café", ["nfc"])


def test_a_flat_destination_joins_the_hierarchy_into_one_name() -> None:
    """Without folders, the path becomes a single name."""
    flat = CapabilityProfile(hierarchical=False)
    facts = Facts(media_kind=MediaKind.DOCUMENT, category="Documents", subcategory="Home")
    assert (
        _render(facts, name="lease.pdf", profile=flat) == "/Organized/Documents - Home - lease.pdf"
    )


_facts = st.builds(
    Facts,
    media_kind=st.sampled_from(list(MediaKind)),
    category=st.one_of(st.none(), st.sampled_from(["Photos", "Documents", "Audio", "Videos"])),
    subcategory=st.one_of(st.none(), st.text(min_size=1, max_size=40)),
    title=st.one_of(st.none(), st.text(max_size=300)),
    captured_at=st.one_of(
        st.none(),
        # Hypothesis takes naive bounds and applies `timezones` to what it generates.
        st.datetimes(
            min_value=datetime(1990, 1, 1),  # noqa: DTZ001
            max_value=datetime(2100, 1, 1),  # noqa: DTZ001
            timezones=st.just(UTC),
        ),
    ),
)


@settings(max_examples=300, deadline=None)
@given(facts=_facts, name=st.text(min_size=1, max_size=300).filter(lambda n: "/" not in n))
def test_rendering_is_a_pure_function_of_its_inputs(facts: Facts, name: str) -> None:
    """Repeated renders agree, whatever the facts, and every component fits the profile."""
    templates = TemplateSet(
        version="p",
        templates=(
            Template(
                name="all",
                path="{category}/{subcategory}/{captured_at:%Y}/{title}/{original_name}",
            ),
        ),
    )
    outcomes = []
    for _ in range(3):
        try:
            outcomes.append(
                render(
                    templates,
                    facts,
                    original_name=name,
                    content_object_id=_CONTENT,
                    profile=PORTABLE_PROFILE,
                    root="/r",
                ).locator
            )
        except RenderError as error:
            outcomes.append(f"error: {error}")
    assert outcomes[0] == outcomes[1] == outcomes[2]
    if not outcomes[0].startswith("error"):
        for component in outcomes[0].split("/")[2:]:
            assert component
            assert len(component.encode()) <= 255
            assert not any(c in component for c in '<>:"\\|?*')


def test_rendering_consults_no_external_state() -> None:
    """No clock, randomness, environment, file or network call appears in rendering."""
    package = Path(render.__code__.co_filename).parent
    forbidden = {"now", "today", "time", "random", "uuid4", "getenv", "open", "urandom"}
    for source in sorted(package.glob("*.py")):
        for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call):
                target = node.func
                name = (
                    target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", "")
                )
                assert name not in forbidden, f"{source.name} calls {name}()"
            if isinstance(node, ast.Import | ast.ImportFrom):
                modules = (
                    [a.name for a in node.names]
                    if isinstance(node, ast.Import)
                    else [node.module or ""]
                )
                assert not {"random", "time", "os", "secrets", "httpx"} & {
                    m.split(".")[0] for m in modules
                }


def test_a_new_template_version_rerenders_from_stored_facts_alone() -> None:
    """Changing the layout changes locators and records the new version; no classifier is
    involved, because rendering takes nothing but facts."""
    facts = Facts(media_kind=MediaKind.PHOTO, category="Photos", captured_at=_TAKEN)
    old = render(
        SEED_TEMPLATES,
        facts,
        original_name="a.jpg",
        content_object_id=_CONTENT,
        profile=PCLOUD_PROFILE,
        root="/r",
    )
    relaid = TemplateSet(
        version="seed-2",
        templates=(
            Template(
                name="photos",
                path="Pictures/{captured_at:%Y-%m-%d}/{original_name}",
                required=("captured_at",),
            ),
        ),
    )
    new = render(
        relaid,
        facts,
        original_name="a.jpg",
        content_object_id=_CONTENT,
        profile=PCLOUD_PROFILE,
        root="/r",
    )
    assert (old.locator, old.template_version) == ("/r/Photos/2026/2026-07/a.jpg", "seed-1")
    assert (new.locator, new.template_version) == ("/r/Pictures/2026-07-14/a.jpg", "seed-2")


def _fingerprint(char: str) -> StrongFingerprint:
    """A SHA-256 fingerprint."""
    return StrongFingerprint(algorithm=HashAlgorithm.SHA256, digest=char * 64)


def _occupant(content: ContentObjectId, locator: str) -> Placement:
    """A placement already at `locator`."""
    return Placement.build(
        content_object_id=content,
        destination_id=derive_destination_id("pcloud", "acct-1", "/r"),
        destination_name="d",
        locator=locator,
        template_version="v",
    )


def test_distinct_content_at_an_occupied_locator_gets_a_deterministic_suffix() -> None:
    """The newcomer moves aside by its own fingerprint; the occupant is untouched."""
    other = ContentObjectId(uuid4())
    resolution = resolve(
        "/r/a/photo.jpg",
        _occupant(other, "/r/a/photo.jpg"),
        content_object_id=_CONTENT,
        fingerprint=_fingerprint("b"),
    )
    assert resolution.locator == "/r/a/photo (bbbbbbbb).jpg"
    assert not resolution.already_present
    assert disambiguate("/r/a/photo.jpg", _fingerprint("b")) == resolution.locator


def test_identical_content_at_its_locator_needs_no_transfer() -> None:
    """The existing placement already satisfies the requirement."""
    resolution = resolve(
        "/r/a/photo.jpg",
        _occupant(_CONTENT, "/r/a/photo.jpg"),
        content_object_id=_CONTENT,
        fingerprint=_fingerprint("b"),
    )
    assert resolution.already_present
    assert resolution.locator == "/r/a/photo.jpg"


def test_a_free_locator_is_used_as_rendered() -> None:
    """No occupant, no change."""
    resolution = resolve("/r/x", None, content_object_id=_CONTENT, fingerprint=_fingerprint("c"))
    assert (resolution.locator, resolution.already_present) == ("/r/x", False)
