import json
from pathlib import Path

import pytest

from app.api_server.config.settings import ApiServerSettings
from app.api_server.storage.image_blob_store import BlobStoreError, ImageBlobLocation
from nianlun.config import PROJECT_ROOT


def test_default_profile_and_relative_root(monkeypatch, tmp_path):
    from app.api_server.storage.image_storage_profiles import (
        build_image_storage_registry,
    )

    monkeypatch.chdir(tmp_path)
    registry = build_image_storage_registry(
        ApiServerSettings(data_dir=Path("relative"))
    )
    assert registry.selected_profile_id == "filesystem-default"
    assert json.loads(registry.locators["filesystem-default"].canonical_json)[
        "root"
    ] == str(PROJECT_ROOT / "relative/answer-images")
    registry.close()
    registry.close()


def test_environment_and_identity_ignore_credentials(monkeypatch):
    from app.api_server.storage.image_storage_profiles import (
        build_image_storage_registry,
    )

    monkeypatch.setenv("NIANLUN_API_ANSWER_IMAGE_STORAGE_BACKEND", "s3")
    monkeypatch.setenv("NIANLUN_API_ANSWER_IMAGE_S3_BUCKET", "test-bucket")
    monkeypatch.setenv("NIANLUN_API_ANSWER_IMAGE_S3_REGION", "us-east-1")
    first = build_image_storage_registry(ApiServerSettings.from_env())
    monkeypatch.setenv("NIANLUN_API_ANSWER_IMAGE_S3_ACCESS_KEY_ID", "private-key")
    monkeypatch.setenv(
        "NIANLUN_API_ANSWER_IMAGE_S3_SECRET_ACCESS_KEY", "private-secret"
    )
    settings = ApiServerSettings.from_env()
    second = build_image_storage_registry(settings)
    assert first.locators == second.locators
    assert "private-" not in repr(settings)
    second.close()
    monkeypatch.setenv("NIANLUN_API_ANSWER_IMAGE_S3_BUCKET", "other-bucket")
    assert (
        build_image_storage_registry(ApiServerSettings.from_env()).locators
        != first.locators
    )


@pytest.mark.parametrize(
    "profiles",
    [
        {"one": {"backend": "unknown"}},
        {"one": {"backend": "filesystem", "root": "ok", "secret": "private-secret"}},
        {
            "one": {
                "backend": "s3",
                "bucket": "test-bucket",
                "region": "us",
                "access_key_id": "private-secret",
            }
        },
    ],
)
def test_invalid_profiles_redact(profiles):
    with pytest.raises(ValueError) as error:
        ApiServerSettings(
            answer_image_storage_profiles=profiles,
            answer_image_storage_profile_id="one",
        )
    assert "private-secret" not in str(error.value)


def test_old_profiles_dispatch_and_invalid_locations(tmp_path):
    from app.api_server.storage.image_storage_profiles import (
        build_image_storage_registry,
    )

    registry = build_image_storage_registry(
        ApiServerSettings(
            answer_image_storage_profile_id="new",
            answer_image_storage_profiles={
                "old": {"backend": "filesystem", "root": str(tmp_path / "old")},
                "new": {"backend": "filesystem", "root": str(tmp_path / "new")},
            },
        )
    )
    location = ImageBlobLocation(
        backend="filesystem", storage_profile_id="old", object_key="missing.png"
    )
    with pytest.raises(BlobStoreError, match="image_missing"):
        registry.stat(location)
    with pytest.raises(BlobStoreError, match="invalid_location"):
        registry.stat(location.model_copy(update={"backend": "s3"}))
    with pytest.raises(BlobStoreError, match="invalid_location"):
        registry.stat(location.model_copy(update={"storage_profile_id": "absent"}))


def test_build_close_never_creates_sdk_client(monkeypatch):
    import boto3
    from app.api_server.storage.image_storage_profiles import (
        build_image_storage_registry,
    )

    def forbidden(*args, **kwargs):
        pytest.fail("SDK client created during configuration")

    monkeypatch.setattr(boto3, "client", forbidden)
    registry = build_image_storage_registry(
        ApiServerSettings(
            answer_image_storage_backend="s3",
            answer_image_s3_bucket="test-bucket",
            answer_image_s3_region="us-east-1",
        )
    )
    registry.close()
    registry.close()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"answer_image_storage_profile_id": "../bad"},
        {
            "answer_image_storage_profile_id": "missing",
            "answer_image_storage_profiles": {
                "one": {"backend": "filesystem", "root": "ok"}
            },
        },
        {
            "answer_image_storage_backend": "s3",
            "answer_image_storage_profile_id": "one",
            "answer_image_storage_profiles": {
                "one": {"backend": "filesystem", "root": "ok"}
            },
        },
        {
            "answer_image_storage_profiles": {
                "one": {"backend": "filesystem", "root": ""}
            }
        },
    ],
)
def test_rejects_invalid_selection_and_paths(kwargs):
    with pytest.raises(ValueError, match="Invalid answer image storage"):
        ApiServerSettings(**kwargs)


