"""Geotagged photos cluster into events by time and place."""

from datetime import UTC, datetime, timedelta
from uuid import NAMESPACE_URL, uuid5

import pytest

from curate_d.classification.clustering import (
    ClusterParameters,
    GeoTimePoint,
    cluster,
    distance_km,
)
from curate_d.domain import ContentObjectId, GeoPoint

_PARIS = GeoPoint(latitude=48.8566, longitude=2.3522)
_VERSAILLES = GeoPoint(latitude=48.8049, longitude=2.1204)
_LYON = GeoPoint(latitude=45.7640, longitude=4.8357)
_TOKYO = GeoPoint(latitude=35.6762, longitude=139.6503)


_NAMES: dict[ContentObjectId, str] = {}


def _point(name: str, when: datetime, place: GeoPoint) -> GeoTimePoint:
    """A photo named `name`, registered so assertions can speak in names."""
    content_object_id = ContentObjectId(uuid5(NAMESPACE_URL, name))
    _NAMES[content_object_id] = name
    return GeoTimePoint(content_object_id=content_object_id, captured_at=when, place=place)


def _day(day: int, hour: int) -> datetime:
    """A time in July 2026."""
    return datetime(2026, 7, day, hour, tzinfo=UTC)


@pytest.fixture
def library() -> list[GeoTimePoint]:
    """A Paris weekend with a Versailles day trip, a Lyon visit, and a Tokyo trip."""
    return [
        _point("paris-1", _day(10, 10), _PARIS),
        _point("paris-2", _day(10, 12), _PARIS),
        _point("versailles-1", _day(10, 15), _VERSAILLES),
        _point("paris-3", _day(10, 20), _PARIS),
        _point("lyon-1", _day(14, 9), _LYON),
        _point("lyon-2", _day(14, 11), _LYON),
        _point("lyon-3", _day(14, 13), _LYON),
        _point("tokyo-1", _day(20, 9), _TOKYO),
        _point("tokyo-2", _day(20, 10), _TOKYO),
        _point("stray", _day(25, 9), _PARIS),
    ]


def _groups(assigned: dict[ContentObjectId, str]) -> list[set[str]]:
    """Groups as sets of photo names."""
    by_group: dict[str, set[str]] = {}
    for content_id, group in assigned.items():
        by_group.setdefault(group, set()).add(_NAMES[content_id])
    return sorted(by_group.values(), key=sorted)


def test_the_library_clusters_into_its_expected_events(library: list[GeoTimePoint]) -> None:
    """Paris with its Versailles day trip is one event, Lyon another; small groups are none."""
    assigned = cluster(library)
    assert _groups(assigned) == [
        {"lyon-1", "lyon-2", "lyon-3"},
        {"paris-1", "paris-2", "paris-3", "versailles-1"},
    ]


def test_the_group_id_names_the_first_day_and_is_stable(library: list[GeoTimePoint]) -> None:
    """Order of input does not matter, and a later photo joining keeps the identifier."""
    first = cluster(library)
    again = cluster(list(reversed(library)))
    assert first == again
    extended = [*library, _point("paris-4", _day(10, 22), _PARIS)]
    paris = uuid5(NAMESPACE_URL, "paris-1")
    assert cluster(extended)[ContentObjectId(paris)] == first[ContentObjectId(paris)]
    assert first[ContentObjectId(paris)].startswith("2026-07-10-")


def test_a_long_gap_splits_one_place_into_two_events() -> None:
    """Same city, days apart: two events."""
    points = [_point(f"a{i}", _day(1, 8 + i), _PARIS) for i in range(3)]
    points += [_point(f"b{i}", _day(5, 8 + i), _PARIS) for i in range(3)]
    assert len(set(cluster(points).values())) == 2


def test_parameters_decide_what_counts_as_one_event(library: list[GeoTimePoint]) -> None:
    """A tighter distance separates the Versailles photo; pairs count with a smaller minimum."""
    tight = cluster(library, ClusterParameters(max_distance_km=5.0, min_size=2))
    versailles = ContentObjectId(uuid5(NAMESPACE_URL, "versailles-1"))
    tokyo = ContentObjectId(uuid5(NAMESPACE_URL, "tokyo-1"))
    assert versailles not in tight
    assert tokyo in tight


def test_distance_is_great_circle() -> None:
    """Paris to Lyon is about 392 km."""
    assert distance_km(_PARIS, _LYON) == pytest.approx(392, abs=5)
    assert distance_km(_PARIS, _PARIS) == 0


def test_nothing_in_nothing_out() -> None:
    """An empty library has no events."""
    assert cluster([]) == {}


def test_boundary_gap_is_inclusive() -> None:
    """A gap exactly at the limit still joins the group."""
    points = [
        _point("x0", _day(1, 0), _PARIS),
        _point("x1", _day(1, 0) + timedelta(hours=8), _PARIS),
        _point("x2", _day(1, 0) + timedelta(hours=16), _PARIS),
    ]
    assert len(set(cluster(points).values())) == 1
