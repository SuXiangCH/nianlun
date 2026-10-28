from io import BytesIO

import boto3
import pytest
from botocore.response import StreamingBody
from botocore.stub import Stubber
from PIL import Image
from pydantic import ValidationError

from app.api_server.storage.image_blob_store import BlobStoreError, ImageBlobLocation
from app.api_server.storage.image_validation import validate_image
from app.api_server.storage.s3_image_blob_store import (
    S3ImageBlobStore,
    S3ImageStoreConfig,
)


def fixture_image():
    output = BytesIO()
    with Image.new("RGB", (8, 6), "red") as image:
        image.save(output, format="PNG")
    data = output.getvalue()
    return data, validate_image(data)


def location(version=None, key="answer-images/kb/blob/image.png"):
    return ImageBlobLocation(
        backend="s3", storage_profile_id="archive", object_key=key, version_id=version
    )


@pytest.fixture
def sdk():
    client = boto3.client(
        "s3",
        region_name="us-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
        endpoint_url="https://objects.example.test",
    )
    with Stubber(client) as stub:
        yield client, stub
        stub.assert_no_pending_responses()
    client.close()


def store(client=None):
    return S3ImageBlobStore(
        S3ImageStoreConfig(
            profile_id="archive", bucket="private-images", region="us-east-1"
        ),
        client=client,
    )