def test_empty_env_credentials_use_role_credentials(monkeypatch):
    monkeypatch.setenv("NIANLUN_API_ANSWER_IMAGE_STORAGE_BACKEND", "s3")
    monkeypatch.setenv("NIANLUN_API_ANSWER_IMAGE_S3_BUCKET", "test-bucket")
    monkeypatch.setenv("NIANLUN_API_ANSWER_IMAGE_S3_REGION", "us-east-1")
    for name in ("ACCESS_KEY_ID", "SECRET_ACCESS_KEY", "SESSION_TOKEN"):
        monkeypatch.setenv("NIANLUN_API_ANSWER_IMAGE_S3_" + name, "")
    settings = ApiServerSettings.from_env()
    assert settings.answer_image_s3_access_key_id is None


def test_retained_s3_dispatch_and_close_with_fake_adapter(monkeypatch, tmp_path):
    from contextlib import nullcontext
    from io import BytesIO
    from app.api_server.storage import image_storage_profiles as module
    from app.api_server.storage.image_blob_store import ImageBlobStat

    calls = []

    class FakeS3:
        def __init__(self, config, *, max_bytes, max_pixels):
            assert max_bytes == 8 * 1024 * 1024
            assert max_pixels == 40_000_000
            calls.append(("init", config.profile_id))

        def put(self, location, data, metadata):
            calls.append(("put", location.storage_profile_id))
            return location

        def open(self, location, metadata):
            calls.append(("open", location.storage_profile_id))
            return nullcontext(BytesIO(b"image"))

        def stat(self, location):
            calls.append(("stat", location.storage_profile_id))
            return ImageBlobStat(size_bytes=5)

        def delete(self, location, *, all_versions=False):
            calls.append(("delete", all_versions))

        def close(self):
            calls.append(("close", None))

    monkeypatch.setattr(module, "S3ImageBlobStore", FakeS3)
    registry = module.build_image_storage_registry(
        ApiServerSettings(
            answer_image_storage_profile_id="new",
            answer_image_storage_profiles={
                "new": {"backend": "filesystem", "root": str(tmp_path)},
                "old": {
                    "backend": "s3",
                    "bucket": "test-bucket",
                    "region": "us-east-1",
                },
            },
        )
    )
    assert calls == []
    location = ImageBlobLocation(
        backend="s3", storage_profile_id="old", object_key="answer-images/a.png"
    )
    assert registry.put(location, b"image", None) == location
    with registry.open(location, None) as stream:
        assert stream.read() == b"image"
    assert registry.stat(location).size_bytes == 5
    registry.delete(location, all_versions=True)
    registry.close()
    registry.close()
    assert calls == [
        ("init", "old"),
        ("put", "old"),
        ("open", "old"),
        ("stat", "old"),
        ("delete", True),
        ("close", None),
    ]
    with pytest.raises(BlobStoreError, match="storage_unavailable"):
        registry.stat(location)


@pytest.mark.parametrize("mapped", [False, True])
def test_relative_s3_ca_bundle_is_anchored_to_project_root(
    tmp_path, monkeypatch, mapped
):
    from app.api_server.storage.image_storage_profiles import (
        build_image_storage_registry,
    )

    monkeypatch.chdir(tmp_path)
    kwargs = {
        "answer_image_storage_backend": "s3",
        "answer_image_s3_bucket": "test-bucket",
        "answer_image_s3_region": "us-east-1",
        "answer_image_s3_ca_bundle": "certs/storage.pem",
    }
    if mapped:
        kwargs.update(
            answer_image_storage_profile_id="cloud",
            answer_image_storage_profiles={
                "cloud": {
                    "backend": "s3",
                    "bucket": "test-bucket",
                    "region": "us-east-1",
                    "ca_bundle": "certs/storage.pem",
                }
            },
        )
    registry = build_image_storage_registry(ApiServerSettings(**kwargs))
    config = registry.profiles[registry.selected_profile_id]
    assert config.ca_bundle == str(PROJECT_ROOT / "certs/storage.pem")


def test_close_attempts_all_clients_when_one_close_fails(monkeypatch):
    from app.api_server.storage import image_storage_profiles as module
    from app.api_server.storage.image_blob_store import ImageBlobStat

    calls = []

    class FakeS3:
        def __init__(self, config, *, max_bytes, max_pixels):
            assert max_bytes == 8 * 1024 * 1024
            assert max_pixels == 40_000_000
            self.profile = config.profile_id

        def stat(self, location):
            return ImageBlobStat(size_bytes=0)

        def close(self):
            calls.append(self.profile)
            if self.profile == "first":
                raise OSError("Close failed")

    monkeypatch.setattr(module, "S3ImageBlobStore", FakeS3)
    registry = module.build_image_storage_registry(
        ApiServerSettings(
            answer_image_storage_backend="s3",
            answer_image_storage_profile_id="first",
            answer_image_storage_profiles={
                name: {
                    "backend": "s3",
                    "bucket": "test-bucket",
                    "region": "us-east-1",
                }
                for name in ("first", "second")
            },
        )
    )
    for name in ("first", "second"):
        registry.stat(
            ImageBlobLocation(backend="s3", storage_profile_id=name, object_key="a.png")
        )
    with pytest.raises(OSError, match="Close failed"):
        registry.close()
    assert set(calls) == {"first", "second"}
    registry.close()
    assert len(calls) == 2
