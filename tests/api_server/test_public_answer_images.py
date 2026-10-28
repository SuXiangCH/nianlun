import asyncio
import base64
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from io import BytesIO
from types import SimpleNamespace
from urllib.parse import urlsplit, parse_qs

import pytest
from PIL import Image

from app.api_server.apis.v1.runtime_schemas import PublicChatRequest
from app.api_server.common.errors import ApiError
from app.api_server.database.models import ApplicationRun, Document, MessageImage
from app.api_server.database.models.publishing import ApplicationApiClient
from app.api_server.repositories.answer_images import AnswerImageRepository
from app.api_server.services.answer_image_service import AnswerImageService
from app.api_server.storage import validate_image
from app.api_server.storage.filesystem_image_blob_store import FilesystemImageBlobStore
from tests.api_server.test_application_publishing import publishing as publishing
from tests.api_server.test_public_runtime import runtime as runtime


@pytest.fixture
def image_runtime(runtime, tmp_path, monkeypatch):
    now = datetime.now(timezone.utc).isoformat()
    with runtime.metadata.factory.session_scope(write=True) as session:
        session.add(
            Document(
                id="doc-image",
                knowledge_base_id="kb",
                original_filename="image.md",
                file_extension=".md",
                size_bytes=10,
                source_relpath="image.md",
                source_sha256="b" * 64,
                parser="native_markdown",
                status="ready",
                created_at=now,
                updated_at=now,
            )
        )
    buffer = BytesIO()
    with Image.new("RGB", (8, 6), "blue") as image:
        image.save(buffer, "PNG")
    raw = buffer.getvalue()
    metadata = validate_image(raw)
    root = tmp_path / "image-workspace"
    root.mkdir()
    (root / "image.png").write_bytes(raw)
    image_id = "img_" + metadata.sha256[:24]
    images = AnswerImageService(
        AnswerImageRepository(runtime.metadata.factory),
        FilesystemImageBlobStore(tmp_path / "archive", profile_id="local"),
        lambda _id: {"workspace_dir": str(root)},
        runtime.publication.applications.require_record,
        profile_id="local",
    )
    runtime.service.image_service = images
    runtime.service.settings = replace(
        runtime.settings, application_api_public_base_url="https://api.test"
    )

    def events(*args, **kwargs):
        runtime.calls.append("call")
        yield {
            "type": "message",
            "data": {"delta": "![candidate](nianlun-image:forged)"},
        }
        yield {
            "type": "done",
            "data": {
                "answer": f"Original [1]\n\n![model](nianlun-image:{image_id})",
                "usage": {"input_tokens": 12, "output_tokens": 7, "total_tokens": 19},
                "retrieved_snippets": [
                    {
                        "doc_id": "doc-image",
                        "node_id": "node",
                        "citation_id": 1,
                        "content_version": 1,
                        "generation": 1,
                        "text": "caption",
                        "images": [
                            {
                                "image_id": image_id,
                                "asset_relpath": "image.png",
                                "alt_text": "Source image",
                                **metadata.model_dump(),
                            }
                        ],
                    }
                ],
            },
        }

    monkeypatch.setattr(
        runtime.publication.applications,
        "release_runtime",
        lambda *args, **kwargs: SimpleNamespace(
            iter_events=events, iter_public_events=events
        ),
    )
    runtime.raw_image = raw
    return runtime


def invoke(context, transport="data_url", key="image-key"):
    request = PublicChatRequest(
        message="show image",
        user_id="user",
        include_images=True,
        image_transport=transport,
    )
    prepared = context.service.prepare(context.principal, request, key)
    return asyncio.run(context.service.wait(prepared)), request