def test_construction_does_not_create_sdk_client(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("SDK initialized before any S3 operation")

    monkeypatch.setattr(boto3, "client", forbidden)
    store().close()


def test_read_uses_frozen_metadata_after_admission_limits_are_lowered(sdk):
    client, stub = sdk
    data, metadata = fixture_image()
    stub.add_response(
        "get_object",
        {
            "Body": StreamingBody(BytesIO(data), len(data)),
            "ContentLength": len(data),
        },
        {"Bucket": "private-images", "Key": location().object_key},
    )
    lowered = S3ImageBlobStore(
        S3ImageStoreConfig(
            profile_id="archive", bucket="private-images", region="us-east-1"
        ),
        client=client,
        max_bytes=1,
        max_pixels=1,
    )
    with lowered.open(location(), metadata) as source:
        assert source.read() == data


def test_upload_verifies_get_and_returns_actual_version(sdk):
    client, stub = sdk
    data, metadata = fixture_image()
    raw_stream = BytesIO(data)
    body = StreamingBody(raw_stream, len(data))
    stub.add_response(
        "put_object",
        {"VersionId": "v1"},
        {
            "Bucket": "private-images",
            "Key": location().object_key,
            "Body": data,
            "ContentType": "image/png",
            "Metadata": {"sha256": metadata.sha256},
        },
    )
    stub.add_response(
        "get_object",
        {"Body": body, "ContentLength": len(data), "VersionId": "v1"},
        {
            "Bucket": "private-images",
            "Key": location().object_key,
            "VersionId": "v1",
        },
    )
    assert store(client).put(location(), data, metadata) == location("v1")
    assert raw_stream.closed


def test_put_cannot_trust_success_or_head_etag_without_get_validation(sdk):
    client, stub = sdk
    data, metadata = fixture_image()
    raw_stream = BytesIO(b"x" * len(data))
    body = StreamingBody(raw_stream, len(data))
    stub.add_response(
        "put_object",
        {},
        {
            "Bucket": "private-images",
            "Key": location().object_key,
            "Body": data,
            "ContentType": "image/png",
            "Metadata": {"sha256": metadata.sha256},
        },
    )
    stub.add_response(
        "get_object",
        {"Body": body, "ContentLength": len(data), "ETag": '"trusted-looking"'},
        {
            "Bucket": "private-images",
            "Key": location().object_key,
        },
    )
    with pytest.raises(BlobStoreError) as error:
        store(client).put(location(), data, metadata)
    assert error.value.reason == "integrity_error"
    assert raw_stream.closed


def test_versioned_get_closes_remote_body_before_disclosure(sdk):
    client, stub = sdk
    data, metadata = fixture_image()
    raw_stream = BytesIO(data)
    body = StreamingBody(raw_stream, len(data))
    stub.add_response(
        "get_object",
        {"Body": body, "ContentLength": len(data)},
        {
            "Bucket": "private-images",
            "Key": location().object_key,
            "VersionId": "v1",
        },
    )
    with store(client).open(location("v1"), metadata) as source:
        assert raw_stream.closed
        assert source.read() == data
    assert source.closed


def test_oversized_body_closes_without_disclosing_bytes(sdk):
    client, stub = sdk
    data, metadata = fixture_image()
    raw_stream = BytesIO(data + b"extra")
    body = StreamingBody(raw_stream, len(data) + 5)
    stub.add_response(
        "get_object",
        {"Body": body, "ContentLength": len(data) + 5},
        {
            "Bucket": "private-images",
            "Key": location().object_key,
        },
    )
    with pytest.raises(BlobStoreError) as error:
        with store(client).open(location(), metadata):
            pytest.fail("Oversized object was disclosed")
    assert error.value.reason == "integrity_error"
    assert raw_stream.closed


@pytest.mark.parametrize(
    ("code", "status", "reason"),
    [
        ("NoSuchKey", 404, "image_missing"),
        ("AccessDenied", 403, "storage_permission_denied"),
        ("SlowDown", 503, "storage_unavailable"),
    ],
)
def test_service_error_is_classified_and_sanitized(sdk, code, status, reason):
    client, stub = sdk
    _, metadata = fixture_image()
    stub.add_client_error(
        "get_object",
        service_error_code=code,
        service_message="SECRET_TOKEN private-bucket",
        http_status_code=status,
        expected_params={"Bucket": "private-images", "Key": location().object_key},
    )
    with pytest.raises(BlobStoreError) as error:
        with store(client).open(location(), metadata):
            pass
    assert error.value.reason == reason
    assert "SECRET_TOKEN" not in str(error.value)
    assert error.value.__suppress_context__


def test_head_only_reports_size_and_uses_exact_version(sdk):
    client, stub = sdk
    stub.add_response(
        "head_object",
        {"ContentLength": 123},
        {
            "Bucket": "private-images",
            "Key": location().object_key,
            "VersionId": "v1",
        },
    )
    result = store(client).stat(location("v1"))
    assert result.size_bytes == 123


def test_delete_uses_exact_version(sdk):
    client, stub = sdk
    stub.add_response(
        "delete_object",
        {},
        {
            "Bucket": "private-images",
            "Key": location().object_key,
            "VersionId": "v1",
        },
    )
    store(client).delete(location("v1"))


def test_unknown_versions_delete_only_exact_owned_key_across_pages(sdk):
    client, stub = sdk
    key = location().object_key
    stub.add_response(
        "list_object_versions",
        {
            "IsTruncated": True,
            "NextKeyMarker": key,
            "NextVersionIdMarker": "v1",
            "Versions": [
                {"Key": key, "VersionId": "v1"},
                {"Key": key + ".other", "VersionId": "other"},
            ],
        },
        {"Bucket": "private-images", "Prefix": key, "MaxKeys": 1000},
    )
    stub.add_response(
        "delete_object", {}, {"Bucket": "private-images", "Key": key, "VersionId": "v1"}
    )
    stub.add_response(
        "list_object_versions",
        {
            "IsTruncated": False,
            "DeleteMarkers": [{"Key": key, "VersionId": "marker"}],
        },
        {
            "Bucket": "private-images",
            "Prefix": key,
            "MaxKeys": 1000,
            "KeyMarker": key,
            "VersionIdMarker": "v1",
        },
    )
    stub.add_response(
        "delete_object",
        {},
        {"Bucket": "private-images", "Key": key, "VersionId": "marker"},
    )
    store(client).delete(location(), all_versions=True)


def test_wrong_profile_is_rejected_without_sdk_call(sdk):
    client, _ = sdk
    target = location().model_copy(update={"storage_profile_id": "another"})
    with pytest.raises(BlobStoreError) as error:
        store(client).stat(target)
    assert error.value.reason == "invalid_location"


def test_version_listing_has_a_finite_page_budget(sdk):
    client, stub = sdk
    key = location().object_key
    stub.add_response(
        "list_object_versions",
        {"IsTruncated": True, "NextKeyMarker": key, "NextVersionIdMarker": "v1"},
        {"Bucket": "private-images", "Prefix": key, "MaxKeys": 1000},
    )
    bounded = S3ImageBlobStore(
        S3ImageStoreConfig(
            profile_id="archive",
            bucket="private-images",
            region="us-east-1",
            max_delete_pages=1,
        ),
        client=client,
    )
    with pytest.raises(BlobStoreError) as error:
        bounded.delete(location(), all_versions=True)
    assert error.value.reason == "storage_unavailable"


def test_lazy_sdk_receives_bounded_configuration_and_no_plaintext_repr(monkeypatch):
    captured = {}
    client = object()

    def build_client(service, **options):
        captured.update(options)
        assert service == "s3"
        return client

    monkeypatch.setattr(boto3, "client", build_client)
    configured = S3ImageStoreConfig(
        profile_id="archive",
        bucket="private-images",
        region="test-region",
        endpoint_url="http://localhost:9000",
        addressing_style="path",
        access_key_id="FAKE_ACCESS_KEY",
        secret_access_key="FAKE_SECRET_VALUE",
    )
    adapter = S3ImageBlobStore(configured)
    assert not captured
    assert adapter._sdk() is client
    assert captured["verify"] is True
    assert captured["config"].connect_timeout == 5
    assert captured["config"].read_timeout == 30
    assert captured["config"].retries == {"mode": "standard", "total_max_attempts": 3}
    assert captured["config"].s3 == {"addressing_style": "path"}
    assert "FAKE_SECRET_VALUE" not in repr(configured)


def test_closed_store_cannot_reinitialize_client(monkeypatch):
    adapter = store()
    adapter.close()
    with pytest.raises(BlobStoreError):
        adapter.stat(location())


@pytest.mark.parametrize(
    "options",
    [
        {"connect_timeout_seconds": 0},
        {"read_timeout_seconds": -1},
        {"total_max_attempts": 0},
        {"endpoint_url": "https://user:SECRET@host"},
        {"endpoint_url": "file:///secret"},
        {"access_key_id": "unpaired"},
        {"session_token": "unpaired"},
        {"addressing_style": "unknown"},
    ],
)
def test_configuration_rejects_unsafe_or_unbounded_options(options):
    with pytest.raises(ValidationError):
        S3ImageStoreConfig(
            profile_id="archive", bucket="private-images", region="us-east-1", **options
        )
