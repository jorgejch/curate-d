"""Grouping geotagged photos into events by time and place.

Photos taken close together in both time and space belong to one event: a trip, a
wedding, an afternoon at the beach. Sorted by capture time, a photo joins the running
group when it follows the previous photo within `max_gap` and lies within `max_distance_km`
of the group's centre; otherwise it starts a new group. Groups smaller than `min_size`
are not events and get no group.

The identifier is the date of the group's first photo and a digest of that photo's
content identity, so it stays put as later photos join the group and never depends on
the order a pass happened to read rows in.
"""

import hashlib
import math
from collections.abc import Sequence
from datetime import timedelta
from typing import Final

from pydantic import AwareDatetime, Field

from curate_d.domain import ContentObjectId, DomainModel, GeoPoint

_EARTH_RADIUS_KM: Final = 6371.0088


class GeoTimePoint(DomainModel):
    """One photo's capture time and place."""

    content_object_id: ContentObjectId
    captured_at: AwareDatetime
    place: GeoPoint


class ClusterParameters(DomainModel):
    """What counts as the same event."""

    max_gap: timedelta = Field(default=timedelta(hours=8))
    max_distance_km: float = Field(default=50.0, gt=0)
    min_size: int = Field(default=3, ge=2)


def distance_km(a: GeoPoint, b: GeoPoint) -> float:
    """Great-circle distance between two points."""
    phi1, phi2 = math.radians(a.latitude), math.radians(b.latitude)
    dphi = phi2 - phi1
    dlambda = math.radians(b.longitude - a.longitude)
    h = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * _EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(h)))


def _centre(points: Sequence[GeoTimePoint]) -> GeoPoint:
    """The mean position of a group; adequate at the scale of one event."""
    return GeoPoint(
        latitude=sum(p.place.latitude for p in points) / len(points),
        longitude=sum(p.place.longitude for p in points) / len(points),
    )


def group_id(first: GeoTimePoint) -> str:
    """A stable, readable identifier for the group `first` starts."""
    digest = hashlib.sha256(str(first.content_object_id).encode()).hexdigest()[:6]
    return f"{first.captured_at:%Y-%m-%d}-{digest}"


def cluster(
    points: Sequence[GeoTimePoint], parameters: ClusterParameters | None = None
) -> dict[ContentObjectId, str]:
    """Assign each photo in an event its group identifier; photos in no event are absent."""
    parameters = parameters or ClusterParameters()
    ordered = sorted(points, key=lambda p: (p.captured_at, str(p.content_object_id)))
    groups: list[list[GeoTimePoint]] = []
    for point in ordered:
        current = groups[-1] if groups else None
        if (
            current is not None
            and point.captured_at - current[-1].captured_at <= parameters.max_gap
            and distance_km(_centre(current), point.place) <= parameters.max_distance_km
        ):
            current.append(point)
        else:
            groups.append([point])
    assigned: dict[ContentObjectId, str] = {}
    for group in groups:
        if len(group) < parameters.min_size:
            continue
        identifier = group_id(group[0])
        for point in group:
            assigned[point.content_object_id] = identifier
    return assigned
