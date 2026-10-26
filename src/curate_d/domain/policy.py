"""Placement policy: where content should be, and when an original may be released.

Destinations are named targets. The name is the policy's label; what a destination is, and
what its placements belong to, is its identity: provider, account and canonical root path.
Two destinations sharing files, one identity under two names or one root inside another in
the same account, are refused (`check_destinations`). Placement rules map facts to the
destinations content should have copies at; every matching rule contributes, so policies
compose. Retention is separate: an original is kept unless a retention rule explicitly
permits release, and even then only once the required number of strongly verified copies
exist besides the original, at destination identities the policy defines.

Every value a policy names is checked when it is parsed: a condition's `media_kind` must
be a `MediaKind`, and a destination's `profile` the value of a `CapabilityProfileName`.
Category and subcategory conditions are checked against the taxonomy only when a validation
context carries one, which activation does (`domain.conditions`).

A destination's `profile` stays declared a `str`: `DestinationConfig` is checkpointed in
workflow inputs, a checkpoint holds the plain string older builds wrote, and unpickling does
not validate it. A reader that needs the member converts at use.
"""

from collections.abc import Mapping, Sequence
from enum import StrEnum
from itertools import combinations
from typing import Final, Self

from pydantic import Field, ValidationInfo, field_validator, model_validator
from pydantic_core import PydanticCustomError

from curate_d.domain.base import DomainModel
from curate_d.domain.conditions import check_conditions, matches, taxonomy_in
from curate_d.domain.facts import Facts
from curate_d.domain.ids import DestinationId, normalize_root
from curate_d.domain.placement import DestinationIdentity, VerificationLevel
from curate_d.domain.source import Provider


class CapabilityProfileName(StrEnum):
    """The capability profiles a destination can name.

    Rendering holds a profile for each (`rendering.templates.PROFILES`); the domain holds
    only the names, so a policy naming another is refused when it is parsed.
    """

    PCLOUD = "pcloud"
    PORTABLE = "portable"


_PROFILE_NAMES: Final = tuple(member.value for member in CapabilityProfileName)
# Pydantic's own wording for an enum mismatch, so a refusal reads as it did when the field
# was typed by the enum.
_PROFILES_EXPECTED: Final = (
    ", ".join(map(repr, _PROFILE_NAMES[:-1])) + " or " + repr(_PROFILE_NAMES[-1])
)


class DestinationConfig(DomainModel):
    """A named target content can be placed at.

    The name labels the destination in the policy; `identity` is what placements belong
    to. The root path is held in its canonical form (`normalize_root`). A checkpointed
    config is not revalidated on unpickling, so `identity` normalizes again.
    """

    name: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9-]*$")
    provider: Provider
    account: str = Field(min_length=1)
    root_path: str = Field(min_length=1)
    profile: str = Field(
        default=CapabilityProfileName.PCLOUD.value,
        description="Capability profile name: the value of a `CapabilityProfileName`.",
    )

    @field_validator("profile")
    @classmethod
    def _check_profile(cls, value: str) -> str:
        """Reject a profile no `CapabilityProfileName` names.

        Raises:
            PydanticCustomError: `value` names no profile. Its type is `enum` and its
                message the one the field typed by the enum gave.
        """
        if value not in _PROFILE_NAMES:
            raise PydanticCustomError(
                "enum", "Input should be {expected}", {"expected": _PROFILES_EXPECTED}
            )
        return value

    @field_validator("root_path")
    @classmethod
    def _normalize_root(cls, value: str, info: ValidationInfo[object]) -> str:
        """Return the root in canonical form; see `normalize_root`.

        Raises:
            ValueError: the root is not absolute or has an empty, "." or ".." segment;
                the message names the destination.
        """
        try:
            return normalize_root(value)
        except ValueError as error:
            raise ValueError(f"destination {info.data.get('name')!r}: {error}") from error

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
    """Refuse destinations that repeat a name or would share files.

    Raises:
        ValueError: a name repeats; a root does not normalize; two names point at one
            identity; or one root lies inside another's in the same account, so one file
            would count as a copy at both. The message names the destinations concerned.
    """
    names = [d.name for d in destinations]
    repeated = sorted({name for name in names if names.count(name) > 1})
    if repeated:
        raise ValueError(f"destination names must be unique; repeated: {repeated}")
    for destination in destinations:
        try:
            normalize_root(destination.root_path)
        except ValueError as error:
            raise ValueError(f"destination {destination.name!r}: {error}") from error
    for first, second in combinations(destinations, 2):
        outer, inner = first.identity, second.identity
        if outer == inner:
            raise ValueError(
                f"destinations {first.name!r} and {second.name!r} are the same place "
                f"({outer.label()})"
            )
        if outer.contains(inner) or inner.contains(outer):
            raise ValueError(
                f"destinations {first.name!r} ({outer.label()}) and {second.name!r} "
                f"({inner.label()}) overlap: one root lies inside the other in the same "
                "account, so one file would count as a copy at both"
            )


