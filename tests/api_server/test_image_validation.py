from hashlib import sha256
from io import BytesIO

import pytest
from PIL import Image

from app.api_server.storage.image_validation import (
    ImageValidationError,
    validate_image,
)


def image_bytes(format="PNG", *, size=(8, 6), **options):
    output = BytesIO()
    with Image.new("RGB", size, color="red") as image:
        image.save(output, format=format, **options)
    return output.getvalue()


@pytest.mark.parametrize(
    ("format", "mime"),
    [("PNG", "image/png"), ("JPEG", "image/jpeg"), ("WEBP", "image/webp")],
)
def test_validates_original_bytes_and_actual_format(format, mime):
    data = image_bytes(format)
    result = validate_image(data)
    assert result.media_type == mime
    assert result.sha256 == sha256(data).hexdigest()
    assert result.size_bytes == len(data)
    assert (result.width, result.height, result.orientation) == (8, 6, 1)


def test_keeps_original_dimensions_and_exif_orientation():
    exif = Image.Exif()
    exif[274] = 6
    data = image_bytes("JPEG", exif=exif)
    result = validate_image(data)
    assert (result.width, result.height, result.orientation) == (8, 6, 6)
    assert result.sha256 == sha256(data).hexdigest()


def test_cmyk_jpeg_remains_original_bytes():
    output = BytesIO()
    with Image.new("CMYK", (8, 6)) as image:
        image.save(output, format="JPEG")
    data = output.getvalue()
    result = validate_image(data)
    assert result.media_type == "image/jpeg"
    assert result.sha256 == sha256(data).hexdigest()


@pytest.mark.parametrize("data", [b"", b"not an image", b"<svg></svg>"])
def test_invalid_input_uses_fixed_reason_without_echoing_data(data):
    with pytest.raises(ImageValidationError) as error:
        validate_image(data)
    assert error.value.reason == "invalid_image"
    assert "svg" not in str(error.value)


def test_rejects_unsupported_gif():
    with pytest.raises(ImageValidationError) as error:
        validate_image(image_bytes("GIF"))
    assert error.value.reason == "unsupported_format"


def test_rejects_animation():
    with Image.new("RGB", (8, 6), "blue") as second:
        data = image_bytes(save_all=True, append_images=[second], duration=20)
    with pytest.raises(ImageValidationError) as error:
        validate_image(data)
    assert error.value.reason == "animated_image"


def test_rejects_truncated_image():
    data = image_bytes()
    with pytest.raises(ImageValidationError) as error:
        validate_image(data[: len(data) // 2])
    assert error.value.reason == "invalid_image"


def test_exact_byte_and_pixel_limits():
    data = image_bytes()
    assert validate_image(data, max_bytes=len(data), max_pixels=48).width == 8
    with pytest.raises(ImageValidationError) as error:
        validate_image(data, max_bytes=len(data) - 1)
    assert error.value.reason == "image_too_large"
    with pytest.raises(ImageValidationError) as error:
        validate_image(data, max_pixels=47)
    assert error.value.reason == "pixel_limit_exceeded"


def test_decompression_bomb_warning_is_rejected_without_changing_global(monkeypatch):
    data = image_bytes()
    import warnings

    filters_before = warnings.filters[:]
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 30)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", Image.DecompressionBombWarning)
        with pytest.raises(ImageValidationError) as error:
            validate_image(data)
    assert error.value.reason == "pixel_limit_exceeded"
    assert Image.MAX_IMAGE_PIXELS == 30
    assert warnings.filters == filters_before


@pytest.mark.parametrize("limit", [0, -1])
def test_invalid_limits_are_configuration_errors(limit):
    with pytest.raises(ValueError):
        validate_image(image_bytes(), max_bytes=limit)
