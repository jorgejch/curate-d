"""The stored configuration check, over in-memory rows."""

import pytest
from sqlalchemy import exc as sa_exc
from structlog.testing import capture_logs

from curate_d.config import Settings
from curate_d.configuration import verify
from curate_d.configuration.verify import StoredVersion
from curate_d.domain.policy import SEED_POLICY
from curate_d.domain.taxonomy import SEED_TAXONOMY
from curate_d.rendering.templates import SEED_TEMPLATES


def _policy(*, profile: str = "pcloud", **retention: object) -> dict[str, object]:
    """Return a policy document with one destination and one retention rule."""
    return {
        "destinations": [
            {
                "name": "archive",
                "provider": "pcloud",
                "account": "a",
                "root_path": "/",
                "profile": profile,
            }
        ],
        "rules": [{"name": "everything", "destinations": ["archive"]}],
        "retention": [{"name": "keep", **retention}],
    }


def _templates(**when: str) -> dict[str, object]:
    """Return the seed template set as JSON, its first template's conditions replaced."""
    document = SEED_TEMPLATES.model_dump(mode="json")
    document["templates"][0]["when"] = when
    return document


def _readable() -> list[StoredVersion]:
    """Return one readable version in force of each kind."""
    return [
        StoredVersion("taxonomy", 1, True, SEED_TAXONOMY.model_dump(mode="json")),
        StoredVersion("templates", 1, True, SEED_TEMPLATES.model_dump(mode="json")),
        StoredVersion("policy", 1, False, SEED_POLICY.model_dump(mode="json")),
        StoredVersion("policy", 2, True, _policy()),
    ]


@pytest.fixture
def stored(monkeypatch: pytest.MonkeyPatch) -> list[StoredVersion]:
    """Serve main's rows from a list the test fills."""
    rows: list[StoredVersion] = []
    monkeypatch.setattr(verify, "read_stored", lambda settings: rows)
    monkeypatch.setattr(verify, "get_settings", Settings)
    monkeypatch.setattr(verify, "configure_logging", lambda **kwargs: None)
    return rows


def test_every_readable_version_exits_zero(stored: list[StoredVersion]) -> None:
    """Nothing fails, so nothing is listed."""
    stored.extend(_readable())
    with capture_logs() as events:
        assert verify.main([]) == 0
    assert [event["event"] for event in events] == ["configuration_verify.checked"]
    assert events[0]["versions"] == 4


def test_no_stored_versions_exits_zero(stored: list[StoredVersion]) -> None:
    """A database not yet seeded has no version in force that could fail."""
    assert verify.main([]) == 0


def test_an_unreadable_older_version_is_listed_and_still_exits_zero(
    stored: list[StoredVersion],
) -> None:
    """Only a version in force stops a read; an older one is reported as a warning."""
    stored.extend([*_readable(), StoredVersion("policy", 3, False, _policy(profile="other"))])
    with capture_logs() as events:
        assert verify.main([]) == 0
    [listed] = [event for event in events if event["event"] == "configuration_verify.unreadable"]
    assert (listed["kind"], listed["version"], listed["in_force"]) == ("policy", 3, False)
    assert listed["log_level"] == "warning"


def test_a_policy_in_force_with_an_unknown_profile_exits_one(stored: list[StoredVersion]) -> None:
    """The failure names its kind, version and the destination's profile."""
    stored.extend([*_readable()[:3], StoredVersion("policy", 2, True, _policy(profile="other"))])
    with capture_logs() as events:
        assert verify.main([]) == verify.EXIT_IN_FORCE_UNREADABLE
    [listed] = [event for event in events if event["event"] == "configuration_verify.unreadable"]
    assert (listed["kind"], listed["version"], listed["in_force"]) == ("policy", 2, True)
    assert listed["log_level"] == "error"
    assert any(problem.startswith("destinations.0.profile:") for problem in listed["problems"])


def test_templates_in_force_with_an_unknown_media_kind_exit_one(
    stored: list[StoredVersion],
) -> None:
    """A condition no fact can hold fails the template set in force."""
    rows = _readable()
    rows[1] = StoredVersion("templates", 1, True, _templates(media_kind="photos"))
    stored.extend(rows)
    with capture_logs() as events:
        assert verify.main([]) == verify.EXIT_IN_FORCE_UNREADABLE
    [listed] = [event for event in events if event["event"] == "configuration_verify.unreadable"]
    assert (listed["kind"], listed["version"]) == ("templates", 1)
    assert "photos" in "; ".join(listed["problems"])


def test_a_stored_policy_carrying_a_retired_field_reads() -> None:
    """`source_counts_as_copy` is dropped on read, as every stored policy read does."""
    report = verify.verify([StoredVersion("policy", 1, True, _policy(source_counts_as_copy=True))])
    assert report == verify.Report(checked=1, failures=())


def test_a_document_that_is_not_an_object_fails() -> None:
    """A malformed row is reported rather than raised."""
    report = verify.verify([StoredVersion("taxonomy", 4, True, ["not", "a", "taxonomy"])])
    [failure] = report.failures
    assert failure.problems == ("(root): the document is a list, not an object",)
    assert report.exit_code == verify.EXIT_IN_FORCE_UNREADABLE


def test_an_unknown_kind_fails() -> None:
    """A kind this build does not know cannot be read by it."""
    [failure] = verify.verify([StoredVersion("layouts", 1, False, {})]).failures
    assert failure.problems == ("(root): unknown configuration kind 'layouts'",)


def test_a_database_error_exits_two(
    stored: list[StoredVersion], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing was checked, so the command neither passes nor blames a version."""

    def fail(settings: Settings) -> list[StoredVersion]:
        """Simulate an unreachable ledger database."""
        raise sa_exc.OperationalError("SELECT", {}, Exception("connection refused"))

    monkeypatch.setattr(verify, "read_stored", fail)
    with capture_logs() as events:
        assert verify.main([]) == verify.EXIT_DATABASE_UNREADABLE
    assert [event["event"] for event in events] == ["configuration_verify.database_unreadable"]
    assert "connection refused" in events[0]["error"]


def test_arguments_are_refused() -> None:
    """The command takes none; argparse exits 2 before anything connects."""
    with pytest.raises(SystemExit) as raised:
        verify.main(["policy"])
    assert raised.value.code == 2
