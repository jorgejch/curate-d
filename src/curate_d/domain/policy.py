"""Placement policy: where content should be, and when an original may be released.

Destinations are named targets. Placement rules map facts to the destinations content
should have copies at; every matching rule contributes, so policies compose. Retention is
separate: an original is kept unless a retention rule explicitly permits release, and even
then only once enough strongly verified copies exist — counting the original itself when
the policy says it is one of the copies.

Every value a policy names is checked when it is parsed: a condition's `media_kind` must
be a `MediaKind`, and a destination's `profile` a `CapabilityProfileName`. Category and
subcategory conditions are checked against the taxonomy only when a validation context
carries one, which activation does (`domain.conditions`).
"""

from collections.abc import Mapping
from enum import StrEnum
from typing import Self

from pydantic import Field, ValidationInfo, model_validator

from curate_d.domain.base import DomainModel
from curate_d.domain.conditions import check_conditions, matches, taxonomy_in
from curate_d.domain.facts import Facts
from curate_d.domain.placement import VerificationLevel
from curate_d.domain.source import Provider


class CapabilityProfileName(StrEnum):
    """The capability profiles a destination can name.

    Rendering holds a profile for each (`rendering.templates.PROFILES`); the domain holds
    only the names, so a policy naming another is refused when it is parsed.
    """

    PCLOUD = "pcloud"
    PORTABLE = "portable"


class DestinationConfig(DomainModel):
    """A named target content can be placed at."""

    name: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9-]*$")
    provider: Provider
    account: str = Field(min_length=1)
    root_path: str = Field(min_length=1, pattern=r"^/")
    profile: CapabilityProfileName = Field(
        default=CapabilityProfileName.PCLOUD, description="Capability profile name."
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
    required_copies: int = Field(default=2, ge=1)
    source_counts_as_copy: bool = True
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
        """Reject duplicate destinations and rules naming undefined ones.

        Raises:
            ValueError: a destination name repeats, or a rule targets an undefined one.
        """
        names = [d.name for d in self.destinations]
        if len(set(names)) != len(names):
            raise ValueError("destination names must be unique")
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
            name for rule in self.rules if matches(rule.when, facts) for name in rule.destinations
        }
        return tuple(sorted(wanted))

    def retention_for(self, facts: Facts) -> RetentionRule | None:
        """The first retention rule that applies, or None: retain by default."""
        return next((rule for rule in self.retention if matches(rule.when, facts)), None)


# No destinations: nothing is placed until a person configures where things go.
SEED_POLICY = PlacementPolicy()
