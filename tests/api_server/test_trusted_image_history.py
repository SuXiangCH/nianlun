from types import SimpleNamespace

import pytest

from app.api_server.database.models import Document, Message, MessageImage
from app.api_server.services.chat_service import ChatService
from tests.api_server.test_answer_image_service import (
    image_context as image_context,
    save_result,
)
from tests.api_server.test_application_publishing import publishing as publishing
from tests.api_server.test_public_runtime import runtime as runtime
from tests.api_server.test_public_answer_images import (
    image_runtime as image_runtime,
    invoke,
)


def commit_image(context):
    context.result["retrieved_snippets"][0]["images"][0]["generation"] = 1
    result = context.service.prepare_result("app-1", context.result)
    result["answer"] = ""
    result["answer_output_mode"] = "images"
    save_result(context, result)
    return result


def test_repository_loads_only_completed_committed_images_with_safe_identity(
    image_context,
):
    context = image_context
    assert not context.chat.trusted_history("app-1", "conversation-1").turns
    commit_image(context)
    history = context.chat.trusted_history("app-1", "conversation-1")
    assert len(history.turns) == 1
    turn = history.turns[0]
    assert turn.answer == "" and turn.answer_output_mode == "images"
    assert turn.images[0].order == 1
    assert turn.images[0].sources[0].generation == 1
    serialized = history.model_dump_json()
    assert "blob_id" not in serialized and "asset_relpath" not in serialized
    assert "url" not in serialized and "ref_ids" not in serialized
    assert not context.chat.trusted_history("other-app", "conversation-1").turns
    assert not context.chat.trusted_history("app-1", "other-conversation").turns
    assert not context.chat.trusted_history(
        "app-1", "conversation-1", channel="api", client_id="other", user_id="user"
    ).turns


def test_repository_marks_deleted_source_unavailable_and_ignores_preview(image_context):
    context = image_context
    result = commit_image(context)
    with context.factory.session_scope(write=True) as session:
        session.get(Document, "doc-1").status = "deleted"
    image = context.chat.trusted_history("app-1", "conversation-1").turns[0].images[0]
    assert image.status == "unavailable" and not image.sources
    with context.factory.session_scope(write=True) as session:
        session.get(MessageImage, result["images"][0]["id"]).display_in_answer = False
    turn = context.chat.trusted_history("app-1", "conversation-1").turns[0]
    assert not turn.images and turn.image_count == 0


@pytest.mark.parametrize("status", ["pending", "failed"])
def test_repository_ignores_unfinished_turn_attachments(image_context, status):
    context = image_context
    commit_image(context)
    with context.factory.session_scope(write=True) as session:
        session.get(Message, "assistant-1").status = status
    assert not context.chat.trusted_history("app-1", "conversation-1").turns


def test_repository_projects_four_valid_sources_and_preserves_source_identity(
    image_context,
):
    import json

    context = image_context
    result = commit_image(context)
    with context.factory.session_scope(write=True) as session:
        attachment = session.get(MessageImage, result["images"][0]["id"])
        source = json.loads(attachment.source_bindings_json)[0]
        attachment.source_bindings_json = json.dumps(
            [
                {
                    **source,
                    "node_id": f"node-{i}",
                    "char_offset": i * 100,
                    "char_limit": 100,
                }
                for i in range(5)
            ]
        )
    image = context.chat.trusted_history("app-1", "conversation-1").turns[0].images[0]
    assert image.sources_truncated and len(image.sources) == 4
    assert image.sources[3].node_id == "node-3" and image.sources[3].char_offset == 300
    assert image.sources[0].content_version == 1 and image.sources[0].generation == 1


def test_repository_keeps_newest_three_completed_turns(image_context):
    from datetime import datetime, timezone

    context = image_context
    commit_image(context)
    for index in range(2, 6):
        context.chat.begin_turn(
            application_id="app-1",
            conversation_id="conversation-1",
            user_message_id=f"user-{index}",
            assistant_message_id=f"assistant-{index}",
            user_content=f"Question {index}",
            now=datetime.now(timezone.utc),
        )
        context.chat.complete_turn(
            application_id="app-1",
            conversation_id="conversation-1",
            assistant_message_id=f"assistant-{index}",
            answer=f"Answer {index}",
            route="direct",
            snippets=[],
            now=datetime.now(timezone.utc),
        )
    history = context.chat.trusted_history("app-1", "conversation-1")
    assert [turn.message_id for turn in history.turns] == [
        "assistant-3",
        "assistant-4",
        "assistant-5",
    ]
    assert not any(turn.images for turn in history.turns)


