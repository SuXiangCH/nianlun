"""Real local MinIO, isolated temporary buckets, with no model calls."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import subprocess
import sys
from uuid import uuid4

import boto3
import pytest
from botocore.config import Config
from pydantic import SecretStr

from app.api_server.services.answer_image_service import AnswerImageService
from app.api_server.storage import validate_image
from app.api_server.storage.image_blob_store import BlobStoreError, ImageBlobLocation
from app.api_server.storage.s3_image_blob_store import (
    S3ImageBlobStore,
    S3ImageStoreConfig,
)
from tests.api_server.test_application_publishing import publishing as publishing
from tests.api_server.test_public_runtime import runtime as runtime
from tests.api_server.test_public_answer_images import (
    image_runtime as image_runtime,
    invoke,
)
from tests.api_server.test_answer_image_service import (
    image_context as image_context,
    save_result,
)

from .conftest import (
    available_port,
    certificate,
    executable,
    running_process,
    wait_ready,
)


@pytest.fixture(scope="module", params=[False, True], ids=["http", "tls"])
def minio_server(request, tmp_path_factory, image_infrastructure):
    root = tmp_path_factory.mktemp("minio-acceptance")
    certs = root / "certs"
    ca = certificate(certs)[0] if request.param else None
    port, console_port = available_port(), available_port()
    endpoint = f"{'https' if ca else 'http'}://127.0.0.1:{port}"
    access, secret = "acceptance" + uuid4().hex, uuid4().hex + uuid4().hex
    env = {
        key: value
        for key, value in os.environ.items()
        if key in {"PATH", "HOME", "TMPDIR", "LANG"}
    }
    env.update(MINIO_ROOT_USER=access, MINIO_ROOT_PASSWORD=secret, MINIO_BROWSER="off")
    command = [
        executable("minio"),
        "server",
        "--address",
        f"127.0.0.1:{port}",
        "--console-address",
        f"127.0.0.1:{console_port}",
        "--certs-dir",
        str(certs),
        str(root / "data"),
    ]
    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        region_name="us-east-1",
        aws_access_key_id=access,
        aws_secret_access_key=secret,
        verify=str(ca) if ca else True,
        config=Config(
            s3={"addressing_style": "path"}, retries={"total_max_attempts": 1}
        ),
    )
    with running_process(command, root / "minio.log", env=env) as process:
        wait_ready(process, endpoint + "/minio/health/live", ca=ca)
        try:
            yield client, endpoint, access, secret, ca
        finally:
            client.close()


@pytest.fixture(params=[False, True], ids=["unversioned", "versioned"])
def object_store(minio_server, request):
    client, endpoint, access, secret, ca = minio_server
    bucket = "nianlun-acceptance-" + uuid4().hex
    client.create_bucket(Bucket=bucket)
    if request.param:
        client.put_bucket_versioning(
            Bucket=bucket, VersioningConfiguration={"Status": "Enabled"}
        )
    config = S3ImageStoreConfig(
        profile_id="acceptance",
        bucket=bucket,
        region="us-east-1",
        endpoint_url=endpoint,
        addressing_style="path",
        ca_bundle=str(ca) if ca else None,
        access_key_id=SecretStr(access),
        secret_access_key=SecretStr(secret),
        total_max_attempts=1,
    )
    store = S3ImageBlobStore(config)
    try:
        yield store, client, bool(request.param)
    finally:
        store.close()
        # This bucket was created above; cleanup never enumerates user buckets.
        for page in client.get_paginator("list_object_versions").paginate(
            Bucket=bucket
        ):
            for record in [*page.get("Versions", []), *page.get("DeleteMarkers", [])]:
                client.delete_object(
                    Bucket=bucket, Key=record["Key"], VersionId=record["VersionId"]
                )
        client.delete_bucket(Bucket=bucket)


def test_original_bytes_reopen_and_exact_version_collection(
    object_store, image_runtime
):
    store, client, versioned = object_store
    raw = image_runtime.raw_image
    metadata = validate_image(raw)
    location = ImageBlobLocation(
        backend="s3",
        storage_profile_id="acceptance",
        object_key="answer-images/original.png",
    )
    uploaded = store.put(location, raw, metadata)
    assert bool(uploaded.version_id) == versioned
    assert store.stat(uploaded).size_bytes == len(raw)
    store.close()
    reopened = S3ImageBlobStore(store.config)
    try:
        with reopened.open(uploaded, metadata) as stream:
            assert stream.read() == raw
        assert stream.closed
        second = reopened.put(location, raw, metadata)
        if versioned:
            assert second.version_id != uploaded.version_id
            reopened.delete(uploaded)
            with reopened.open(second, metadata) as stream:
                assert stream.read() == raw
        reopened.delete(location, all_versions=True)
        versions = client.list_object_versions(Bucket=store.config.bucket)
        assert not versions.get("Versions")
        assert not versions.get("DeleteMarkers")
        with pytest.raises(BlobStoreError) as missing:
            with reopened.open(second, metadata):
                pytest.fail("Deleted object was disclosed")
        assert missing.value.reason == "image_missing"
    finally:
        reopened.close()


@pytest.mark.parametrize("transport", ["url", "data_url"])
def test_public_result_replay_and_revocation_use_real_objects(
    object_store, image_runtime, transport
):
    from app.api_server.database.models import Document

    store, _client, _versioned = object_store
    context = image_runtime
    service = context.service.image_service
    service.blob_store = store
    service.profile_id = store.config.profile_id
    service.backend = "s3"
    result, request = invoke(context, transport)
    assert len(result.images) == 1
    assert result.images[0].status == "available"
    assert len(context.calls) == 1
    # Recreate the adapter and service against persisted locators, as after restart.
    store.close()
    reopened = S3ImageBlobStore(store.config)
    restored = AnswerImageService(
        service.repository,
        reopened,
        service.knowledge_base_lookup,
        service.application_lookup,
        profile_id=store.config.profile_id,
        backend="s3",
    )
    context.service.image_service = restored
    try:
        repeated = asyncio.run(
            context.service.wait(
                context.service.prepare(context.principal, request, "image-key")
            )
        )
        assert repeated == result
        if transport == "data_url":
            assert (
                base64.b64decode(repeated.images[0].url.split(",", 1)[1])
                == context.raw_image
            )
        else:
            from urllib.parse import parse_qs, urlsplit

            url = urlsplit(repeated.images[0].url)
            assert (
                context.service.image_access.read(
                    url.path.rsplit("/", 1)[1], parse_qs(url.query)["token"][0]
                )[0]
                == context.raw_image
            )
        with context.metadata.factory.session_scope(write=True) as session:
            session.get(Document, "doc-image").status = "deleted"
        revoked = context.service.get(context.principal, result.run_id, "user").result
        if transport == "data_url":
            assert revoked.images[0].url is None
            assert "base64" not in revoked.answer
        else:
            from app.api_server.common.errors import ApiError

            with pytest.raises(ApiError):
                context.service.image_access.read(
                    url.path.rsplit("/", 1)[1], parse_qs(url.query)["token"][0]
                )
        assert len(context.calls) == 1
    finally:
        restored.shutdown()


def test_permission_and_ca_failures_are_sanitized(object_store, image_runtime):
    store, _client, _versioned = object_store
    wrong = S3ImageBlobStore(
        store.config.model_copy(
            update={"secret_access_key": SecretStr("incorrect-acceptance-secret")}
        )
    )
    location = ImageBlobLocation(
        backend="s3",
        storage_profile_id="acceptance",
        object_key="answer-images/missing.png",
    )
    try:
        with pytest.raises(BlobStoreError) as denied:
            wrong.stat(location)
        assert denied.value.reason == "storage_permission_denied"
        assert "incorrect-acceptance-secret" not in str(denied.value)
    finally:
        wrong.close()
    if store.config.ca_bundle:
        untrusted = S3ImageBlobStore(
            store.config.model_copy(update={"ca_bundle": None})
        )
        try:
            with pytest.raises(BlobStoreError) as failure:
                untrusted.stat(location)
            assert failure.value.reason == "storage_unavailable"
        finally:
            untrusted.close()


def test_late_upload_and_gc_keep_replacement_object_intact(object_store, image_context):
    from datetime import datetime, timedelta, timezone

    from app.api_server.database.models import AnswerImageBlob

    store, client, _versioned = object_store
    context = image_context
    context.service.blob_store = store
    context.service.profile_id = store.config.profile_id
    context.service.backend = "s3"
    first = context.service.prepare_result("app-1", context.result)
    old_id = first["images"][0]["blob_id"]
    with context.factory.session_scope(write=True) as session:
        session.get(AnswerImageBlob, old_id).lease_expires_at = (
            datetime.now(timezone.utc) - timedelta(seconds=1)
        ).isoformat()
    replacement = context.service.prepare_result("app-1", context.result)
    new_id = replacement["images"][0]["blob_id"]
    assert new_id != old_id
    assert context.repository.blob(old_id)["status"] == "retired"
    save_result(context, replacement)
    old = context.repository.blob(old_id)
    location = ImageBlobLocation(
        backend="s3",
        storage_profile_id=old["storage_profile_id"],
        object_key=old["object_key"],
    )
    metadata = validate_image(context.raw)
    for _ in range(2):
        # Includes a PUT that arrives after the first DELETE and its GC receipt.
        store.put(location, context.raw, metadata)
        assert context.service.collect_orphans(grace_hours=0) >= 1
        objects = client.list_object_versions(Bucket=store.config.bucket)
        assert all(
            row["Key"] != old["object_key"]
            for row in [*objects.get("Versions", []), *objects.get("DeleteMarkers", [])]
        )
        assert context.service.read_content(
            "app-1", "conversation-1", "assistant-1", replacement["images"][0]["id"]
        ) == (context.raw, "image/png")
    assert context.repository.blob(new_id)["status"] == "ready"


def test_committed_attachment_recovers_in_new_python_process(
    object_store, image_context
):
    from tests.api_server.test_image_deployment import ROOT

    store, _client, _versioned = object_store
    context = image_context
    context.service.blob_store = store
    context.service.profile_id = store.config.profile_id
    context.service.backend = "s3"
    result = context.service.prepare_result("app-1", context.result)
    save_result(context, result)
    context.service.shutdown()
    context.factory.dispose()
    config = store.config.model_dump(
        mode="json", exclude={"access_key_id", "secret_access_key"}
    )
    config.update(
        access_key_id=store.config.access_key_id.get_secret_value(),
        secret_access_key=store.config.secret_access_key.get_secret_value(),
    )
    payload = {
        "config": config,
        "database": str(context.factory.path),
        "attachment_id": result["images"][0]["id"],
        "sha256": validate_image(context.raw).sha256,
    }
    env = {
        key: value
        for key, value in os.environ.items()
        if key in {"PATH", "HOME", "TMPDIR", "LANG"}
    }
    env["PYTHON_DOTENV_DISABLED"] = "1"
    # Credentials travel only over the temporary pipe, never command arguments,
    # persisted configuration, or subprocess output.
    recovered = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import hashlib, json, sys
from pathlib import Path
from app.api_server.database import SQLiteConnectionFactory
from app.api_server.repositories.answer_images import AnswerImageRepository
from app.api_server.services.answer_image_service import AnswerImageService
from app.api_server.storage.s3_image_blob_store import S3ImageBlobStore, S3ImageStoreConfig
payload = json.load(sys.stdin)
factory = SQLiteConnectionFactory(Path(payload['database']))
config = S3ImageStoreConfig.model_validate(payload['config'])
service = AnswerImageService(
    AnswerImageRepository(factory), S3ImageBlobStore(config), lambda _: {},
    lambda _: {'knowledge_base_id': 'kb-1'}, profile_id=config.profile_id, backend='s3',
)
try:
    data, mime = service.read_content('app-1', 'conversation-1', 'assistant-1', payload['attachment_id'])
    assert mime == 'image/png'
    assert hashlib.sha256(data).hexdigest() == payload['sha256']
    print('recovered')
finally:
    service.shutdown()
    factory.dispose()
""",
        ],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        env=env,
        cwd=ROOT,
        timeout=30,
    )
    assert recovered.returncode == 0, recovered.stderr
    assert recovered.stdout.strip() == "recovered"
