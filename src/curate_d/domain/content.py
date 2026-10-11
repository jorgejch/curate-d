"""The content-addressed identity layer.

A content object is a distinct sequence of bytes. Two source objects in different clouds
that fingerprint alike are one content object, which is why the same file is never
classified twice.
"""

import re
from typing import Self

from pydantic import Field, model_validator

from curate_d.domain.base import DomainModel
from curate_d.domain.fingerprint import PerceptualFingerprint, StrongFingerprint
from curate_d.domain.ids import ContentObjectId, derive_content_object_id

_MEDIA_TYPE = re.compile(r"\A[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*\Z")


class ContentObject(DomainModel):
    """One distinct sequence of bytes, however many places it is stored."""

    id: ContentObjectId
    fingerprint: StrongFingerprint
    size_bytes: int = Field(ge=0)
    media_type: str = Field(description="Sniffed media type, lowercase.")
    perceptual_fingerprint: PerceptualFingerprint | None = Field(
        default=None,
        description="Images only; absent for content with no visual form.",
    )

    @model_validator(mode="after")
    def _check_media_type(self) -> Self:
        """Reject a media_type that isn't a lowercase type/subtype pair.

        Raises:
            ValueError: `media_type` doesn't match the `type/subtype` shape.
        """
        if not _MEDIA_TYPE.fullmatch(self.media_type):
            raise ValueError(
                f"media_type must be a lowercase type/subtype, got {self.media_type!r}"
            )
        return self

    @classmethod
    def build(
        cls,
        *,
        fingerprint: StrongFingerprint,
        size_bytes: int,
        media_type: str,
        perceptual_fingerprint: PerceptualFingerprint | None = None,
    ) -> "ContentObject":
        """Construct with the identifier derived from the fingerprint."""
        return cls(
            id=derive_content_object_id(str(fingerprint)),
            fingerprint=fingerprint,
            size_bytes=size_bytes,
            media_type=media_type,
            perceptual_fingerprint=perceptual_fingerprint,
        )