@pytest.mark.parametrize("stream", [False, True])
def test_console_reconstructs_image_history_on_next_request(
    image_context, monkeypatch, stream
):
    context = image_context
    commit_image(context)
    captured = {}

    def run(*args, **kwargs):
        captured.update(kwargs)
        return {"answer": "Next"}

    def events(*args, **kwargs):
        yield {"type": "done", "data": run(*args, **kwargs)}

    monkeypatch.setattr("app.api_server.services.chat_service.run_agent", run)
    monkeypatch.setattr(
        "app.api_server.services.chat_service.iter_agent_stream_events", events
    )
    service = ChatService(
        SimpleNamespace(require_record=lambda _: {}, runtime=lambda *_, **__: object()),
        context.chat,
    )
    if stream:
        list(service.stream("app-1", "Show that again", "conversation-1")[2])
    else:
        service.complete("app-1", "Show that again", "conversation-1")
    assert captured["trusted_history"].turns[0].message_id == "assistant-1"


def test_native_history_loads_after_admission_checks_and_binds_complete_owner(
    image_runtime, monkeypatch
):
    import asyncio
    from app.api_server.apis.v1.runtime_schemas import PublicChatRequest
    from app.api_server.common.errors import ApiError

    context = image_runtime
    response, _ = invoke(context)
    captured = {}
    loader = context.service.repository.chat.trusted_history

    def history(*args, **kwargs):
        captured["owner"] = (args, kwargs)
        return loader(*args, **kwargs)

    def events(*args, **kwargs):
        captured["history"] = kwargs["trusted_history"]
        yield {"type": "done", "data": {"answer": "Next"}}

    monkeypatch.setattr(context.service.repository.chat, "trusted_history", history)
    monkeypatch.setattr(
        context.service.checkpointer, "get_tuple", lambda *args: object()
    )
    monkeypatch.setattr(
        context.publication.applications,
        "release_runtime",
        lambda *_, **__: SimpleNamespace(iter_events=events),
    )
    body = PublicChatRequest(
        message="Show the first image",
        user_id="user",
        conversation_id=response.conversation_id,
    )
    prepared = context.service.prepare(context.principal, body, "next-turn")
    asyncio.run(context.service.wait(prepared))
    assert captured["history"].turns[0].message_id == response.message_id
    assert captured["owner"] == (
        (context.app_id, response.conversation_id),
        dict(channel="api", client_id=context.principal.client_id, user_id="user"),
    )
    captured.clear()
    with pytest.raises(ApiError) as error:
        context.service.prepare(
            context.principal,
            body.model_copy(update={"user_id": "another"}),
            "another-owner",
        )
    assert error.value.status_code == 404
    assert "owner" not in captured


def test_native_new_conversation_does_not_infer_user_history(
    image_runtime, monkeypatch
):
    import asyncio
    from app.api_server.apis.v1.runtime_schemas import PublicChatRequest

    context = image_runtime
    invoke(context)
    captured = {}

    def events(*args, **kwargs):
        captured["history"] = kwargs["trusted_history"]
        yield {"type": "done", "data": {"answer": "New"}}

    monkeypatch.setattr(
        context.publication.applications,
        "release_runtime",
        lambda *_, **__: SimpleNamespace(iter_events=events),
    )
    prepared = context.service.prepare(
        context.principal,
        PublicChatRequest(message="Show the last image", user_id="user"),
        "new-conversation",
    )
    asyncio.run(context.service.wait(prepared))
    assert captured["history"].turns == ()


@pytest.mark.parametrize("deadline_kind", ["future", "expired", "legacy"])
def test_api_history_enforces_result_deadline_before_cleanup(
    image_runtime, deadline_kind
):
    from datetime import datetime, timedelta, timezone
    from app.api_server.database.models import ApplicationRun

    context = image_runtime
    response, _ = invoke(context)
    deadline = (
        None
        if deadline_kind == "legacy"
        else (
            datetime.now(timezone.utc)
            + timedelta(days=1 if deadline_kind == "future" else -1)
        ).isoformat()
    )
    with context.metadata.factory.session_scope(write=True) as session:
        session.get(ApplicationRun, response.run_id).result_expires_at = deadline
    history = context.service.repository.chat.trusted_history(
        context.app_id,
        response.conversation_id,
        channel="api",
        client_id=context.principal.client_id,
        user_id="user",
    )
    assert history.turns[0].answer.startswith("Original")
    assert history.turns[0].images[0].status == (
        "available" if deadline_kind == "future" else "unavailable"
    )


def test_console_history_image_has_no_result_ttl(image_context):
    context = image_context
    commit_image(context)
    assert (
        context.chat.trusted_history("app-1", "conversation-1")
        .turns[0]
        .images[0]
        .status
        == "available"
    )