def test_inline_run_is_replayable_without_storing_base64(image_runtime):
    context = image_runtime
    response, request = invoke(context)
    target = "data:image/png;base64," + base64.b64encode(context.raw_image).decode()
    assert response.images[0].url == target
    assert target in response.answer
    row = context.service.repository.get(response.run_id)
    assert "base64" not in row["result_json"]
    assert "inline_projection_v1" in row["result_json"]
    history = context.service.repository.chat.get_messages(
        context.app_id, response.conversation_id
    )
    assert "nianlun-image:" in history[-1]["content"]
    assert "base64" not in str(history)
    repeated = context.service.prepare(context.principal, request, "image-key")
    assert asyncio.run(context.service.wait(repeated)) == response
    assert (
        context.service.get(context.principal, response.run_id, "user").result
        == response
    )
    assert len(context.calls) == 1
    with pytest.raises(ApiError) as error:
        context.service.prepare(
            context.principal,
            request.model_copy(update={"image_transport": "url"}),
            "image-key",
        )
    assert error.value.status_code == 409
    with pytest.raises(ApiError) as error:
        context.service.get(context.principal, response.run_id, "other")
    assert error.value.status_code == 404


def test_inline_replay_rechecks_source_and_result_deadline(image_runtime):
    context = image_runtime
    response, request = invoke(context)
    with context.metadata.factory.session_scope(write=True) as session:
        session.get(Document, "doc-image").status = "deleted"
    replayed = context.service.get(context.principal, response.run_id, "user").result
    assert replayed.images[0].url is None
    assert "base64" not in replayed.answer
    with context.metadata.factory.session_scope(write=True) as session:
        session.get(ApplicationRun, response.run_id).result_expires_at = (
            datetime.now(timezone.utc) - timedelta(seconds=1)
        ).isoformat()
    for operation in [
        lambda: context.service.get(context.principal, response.run_id, "user"),
        lambda: context.service.prepare(context.principal, request, "image-key"),
    ]:
        with pytest.raises(ApiError) as error:
            operation()
        assert error.value.status_code == 410


def test_url_run_grant_refresh_and_snapshot_replay(image_runtime):
    context = image_runtime
    response, request = invoke(context, "url")
    image = response.images[0]
    assert image.url.startswith("https://api.test/api/v1/runtime/images/")
    parsed = urlsplit(image.url)
    grant_id = parsed.path.rsplit("/", 1)[1]
    token = parse_qs(parsed.query)["token"][0]
    access = context.service.image_access
    assert access.read(grant_id, token) == (context.raw_image, "image/png")
    with pytest.raises(ApiError) as error:
        access.read(grant_id, "wrong")
    assert error.value.status_code == 404
    refreshed = access.refresh(context.principal, response.run_id, image.id, "user")
    assert refreshed.url != image.url
    replayed = asyncio.run(
        context.service.wait(
            context.service.prepare(context.principal, request, "image-key")
        )
    )
    assert replayed == response
    assert "token=" not in str(context.service.list(context.app_id))
    with pytest.raises(ApiError) as error:
        access.refresh(context.principal, response.run_id, image.id, "other")
    assert error.value.status_code == 404


def test_inline_request_does_not_need_public_base_url(image_runtime):
    context = image_runtime
    context.service.settings = replace(
        context.service.settings, application_api_public_base_url=None
    )
    response, _ = invoke(context)
    assert response.images[0].url.startswith("data:")
    with pytest.raises(ApiError) as error:
        invoke(context, "url", "url-key")
    assert error.value.status_code == 503


@pytest.mark.parametrize("transport", ["url", "data_url"])
def test_run_list_preserves_usage_without_loading_response_snapshot(
    image_runtime, transport
):
    context = image_runtime
    response, _ = invoke(context, transport)
    with context.metadata.factory.session_scope(write=True) as session:
        session.get(ApplicationRun, response.run_id).result_json = "not valid JSON"
    row = context.service.repository.list(context.app_id)[0]
    assert "result_json" not in row
    item = context.service.list(context.app_id)["items"][0]
    assert item["usage"] == {"input_tokens": 12, "output_tokens": 7, "total_tokens": 19}
    assert item["result"] is None


def test_completion_preserves_permission_error_when_client_revoked(
    image_runtime, monkeypatch
):
    context = image_runtime
    images = context.service.image_service
    read = images.read_prepared_image

    def revoke_client(image):
        raw = read(image)
        with context.metadata.factory.session_scope(write=True) as session:
            session.get(
                ApplicationApiClient, context.principal.client_id
            ).enabled = False
        return raw

    monkeypatch.setattr(images, "read_prepared_image", revoke_client)
    with pytest.raises(ApiError) as error:
        invoke(context)
    assert error.value.status_code == 403
    rows = context.service.repository.list(context.app_id)
    assert rows[0]["status"] == "failed"
    assert rows[0]["error_status"] == 403