class PlacementRule(DomainModel):
    """Content matching `when` should have copies at `destinations`."""

    name: str = Field(min_length=1)
    when: Mapping[str, str] = Field(default_factory=dict)
    destinations: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _check(self, info: ValidationInfo[object]) -> Self:
        """Reject conditions that cannot match.

        Raises:
            ValueError: a condition names another fact or a value no fact can hold, or,
                with a taxonomy in the validation context, a value it does not list.
        """
        check_conditions(
            f"placement rule {self.name!r}", self.when, taxonomy=taxonomy_in(info.context)
        )
        return self


class RetentionRule(DomainModel):
    """For content matching `when`: may its originals be released, and on what terms."""

    name: str = Field(min_length=1)
    when: Mapping[str, str] = Field(default_factory=dict)
    release_source: bool = False
    required_copies: int = Field(
        default=2,
        ge=1,
        description="Strongly verified copies at destinations, not counting the original, "
        "needed before release is permitted; the original is retained until they exist.",
    )
    minimum_level: VerificationLevel = VerificationLevel.STRONG_IN_TRANSIT

    @model_validator(mode="after")
    def _check(self, info: ValidationInfo[object]) -> Self:
        """Reject conditions that cannot match and a release below the strong rungs.

        Raises:
            ValueError: a condition names another fact or a value no fact can hold, or,
                with a taxonomy in the validation context, a value it does not list; or
                release would accept a copy weaker than the strong rungs.
        """
        check_conditions(
            f"retention rule {self.name!r}", self.when, taxonomy=taxonomy_in(info.context)
        )
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
        """Reject destinations sharing a name or a place, and rules naming undefined ones.

        Raises:
            ValueError: `check_destinations` refuses the destinations, or a rule targets an
                undefined one.
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

    def destination_names(self) -> frozenset[str]:
        """Return the names of the destinations this policy defines.

        These are the destinations the destination checks verify, so only copies at them
        count toward source release.
        """
        return frozenset(d.name for d in self.destinations)

    def destinations_by_id(self) -> dict[DestinationId, str]:
        """Return each destination identity this policy defines, mapped to its name here.

        These are the identities the destination checks verify, so only copies at them
        count toward source release, reported under the name the policy now gives them.
        """
        return {d.id: d.name for d in self.destinations}

    def desired_destinations(self, facts: Facts) -> tuple[str, ...]:
        """Return every destination a matching rule asks for, in a stable order."""
        return tuple(self.selections(facts))

    def selections(self, facts: Facts) -> dict[str, tuple[str, ...]]:
        """Return each destination a matching rule asks for, with the rules that do.

        Returns:
            Destination names in sorted order, each mapped to the names of the matching
            rules that target it, in policy order.
        """
        selected: dict[str, list[str]] = {}
        for rule in self.rules:
            if matches(rule.when, facts):
                for name in rule.destinations:
                    rules = selected.setdefault(name, [])
                    if rule.name not in rules:
                        rules.append(rule.name)
        return {name: tuple(selected[name]) for name in sorted(selected)}

    def retention_for(self, facts: Facts) -> RetentionRule | None:
        """The first retention rule that applies, or None: retain by default."""
        return next((rule for rule in self.retention if matches(rule.when, facts)), None)


# No destinations: nothing is placed until a person configures where things go.
SEED_POLICY = PlacementPolicy()
