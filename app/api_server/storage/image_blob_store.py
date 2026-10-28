"""Typed IO boundary for private, immutable answer-image objects."""

from __future__ import annotations

from contextlib import AbstractContextManager
from typing import BinaryIO, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.api_server.storage.image_validation import (
    ImageMetadata,
    ImageValidationError,
    MAX_IMAGE_BYTES,
    MAX_IMAGE_PIXELS,
    validate_image,
)

BlobStoreReason = Literal[
    "invalid_location",
    "image_missing",
    "integrity_error",
    "storage_permission_denied",
    "storage_unavailable",
]


class BlobStoreError(RuntimeError):
    def __init__(self, reason: BlobStoreReason) -> None:
        self.reason = reason
        super().__init__(reason)


class ImageBlobLocation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    backend: Literal["filesystem", "s3"]
    storage_profile_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
    object_key: str = Field(min_length=1, max_length=1024)
    version_id: str | None = Field(default=None, min_length=1, max_length=1024)

    @model_validator(mode="after")
    def canonical_location(self) -> ImageBlobLocation:
        import re

        if not re.fullmatch(r"[A-Za-z0-9_./-]+", self.object_key) or any(
            part in {"", ".", ".."} for part in self.object_key.split("/")
        ):
            raise ValueError("Invalid image object key")
        if self.backend == "filesystem" and self.version_id is not None:
            raise ValueError("Filesystem objects have no version ID")
        return self


class ImageBlobStat(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    size_bytes: int = Field(ge=0)
    version_id: str | None = None


class ImageBlobStore(Protocol):
    def put(
        self, location: ImageBlobLocation, data: bytes, metadata: ImageMetadata
    ) -> ImageBlobLocation: ...

    def open(
        self, location: ImageBlobLocation, metadata: ImageMetadata
    ) -> AbstractContextManager[BinaryIO]: ...

    def stat(self, location: ImageBlobLocation) -> ImageBlobStat: ...

    def delete(
        self, location: ImageBlobLocation, *, all_versions: bool = False
    ) -> None: ...


def verify_image_bytes(
    data: bytes,
    metadata: ImageMetadata,
    *,
    max_bytes: int = MAX_IMAGE_BYTES,
    max_pixels: int = MAX_IMAGE_PIXELS,
) -> None:
    """Recheck original bytes; metadata alone is never proof of integrity."""
    if len(data) != metadata.size_bytes:
        raise BlobStoreError("integrity_error")
    try:
        actual = validate_image(data, max_bytes=max_bytes, max_pixels=max_pixels)
    except ImageValidationError:
        raise BlobStoreError("integrity_error") from None
    if actual != metadata:
        raise BlobStoreError("integrity_error")