@pytest.mark.parametrize(
    "field,value",
    [
        ("sha256", "0" * 64),
        ("size_bytes", 1),
        ("media_type", "image/jpeg"),
    ],
)
def test_inline_replay_refuses_changed_frozen_binding(image_runtime, field, value):
    context = image_runtime
    response, _ = invoke(context)
    with context.metadata.factory.session_scope(write=True) as session:
        row = session.get(ApplicationRun, response.run_id)
        stored = json.loads(row.result_json)
        stored["bindings"][0][field] = value
        row.result_json = json.dumps(stored)
    replay = context.service.get(context.principal, response.run_id, "user").result
    assert replay.images[0].url is None
    assert replay.images[0].status == "unavailable"
    assert "base64" not in replay.answer


def test_expired_public_result_cannot_be_read_via_console_image_url(image_runtime):
    context = image_runtime
    response, _ = invoke(context)
    images = context.service.image_service
    with context.metadata.factory.session_scope(write=True) as session:
        session.get(ApplicationRun, response.run_id).result_expires_at = (
            datetime.now(timezone.utc) - timedelta(seconds=1)
        ).isoformat()
    projection = images.console_images(
        context.app_id, response.conversation_id, response.message_id
    )
    assert projection[0]["status"] == "unavailable"
    assert projection[0]["url"] is None
    with pytest.raises(ApiError) as error:
        images.read_content(
            context.app_id,
            response.conversation_id,
            response.message_id,
            response.images[0].id,
        )
    assert error.value.status_code == 404


def test_grant_refresh_rechecks_blob_state_in_write_transaction(
    image_runtime, monkeypatch
):
    context = image_runtime
    response, _ = invoke(context, "url")
    access = context.service.image_access
    image = context.service.image_service.repository.message_images(
        response.message_id
    )[0]
    plan = access.plan_grant

    def retire_before_save(*args):
        result = plan(*args)
        context.service.image_service.repository.retire_blob(image["blob_id"])
        return result

    monkeypatch.setattr(access, "plan_grant", retire_before_save)
    with pytest.raises(ApiError) as error:
        access.refresh(context.principal, response.run_id, image["id"], "user")
    assert error.value.status_code == 404


def test_source_preview_cannot_receive_public_grant(image_runtime):
    context = image_runtime
    response, _ = invoke(context, "url")
    with context.metadata.factory.session_scope(write=True) as session:
        session.get(MessageImage, response.images[0].id).display_in_answer = False
    with pytest.raises(ApiError) as error:
        context.service.image_access.refresh(
            context.principal, response.run_id, response.images[0].id, "user"
        )
    assert error.value.status_code == 404


def test_inline_replay_rechecks_deadline_after_reading_bytes(
    image_runtime, monkeypatch
):
    context = image_runtime
    response, _ = invoke(context)
    images = context.service.image_service
    read = images.read_content

    def expire_run(*args):
        content = read(*args)
        with context.metadata.factory.session_scope(write=True) as session:
            session.get(ApplicationRun, response.run_id).result_expires_at = (
                datetime.now(timezone.utc) - timedelta(seconds=1)
            ).isoformat()
        return content

    monkeypatch.setattr(images, "read_content", expire_run)
    with pytest.raises(ApiError) as error:
        context.service.get(context.principal, response.run_id, "user")
    assert error.value.status_code == 410


def test_unknown_result_snapshot_version_has_safe_domain_error(image_runtime):
    context = image_runtime
    response, _ = invoke(context)
    with context.metadata.factory.session_scope(write=True) as session:
        session.get(ApplicationRun, response.run_id).result_json = json.dumps(
            {"storage_format": "inline_projection_v999", "secret": "do-not-disclose"}
        )
    with pytest.raises(ApiError) as error:
        context.service.get(context.principal, response.run_id, "user")
    assert error.value.status_code == 500
    assert "do-not-disclose" not in error.value.message


