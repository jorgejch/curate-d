"""A dry run's drafts are checked against a taxonomy as activating them would be."""

import pickle
from uuid import uuid4

import pytest

from curate_d.configuration.documents import parse_policy
from curate_d.configuration.store import ConfigurationError
from curate_d.domain import SEED_TAXONOMY
from curate_d.placement.dry_run import DryRunRequest
from curate_d.rendering.templates import parse_templates
from curate_d.workflows.dry_run import DraftRefusedError, check_drafts

_DESTINATION = {"name": "drafted", "provider": "pcloud", "account": "1", "root_path": "/D"}


def _policy(category: str) -> DryRunRequest:
    """Return a request whose draft policy selects `category`."""
    rule = {"name": "x", "when": {"category": category}, "destinations": ["drafted"]}
    return DryRunRequest(
        run_id=uuid4(), policy=parse_policy({"destinations": [_DESTINATION], "rules": [rule]})
    )


def test_a_draft_the_taxonomy_lists_passes() -> None:
    """A category the taxonomy lists is accepted, and a request with no draft is too."""
    check_drafts(_policy("Photos"), SEED_TAXONOMY)
    check_drafts(DryRunRequest(run_id=uuid4()), SEED_TAXONOMY)


def test_a_draft_policy_naming_an_unlisted_category_is_refused() -> None:
    """The problem names the policy and the category."""
    with pytest.raises(ConfigurationError, match="category 'Fotos'") as refused:
        check_drafts(_policy("Fotos"), SEED_TAXONOMY)
    assert "policy" in str(refused.value)


def test_a_draft_template_set_naming_an_unlisted_category_is_refused() -> None:
    """Templates are checked against the same taxonomy."""
    templates = parse_templates(
        {
            "version": "draft-1",
            "templates": [{"name": "t", "when": {"category": "Fotos"}, "path": "{original_name}"}],
        }
    )
    with pytest.raises(ConfigurationError, match="category 'Fotos'"):
        check_drafts(DryRunRequest(run_id=uuid4(), templates=templates), SEED_TAXONOMY)


def test_a_refused_draft_error_survives_its_checkpoint() -> None:
    """DBOS pickles a workflow's error; the message comes back."""
    error = pickle.loads(pickle.dumps(DraftRefusedError("invalid policy: category 'Fotos'")))
    assert isinstance(error, DraftRefusedError)
    assert str(error) == "invalid policy: category 'Fotos'"
