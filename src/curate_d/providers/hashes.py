"""The digests each provider reports for the files it stores.

What a placement can be verified with is decided by these before any byte moves: two
providers that report a common algorithm give the common-hash rung, and otherwise the
copy has to be read back.
"""

from curate_d.domain import HashAlgorithm, Provider
from curate_d.providers.pcloud.api import EU_API_HOST, US_API_HOST


def advertised_hashes(provider: Provider, api_host: str | None) -> frozenset[HashAlgorithm]:
    """The algorithms `provider` reports for an account served from `api_host`.

    pCloud reports SHA-1 everywhere, plus SHA-256 in its EU region and MD5 in its US
    region. An account whose region is unknown gets only what every region reports.
    """
    match provider:
        case Provider.PCLOUD:
            if api_host == EU_API_HOST:
                return frozenset({HashAlgorithm.SHA1, HashAlgorithm.SHA256})
            if api_host == US_API_HOST:
                return frozenset({HashAlgorithm.SHA1, HashAlgorithm.MD5})
            return frozenset({HashAlgorithm.SHA1})
