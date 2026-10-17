"""Placement policy: where content should be, and when an original may be released.

Destinations are named targets. The name is the policy's label; what a destination is, and
what its placements belong to, is its identity: provider, account and root path; two
destinations sharing files are refused. Placement rules map facts to the destinations
content should have copies at; every matching rule contributes, so policies compose.
Retention is separate: an original is kept unless a retention rule explicitly permits
release, and even then only once enough strongly verified copies exist — counting the
original itself when the policy says it is one of the copies.
"""

from collections.abc import Mapping, Sequence
from itertools import combinations
from typing import Self

from pydantic import Field, field_validator, model_validator

from curate_d.domain.base import DomainModel
from curate_d.domain.facts import Facts
from curate_d.domain.ids import DestinationId, normalize_root
from curate_d.domain.placement import DestinationIdentity, VerificationLevel
from curate_d.domain.source import Provider

_CONDITION_FIELDS = frozenset({"media_kind", "category", "subcategory"})


def _matches(when: Mapping[str, str], facts: Facts) -> bool:
    """Whether every condition matches the facts."""
    values = facts.model_dump(mode="json")
    return all(values.get(field) == value for field, value in when.items())


def _check_conditions(owner: str, when: Mapping[str, str]) -> None:
    """Reject a condition on a fact that cannot select.

    Raises:
        ValueError: the condition names another fact.
    """
    unknown = set(when) - _CONDITION_FIELDS
    if unknown:
        raise ValueError(
            f"{owner} selects on {sorted(unknown)}; only {sorted(_CONDITION_FIELDS)} can select"
        )


class DestinationConfig(DomainModel):
    """A named target content can be placed at.

    The name labels the destination in the policy; `identity` is what placements belong
    to. The root path is stored in its canonical form (see `normalize_root`).
    """

    name: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9-]*$")
    provider: Provider
    account: str = Field(min_length=1)
    root_path: str = Field(min_length=1, pattern=r"^/")
    profile: str = Field(default="pcloud", description="Capability profile name.")

    @field_validator("root_path")
    @classmethod
    def _normalize_root(cls, value: str) -> str:
        """Return the root in canonical form.

        Raises:
            ValueError: the root has an empty, "." or ".." segment.
        """
        return normalize_root(value)

    @property
    def identity(self) -> DestinationIdentity:
        """Return the provider, account and root this name points at."""
        return DestinationIdentity(
            provider=self.provider, account=self.account, root_path=self.root_path
        )

    @property
    def id(self) -> DestinationId:
        """Return the identifier of this destination's identity."""
        return self.identity.id


def check_destinations(destinations: Sequence[DestinationConfig]) -> None:
    """Refuse destinations that repeat a name or share files.

    Raises:
        ValueError: a name repeats; two names point at one identity; or one destination's
            root lies inside another's in the same account, so one file would count as
            a copy at both.
    """
    names = [d.name for d in destinations]
    if len(set(names)) != len(names):
        raise ValueError("destination names must be unique")
    for first, second in combinations(destinations, 2):
        if first.id == second.id:
            raise ValueError(
                f"destinations {first.name!r} and {second.name!r} are the same place "
                f"({first.identity.label()})"
            )
        if first.identity.contains(second.identity) or second.identity.contains(first.identity):
            raise ValueError(
                f"destinations {first.name!r} ({first.identity.label()}) and "
                f"{second.name!r} ({second.identity.label()}) overlap: one root lies "
                "inside the other in the same account"
            )


class PlacementRule(DomainModel):
    """Content matching `when` should have copies at `destinations`."""

    name: str = Field(min_length=1)
    when: Mapping[str, str] = Field(default_factory=dict)
    destinations: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _check(self) -> Self:
        """Reject conditions on facts that cannot select.

        Raises:
            ValueError: a condition names another fact.
        """
        _check_conditions(f"placement rule {self.name!r}", self.when)
        return self


class RetentionRule(DomainModel):
    """For content matching `when`: may its originals be released, and on what terms."""

    name: str = Field(min_length=1)
    when: Mapping[str, str] = Field(default_factory=dict)
    release_source: bool = False
    required_copies: int = Field(default=2, ge=1)
    source_counts_as_copy: bool = True
    minimum_level: VerificationLevel = VerificationLevel.STRONG_IN_TRANSIT

    @model_validator(mode="after")
    def _check(self) -> Self:
        """Reject conditions on unknown facts and a release below the strong rungs.

        Raises:
            ValueError: a condition names another fact, or release would accept a copy
                weaker than the strong rungs.
        """
        _check_conditions(f"retention rule {self.name!r}", self.when)
        if self.release_source and not self.minimum_level.permits_source_release:
            raise ValueError(
                f"retention rule {self.name!r} would release sources on "
                f"{self.minimum_level.name.lower()} copies; only strongly verified copies may"
            )
        return self


class PlacementPolicy(DomainModel):
    """Destinations, placement rules and retention rules."""

    destinations: tuple[DestinationConfig, ...] = ()
    rules: tuple[PlacementRule, ...] = ()
    retention: tuple[RetentionRule, ...] = ()

    @model_validator(mode="after")
    def _check(self) -> Self:
        """Reject duplicate or overlapping destinations and rules naming undefined ones.

        Raises:
            ValueError: a destination name or identity repeats, two destinations overlap
                (see `check_destinations`), or a rule targets an undefined one.
        """
        check_destinations(self.destinations)
        names = [d.name for d in self.destinations]
        for rule in self.rules:
            missing = set(rule.destinations) - set(names)
            if missing:
                raise ValueError(
                    f"placement rule {rule.name!r} targets undefined {sorted(missing)}"
                )
        return self

    def destination(self, name: str) -> DestinationConfig | None:
        """A destination by name."""
        return next((d for d in self.destinations if d.name == name), None)

    def desired_destinations(self, facts: Facts) -> tuple[str, ...]:
        """Every destination a matching rule asks for, in a stable order."""
        wanted = {
            name for rule in self.rules if _matches(rule.when, facts) for name in rule.destinations
        }
        return tuple(sorted(wanted))

    def retention_for(self, facts: Facts) -> RetentionRule | None:
        """The first retention rule that applies, or None: retain by default."""
        return next((rule for rule in self.retention if _matches(rule.when, facts)), None)


# No destinations: nothing is placed until a person configures where things go.
SEED_POLICY = PlacementPolicy()
