"""Hash algorithms each provider reports, and the rung they allow."""

import pytest

from curate_d.domain import HashAlgorithm, Provider, VerificationLevel
from curate_d.placement.verification import expected_level
from curate_d.providers.hashes import advertised_hashes
from curate_d.providers.pcloud.api import EU_API_HOST, US_API_HOST

_EU = advertised_hashes(Provider.PCLOUD, EU_API_HOST)
_US = advertised_hashes(Provider.PCLOUD, US_API_HOST)
_UNKNOWN = advertised_hashes(Provider.PCLOUD, None)


def test_pcloud_regions_report_sha1_and_one_regional_algorithm() -> None:
    """SHA-256 in the EU, MD5 in the US, SHA-1 in both."""
    assert advertised_hashes(Provider.PCLOUD, EU_API_HOST) == {
        HashAlgorithm.SHA1,
        HashAlgorithm.SHA256,
    }
    assert advertised_hashes(Provider.PCLOUD, US_API_HOST) == {
        HashAlgorithm.SHA1,
        HashAlgorithm.MD5,
    }
    assert advertised_hashes(Provider.PCLOUD, None) == {HashAlgorithm.SHA1}


@pytest.mark.parametrize(
    ("source", "destination", "level"),
    [
        (_EU, _US, VerificationLevel.STRONG_COMMON_HASH),
        (_UNKNOWN, _EU, VerificationLevel.STRONG_COMMON_HASH),
        (frozenset({HashAlgorithm.SHA256}), _US, VerificationLevel.STRONG_IN_TRANSIT),
        (frozenset(), _EU, VerificationLevel.WEAK),
    ],
)
def test_the_expected_level_follows_both_sides_algorithms(
    source: frozenset[HashAlgorithm],
    destination: frozenset[HashAlgorithm],
    level: VerificationLevel,
) -> None:
    """A common algorithm, then a read-back in the source's, then size only."""
    assert expected_level(source, destination) is level