@pytest.mark.parametrize("stage", ["delta", "done"])
def test_image_output_rejects_oversized_canonical_text(
    image_runtime, monkeypatch, stage
):
    context = image_runtime

    def events(*args, **kwargs):
        oversized = "图" * (1024 * 1024 // 3 + 1)
        if stage == "delta":
            yield {"type": "message", "data": {"delta": oversized}}
        yield {
            "type": "done",
            "data": {"answer": oversized if stage == "done" else "small"},
        }

    monkeypatch.setattr(
        context.publication.applications,
        "release_runtime",
        lambda *args, **kwargs: SimpleNamespace(iter_events=events),
    )
    with pytest.raises(ApiError) as error:
        invoke(context)
    assert error.value.status_code == 413
    row = context.service.repository.list(context.app_id)[0]
    assert row["status"] == "failed"
    assert (
        context.service.repository.chat.get_messages(
            context.app_id, row["conversation_id"]
        )[-1]["status"]
        == "failed"
    )


@pytest.mark.parametrize("transport", ["url", "data_url"])
@pytest.mark.parametrize("stream", [False, True])
def test_openai_projects_images_after_completion(image_runtime, transport, stream):
    import json
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api_server.apis.v1.router import v1_router
    from app.api_server.common.errors import api_error_handler

    context = image_runtime
    app = FastAPI()
    app.add_exception_handler(ApiError, api_error_handler)
    app.state.services = SimpleNamespace(invocations=context.service)
    app.include_router(v1_router)
    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {context.token}",
                "X-Nianlun-Include-Images": "true",
                "X-Nianlun-Image-Transport": transport,
            },
            json={
                "model": context.app_id,
                "messages": [{"role": "user", "content": "show image"}],
                "stream": stream,
            },
        )
        assert response.status_code == 200
        if stream:
            chunks = [
                json.loads(line.removeprefix("data: "))
                for line in response.text.splitlines()
                if line.startswith("data: ") and line != "data: [DONE]"
            ]
            content = "".join(
                choice["delta"].get("content", "")
                for chunk in chunks
                for choice in chunk["choices"]
            )
            assert response.text.endswith("data: [DONE]\n\n")
        else:
            content = response.json()["choices"][0]["message"]["content"]
        assert "nianlun-image:" not in content
        assert "candidate" not in content
        assert "![Source image](" in content
        assert (
            "data:image/png;base64,"
            if transport == "data_url"
            else "https://api.test/api/v1/runtime/images/"
        ) in content


def test_public_grant_endpoints_and_refresh_use_existing_envelopes(image_runtime):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api_server.apis.v1.router import v1_router
    from app.api_server.common.errors import api_error_handler

    context = image_runtime
    response, _ = invoke(context, "url")
    app = FastAPI()
    app.add_exception_handler(ApiError, api_error_handler)
    app.state.services = SimpleNamespace(invocations=context.service)
    app.include_router(v1_router)
    with TestClient(app) as client:
        parsed = urlsplit(response.images[0].url)
        downloaded = client.get(parsed.path + "?" + parsed.query)
        assert downloaded.status_code == 200
        assert downloaded.content == context.raw_image
        assert downloaded.headers["cache-control"] == "private, no-store"
        path = f"/api/v1/runtime/apps/{context.app_id}/runs/{response.run_id}/images/{response.images[0].id}/access"
        refreshed = client.post(
            path,
            headers={"Authorization": f"Bearer {context.token}"},
            json={"user_id": "user"},
        )
        assert refreshed.status_code == 200
        assert refreshed.headers["cache-control"] == "private, no-store"
        assert refreshed.json()["data"]["url"] != response.images[0].url


