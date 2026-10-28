"""Validate bounded original image bytes without transcoding."""

from __future__ import annotations

import hashlib
import struct
from io import BytesIO
from typing import Literal

from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, Field

ImageMediaType = Literal["image/png", "image/jpeg", "image/webp"]
ImageValidationReason = Literal[
    "invalid_image",
    "unsupported_format",
    "animated_image",
    "image_too_large",
    "pixel_limit_exceeded",
]

MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
_MEDIA_TYPES: dict[str, ImageMediaType] = {
    "PNG": "image/png",
    "JPEG": "image/jpeg",
    "WEBP": "image/webp",
}


class ImageValidationError(ValueError):
    """Publicly safe error; decoder exceptions and image contents stay private."""

    def __init__(self, reason: ImageValidationReason) -> None:
        self.reason = reason
        super().__init__(reason)


class ImageMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    media_type: ImageMediaType
    size_bytes: int = Field(gt=0)
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    orientation: int = Field(default=1, ge=1, le=8)


def validate_image(
    data: bytes,
    *,
    max_bytes: int = MAX_IMAGE_BYTES,
    max_pixels: int = MAX_IMAGE_PIXELS,
) -> ImageMetadata:
    if max_bytes < 1 or max_pixels < 1:
        raise ValueError("Image limits must be positive")
    if len(data) > max_bytes:
        raise ImageValidationError("image_too_large")
    try:
        with Image.open(BytesIO(data)) as image:
            media_type = _MEDIA_TYPES.get(image.format or "")
            if media_type is None:
                raise ImageValidationError("unsupported_format")
            width, height = image.size
            configured_pixel_limit = Image.MAX_IMAGE_PIXELS
            if width * height > max_pixels or (
                configured_pixel_limit is not None
                and width * height > configured_pixel_limit
            ):
                raise ImageValidationError("pixel_limit_exceeded")
            if getattr(image, "n_frames", 1) != 1 or getattr(
                image, "is_animated", False
            ):
                raise ImageValidationError("animated_image")
            image.verify()
        # verify() invalidates the decoder; load from the same immutable bytes.
        with Image.open(BytesIO(data)) as image:
            image.load()
            orientation = image.getexif().get(274, 1)
            if type(orientation) is not int or not 1 <= orientation <= 8:
                orientation = 1
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise ImageValidationError("pixel_limit_exceeded") from None
    except ImageValidationError:
        raise
    except (
        UnidentifiedImageError,
        OSError,
        SyntaxError,
        ValueError,
        EOFError,
        struct.error,
    ):
        raise ImageValidationError("invalid_image") from None
    return ImageMetadata(
        sha256=hashlib.sha256(data).hexdigest(),
        media_type=media_type,
        size_bytes=len(data),
        width=width,
        height=height,
        orientation=orientation,
    )
