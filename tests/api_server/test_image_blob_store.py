import hashlib
import os
from io import BytesIO
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import pytest
from PIL import Image
from pydantic import ValidationError

from app.api_server.storage import ImageBlobLocation as PackageImageBlobLocation
from app.api_server.storage.filesystem_image_blob_store import FilesystemImageBlobStore
from app.api_server.storage.image_blob_store import BlobStoreError, ImageBlobLocation
from app.api_server.storage.image_validation import validate_image


def sample_image(color="red"):
    output = BytesIO()
    with Image.new("RGB", (8, 6), color) as image:
        image.save(output, format="PNG")
    data = output.getvalue()
    return data, validate_image(data)


def location(key="answer-images/kb-1/blob-1/image.png", **options):
    return ImageBlobLocation(
        backend="filesystem", storage_profile_id="local", object_key=key, **options
    )


def test_storage_package_exports_stable_types():
    assert PackageImageBlobLocation is ImageBlobLocation


def test_local_store_roundtrip_and_closed_handle(tmp_path):
    store = FilesystemImageBlobStore(tmp_path, profile_id="local")
    data, metadata = sample_image()
    target = store.put(location(), data, metadata)
    assert target == location()
    assert store.stat(target).size_bytes == len(data)
    with store.open(target, metadata) as source:
        assert source.read() == data
    assert source.closed
    store.delete(target)
    store.delete(target)
    with pytest.raises(BlobStoreError) as error:
        with store.open(target, metadata):
            pass
    assert error.value.reason == "image_missing"


def test_read_uses_committed_metadata_after_admission_limits_are_lowered(tmp_path):
    data, metadata = sample_image()
    FilesystemImageBlobStore(tmp_path, profile_id="local").put(
        location(), data, metadata
    )
    lowered = FilesystemImageBlobStore(
        tmp_path, profile_id="local", max_bytes=1, max_pixels=1
    )
    with lowered.open(location(), metadata) as source:
        assert source.read() == data
    with pytest.raises(BlobStoreError):
        lowered.put(location("other.png"), data, metadata)


def test_idempotent_put_cannot_overwrite_other_content(tmp_path):
    store = FilesystemImageBlobStore(tmp_path, profile_id="local")
    data, metadata = sample_image()
    store.put(location(), data, metadata)
    store.put(location(), data, metadata)
    other, other_meta = sample_image("blue")
    with pytest.raises(BlobStoreError) as error:
        store.put(location(), other, other_meta)
    assert error.value.reason == "integrity_error"
    with store.open(location(), metadata) as source:
        assert source.read() == data
    assert not list(tmp_path.rglob("*.tmp"))


def test_put_rejects_unverified_or_mismatched_bytes(tmp_path):
    store = FilesystemImageBlobStore(tmp_path, profile_id="local")
    data, metadata = sample_image()
    with pytest.raises(BlobStoreError):
        store.put(location(), data + b"unexpected", metadata)
    assert not (tmp_path / location().object_key).exists()
    forged = metadata.model_copy(update={"sha256": hashlib.sha256(b"bad").hexdigest()})
    with pytest.raises(BlobStoreError):
        store.put(location(), data, forged)


def test_full_integrity_is_checked_before_yielding_handle(tmp_path):
    store = FilesystemImageBlobStore(tmp_path, profile_id="local")
    data, metadata = sample_image()
    store.put(location(), data, metadata)
    target = tmp_path / location().object_key
    target.write_bytes(b"x" * len(data))
    with pytest.raises(BlobStoreError) as error:
        with store.open(location(), metadata):
            pytest.fail("Corrupt data was disclosed")
    assert error.value.reason == "integrity_error"
    assert str(tmp_path) not in str(error.value)


@pytest.mark.parametrize(
    "key",
    ["/etc/passwd", "../x", "a/../x", "a//x", "a\\x", ".", "a/", "https://host/x"],
)
def test_location_rejects_noncanonical_paths(key):
    with pytest.raises(ValidationError):
        location(key)


def test_profile_and_backend_are_checked_before_io(tmp_path):
    store = FilesystemImageBlobStore(tmp_path, profile_id="other")
    data, metadata = sample_image()
    with pytest.raises(BlobStoreError) as error:
        store.put(location(), data, metadata)
    assert error.value.reason == "invalid_location"
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("leaf", [False, True])
def test_symlink_cannot_escape_on_any_operation(tmp_path, leaf):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    data, metadata = sample_image()
    (outside / "image.png").write_bytes(data)
    if leaf:
        (root / "assets").mkdir()
        os.symlink(outside / "image.png", root / "assets" / "image.png")
    else:
        os.symlink(outside, root / "assets")
    store = FilesystemImageBlobStore(root, profile_id="local")
    target = location("assets/image.png")
    for operation in (
        lambda: store.put(target, data, metadata),
        lambda: store.stat(target),
        lambda: store.delete(target),
    ):
        with pytest.raises(BlobStoreError):
            operation()
    with pytest.raises(BlobStoreError):
        with store.open(target, metadata):
            pytest.fail("Symlink was followed")
    assert (outside / "image.png").read_bytes() == data


def test_read_limit_applies_even_if_file_changes(tmp_path):
    store = FilesystemImageBlobStore(tmp_path, profile_id="local")
    data, metadata = sample_image()
    store.put(location(), data, metadata)
    Path(tmp_path / location().object_key).write_bytes(data + b"oversized")
    with pytest.raises(BlobStoreError) as error:
        with store.open(location(), metadata):
            pass
    assert error.value.reason == "integrity_error"


def test_concurrent_publication_is_complete_and_idempotent(tmp_path):
    data, metadata = sample_image()
    stores = [FilesystemImageBlobStore(tmp_path, profile_id="local") for _ in range(4)]
    with ThreadPoolExecutor(max_workers=4) as workers:
        results = list(
            workers.map(lambda adapter: adapter.put(location(), data, metadata), stores)
        )
    assert results == [location()] * 4
    with stores[0].open(location(), metadata) as source:
        assert source.read() == data
    assert not list(tmp_path.rglob("*.tmp"))


def test_publication_failure_removes_temporary_file(tmp_path, monkeypatch):
    data, metadata = sample_image()
    store = FilesystemImageBlobStore(tmp_path, profile_id="local")

    def fail_publication(*args, **kwargs):
        raise OSError("private filesystem detail")

    monkeypatch.setattr(os, "link", fail_publication)
    with pytest.raises(BlobStoreError) as error:
        store.put(location(), data, metadata)
    assert error.value.reason == "storage_unavailable"
    assert "private filesystem" not in str(error.value)
    assert not list(tmp_path.rglob("*.tmp"))
    assert not (tmp_path / location().object_key).exists()