@pytest.mark.parametrize("native", [True, False])
@pytest.mark.parametrize("transport", ["url", "data_url"])
def test_image_json_results_and_replay_are_not_cacheable(
    image_runtime, native, transport
):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api_server.apis.v1.router import v1_router

    context = image_runtime
    app = FastAPI()
    app.state.services = SimpleNamespace(invocations=context.service)
    app.include_router(v1_router)
    headers = {
        "Authorization": f"Bearer {context.token}",
        "Idempotency-Key": "cache-check",
        "X-Nianlun-Include-Images": "true",
        "X-Nianlun-Image-Transport": transport,
    }
    path = (
        f"/api/v1/runtime/apps/{context.app_id}/chat"
        if native
        else "/v1/chat/completions"
    )
    body = (
        {
            "message": "show image",
            "user_id": "user",
            "include_images": True,
            "image_transport": transport,
        }
        if native
        else {
            "model": context.app_id,
            "user": "user",
            "messages": [{"role": "user", "content": "show image"}],
        }
    )
    with TestClient(app) as client:
        for _ in range(2):
            result = client.post(path, headers=headers, json=body)
            assert result.status_code == 200
            assert result.headers["cache-control"] == "private, no-store"
            detail = client.get(result.headers["Location"], headers=headers)
            assert detail.status_code == 200
            assert detail.json()["data"]["result"]["images"]
            assert detail.headers["cache-control"] == "private, no-store"


def test_request_keeps_admission_policy_until_archive(image_runtime, monkeypatch):
    import threading

    context = image_runtime
    context.service.settings = replace(
        context.service.settings, answer_image_inline_max_bytes=1
    )
    entered, proceed = threading.Event(), threading.Event()
    original = context.service.image_service.prepare_result

    def delayed(*args, **kwargs):
        entered.set()
        assert proceed.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(context.service.image_service, "prepare_result", delayed)
    request = PublicChatRequest(
        message="Show the original image",
        user_id="user",
        include_images=True,
        image_transport="data_url",
    )
    prepared = context.service.prepare(context.principal, request, "frozen-budget")
    try:
        assert entered.wait(5)
        assert prepared.image_policy.inline_max_bytes == 1
        context.service.settings = replace(
            context.service.settings,
            answer_image_inline_max_bytes=1024 * 1024,
        )
    finally:
        proceed.set()
    response = asyncio.run(context.service.wait(prepared))
    assert response.images[0].status == "unavailable"
    assert response.images[0].unavailable_reason == "inline_budget_exceeded"
    assert response.images[0].url is None


def test_inline_replay_keeps_original_ast_and_encoding_budget(image_runtime):
    context = image_runtime
    response, request = invoke(context)
    context.service.settings = replace(
        context.service.settings,
        answer_image_inline_max_bytes=1,
        answer_image_max_candidate_line_bytes=1,
        answer_image_max_canonical_bytes=1,
        answer_image_inline_response_max_bytes=1,
    )
    repeated = context.service.prepare(context.principal, request, "image-key")
    assert asyncio.run(context.service.wait(repeated)) == response


def test_expiry_cleanup_unbinds_public_result_but_preserves_null_legacy(image_runtime):
    context = image_runtime
    first, _ = invoke(context, key="expired-run")
    second, _ = invoke(context, key="legacy-run")
    with context.metadata.factory.session_scope(write=True) as session:
        session.get(ApplicationRun, first.run_id).result_expires_at = (
            datetime.now(timezone.utc) - timedelta(seconds=1)
        ).isoformat()
        session.get(ApplicationRun, second.run_id).result_expires_at = None
    assert context.service.image_service.repository.expire_results() == 1
    assert context.service.repository.get(first.run_id)["result_json"] is None
    assert context.service.repository.get(second.run_id)["result_json"] is not None
    first_image = context.service.image_service.repository.message_images(
        first.message_id
    )[0]
    assert first_image["blob_id"] is None
    assert first_image["unavailable_reason"] == "result_expired"
    with pytest.raises(ApiError) as error:
        context.service.get(context.principal, first.run_id, "user")
    assert error.value.status_code == 410


def test_source_loss_during_completion_keeps_text_without_image(
    image_runtime, monkeypatch
):
    context = image_runtime
    original = context.service.repository.complete

    def delete_then_complete(*args, **kwargs):
        with context.metadata.factory.session_scope(write=True) as session:
            session.get(Document, "doc-image").status = "deleted"
        return original(*args, **kwargs)

    monkeypatch.setattr(context.service.repository, "complete", delete_then_complete)
    response, _ = invoke(context)
    assert "Original" in response.answer
    assert "data:image" not in response.answer
    assert response.answer_output_mode == "text"
    assert response.images[0].status == "unavailable"
