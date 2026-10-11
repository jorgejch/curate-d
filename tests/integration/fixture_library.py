"""A small library spanning what the organizer meets, with where each file should end up.

Documents, photos with and without location, an audio recording, the same photo at two
paths, two documents the remote tier files under a value the seed taxonomy lacks, and a
file nothing can classify. Every file's bytes carry the run's tag: classification is
keyed by content, and bytes an earlier run classified would find that run's work.

`Fixture.locator` is where the file lands under a destination's root once organized, or
None for what stays unresolved under the seed taxonomy. `after_promotion` is where it
lands once the taxonomy contains the value it was refused.
"""

import os
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Final

from curate_d.classification.deterministic import TierResult
from curate_d.classification.remote import RemoteOutcome, RemoteRequest, RemoteResult
from curate_d.domain import (
    ClassificationTier,
    Facts,
    RejectedValue,
    Taxonomy,
    TaxonomyField,
)
from curate_d.placement.rclone import RcloneClient
from tests.stubs import media
from tests.stubs.pcloud import StubPCloud

REMOTE_MODEL: Final = "stub/library-model"
RECEIPTS: Final = RejectedValue(
    field=TaxonomyField.SUBCATEGORY, value="Receipts", category="Documents"
)


@dataclass(frozen=True)
class Fixture:
    """One file of the library and its expected outcome."""

    path: str
    content: bytes
    locator: str | None
    after_promotion: str | None = None

    @property
    def name(self) -> str:
        """The file's name."""
        return self.path.rsplit("/", 1)[-1]


@dataclass(frozen=True)
class Library:
    """The fixtures, by path."""

    fixtures: tuple[Fixture, ...]

    def __getitem__(self, path: str) -> Fixture:
        """The fixture at `path`."""
        return next(f for f in self.fixtures if f.path == path)

    @property
    def placed(self) -> tuple[Fixture, ...]:
        """Fixtures organized under the seed taxonomy."""
        return tuple(f for f in self.fixtures if f.locator is not None)

    @property
    def unresolved(self) -> tuple[Fixture, ...]:
        """Fixtures nothing places under the seed taxonomy."""
        return tuple(f for f in self.fixtures if f.locator is None)


GPS_PHOTO: Final = "/Camera/IMG_0001.JPG"
COPY_OF_GPS_PHOTO: Final = "/Backup/IMG_0001.JPG"
PLAIN_PHOTO: Final = "/Camera/IMG_0002.JPG"
INVOICE: Final = "/Documents/electric.pdf"
RECORDING: Final = "/Recordings/Voice Memo 001.mp3"
PICNIC: Final = "/Documents/picnic-minutes.pdf"
CLUB: Final = "/Documents/club-minutes.pdf"
BLOB: Final = "/Misc/blob.bin"


def library(tag: str) -> Library:
    """The library, its bytes made unique by `tag`."""
    camera = ("Canon", f"EOS {tag}")
    gps_photo = media.photo(
        camera=camera,
        taken=datetime(2024, 5, 1, 9, 30),  # noqa: DTZ001 - EXIF wall-clock time
        offset="+02:00",
        gps=(38.7223, -9.1393),
    )
    return Library(
        fixtures=(
            Fixture(GPS_PHOTO, gps_photo, "Photos/2024/2024-05/IMG_0001.JPG"),
            Fixture(COPY_OF_GPS_PHOTO, gps_photo, "Photos/2024/2024-05/IMG_0001.JPG"),
            Fixture(
                PLAIN_PHOTO,
                media.photo(
                    camera=camera,
                    taken=datetime(2024, 6, 12, 18, 5),  # noqa: DTZ001 - EXIF wall-clock time
                    offset="+01:00",
                    seed=2,
                ),
                "Photos/2024/2024-06/IMG_0002.JPG",
            ),
            Fixture(
                INVOICE,
                media.pdf([f"INVOICE {tag}", "Total amount due", "Payment by bank", "IBAN", "VAT"]),
                "Documents/Finance/electric.pdf",
            ),
            Fixture(
                RECORDING,
                media.mp3() + f"TAG{tag}".encode(),
                "Audio/Recordings/Voice Memo 001.mp3",
            ),
            Fixture(
                PICNIC,
                media.pdf([f"Minutes of the association picnic {tag}", "Weather was fine"]),
                None,
                "Documents/Receipts/picnic-minutes.pdf",
            ),
            Fixture(
                CLUB,
                media.pdf([f"Minutes of the chess club {tag}", "Nothing was decided"]),
                None,
                "Documents/Receipts/club-minutes.pdf",
            ),
            Fixture(BLOB, os.urandom(512) + tag.encode(), None),
        )
    )


async def load(library: Library, pcloud: StubPCloud, daemon: tuple[RcloneClient, str]) -> None:
    """Put every fixture on the stub account and in the daemon's source directory."""
    rclone, workspace = daemon
    for fixture in library.fixtures:
        pcloud.file(fixture.path, fixture.content)
        directory = fixture.path.rsplit("/", 1)[0]
        await rclone.upload("/", f"{workspace}/source{directory}", fixture.name, fixture.content)


class LibraryRemote:
    """The remote tier as the library needs it: minutes are filed under Receipts, which
    the seed taxonomy lacks; anything else gets an answer that cannot be used."""

    def __init__(self) -> None:
        """No calls yet."""
        self.calls: list[str] = []

    async def classify(
        self, model: str, request: RemoteRequest, taxonomy: Taxonomy, base: Facts
    ) -> RemoteResult:
        """Answer by media type."""
        self.calls.append(request.paths[0] if request.paths else "")
        if request.media_type != "application/pdf":
            return RemoteResult(
                outcome=RemoteOutcome.MALFORMED,
                model_id=REMOTE_MODEL,
                detail="no facts in the answer",
                cost_usd=Decimal("0.001"),
            )
        return RemoteResult(
            outcome=RemoteOutcome.ANSWERED,
            model_id=REMOTE_MODEL,
            cost_usd=Decimal("0.01"),
            result=TierResult(
                tier=ClassificationTier.REMOTE_MODEL,
                facts=Facts.model_validate(
                    base.model_dump() | {"category": "Documents", "subcategory": "Receipts"}
                ),
                confidence=0.93,
                model_id=REMOTE_MODEL,
            ),
        )
