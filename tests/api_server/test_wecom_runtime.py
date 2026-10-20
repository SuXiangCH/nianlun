import asyncio
from datetime import datetime, timedelta, timezone
import json
import threading
import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api_server.apis.internal.router import internal_router
from app.api_server.apis.v1.publishing_schemas import (
    ApiClientCreateRequest,
    ApiKeyCreateRequest,
)
from app.api_server.common.errors import ApiError, api_error_handler
from app.api_server.config import ApiServerSettings
from app.api_server.database.models.chat import Conversation
from app.api_server.database.models.publishing import ApplicationWecomBotRevision
from app.api_server.repositories.sqlite_chat import SQLiteChatRepository
from app.api_server.repositories.wecom_channels import WecomChannelRepository
from app.api_server.services.application_invocation_service import (
    ApplicationInvocationService,
)
from tests.api_server.test_application_publishing import publish
from tests.api_server.test_application_publishing import publishing as publishing

TOKEN = "i" * 32
USER_KEY = "wc_" + "a" * 32


@pytest.fixture
def runtime(publishing, tmp_path, monkeypatch):
    from langchain_core.messages import AIMessage, HumanMessage
    from langgraph.graph import END, START, MessagesState, StateGraph

    publication, app_id, metadata = publishing
    publish(publication, app_id)
    release = publication.require_release(app_id)
    repository = WecomChannelRepository(metadata.factory)
    channel = repository.create(
        app_id,
        bot_id="bot-runtime",
        secret="runtime-secret",
        default_response="fallback",
        expires_at=None,
    )
    channel = repository.deploy(
        app_id,
        release.id,
        expected_deployment_version=0,
        content_version=1,
    )
    calls = []

    def build_runtime(config, checkpointer, record):
        builder = StateGraph(MessagesState)
        builder.add_node(
            "answer",
            lambda state: {
                "messages": [AIMessage(content=f"turn {len(state['messages'])}")]
            },
        )
        builder.add_edge(START, "answer")
        builder.add_edge("answer", END)
        graph = builder.compile(checkpointer=checkpointer)

        def events(message, thread_id, **kwargs):
            calls.append((config, record.copy()))
            yield {"type": "message", "data": {"delta": "private candidate"}}
            result = graph.invoke(
                {"messages": [HumanMessage(content=message)]},
                {"configurable": {"thread_id": thread_id}},
            )
            yield {
                "type": "done",
                "data": {
                    "answer": result["messages"][-1].content,
                    "retrieved_snippets": [
                        {
                            "doc_id": "doc",
                            "node_id": "node",
                            "line_spec": "1",
                            "text": "evidence",
                            "path": "/private/path",
                        }
                    ],
                },
            }

        return SimpleNamespace(iter_events=events)

    monkeypatch.setattr(publication.applications, "release_runtime", build_runtime)
    monkeypatch.setattr(
        publication.applications, "release_runtime_resources", lambda runtime: None
    )
    settings = ApiServerSettings(
        data_dir=tmp_path,
        application_api_enabled=True,
        internal_token=TOKEN,
        wecom_conversation_retention_days=2,
    )
    service = ApplicationInvocationService(
        publication, publication.applications, metadata.factory, settings
    )
    client = publication.create_client(app_id, ApiClientCreateRequest(name="Caller"))
    token = "nl_app_" + "e" * 32 + "." + "s" * 43
    publication.register_key(
        app_id, client.id, ApiKeyCreateRequest(name="Key", api_key=token)
    )
    principal = service.authenticate(app_id, f"Bearer {token}")
    app = FastAPI()
    app.state.settings = settings
    app.state.services = SimpleNamespace(invocations=service, wecom=None)
    app.include_router(internal_router)
    app.add_exception_handler(ApiError, api_error_handler)
    with TestClient(app) as http:
        yield SimpleNamespace(
            service=service,
            publication=publication,
            app_id=app_id,
            metadata=metadata,
            channel=channel,
            channel_id=channel["id"],
            revision=channel["config_revision"],
            release=release,
            calls=calls,
            settings=settings,
            http=http,
            principal=principal,
            token=token,
            client_id=client.id,
        )
    service.close()


def _attempt(
    runtime,
    key,
    text,
    *,
    user_key=USER_KEY,
    revision=None,
    channel_id=None,
):
    return runtime.http.post(
        f"/internal/v1/wecom/channels/{channel_id or runtime.channel_id}/runs",
        json={
            "revision": runtime.revision if revision is None else revision,
            "message": {"user_key": user_key, "text": text},
        },
        headers={
            "Authorization": "Bearer " + TOKEN,
            "Idempotency-Key": key,
        },
    )


def _events(response):
    return [json.loads(line) for line in response.text.splitlines() if line]


def test_first_message_streams_and_persists_channel_conversation(runtime):
    response = _attempt(runtime, "first", "hello")
    assert response.status_code == 200
    assert response.headers["Content-Type"].startswith("application/x-ndjson")
    assert response.headers["Cache-Control"] == "no-store"
    events = _events(response)
    assert [event["type"] for event in events] == ["start", "chunk", "done"]
    assert events[0]["release_id"] == runtime.release.id
    assert events[1]["content"] == "private candidate"
    assert events[2]["content"] == "turn 1"

    chat = SQLiteChatRepository(runtime.metadata.factory)
    conversations = chat.list_conversations(runtime.app_id, "wecom")
    assert len(conversations) == 1
    conversation = conversations[0]
    assert conversation["channel"] == "wecom"
    assert conversation["external_user_id"] == USER_KEY
    assert conversation["id"] == events[0]["conversation_id"]

    runs = runtime.service.repository.list(
        runtime.app_id, client_id=f"wecom:{runtime.channel_id}"
    )
    assert len(runs) == 1
    assert runs[0]["release_id"] == runtime.release.id
    messages = chat.get_messages(runtime.app_id, conversation["id"])
    assert [message["role"] for message in messages] == ["user", "assistant"]
    assert [message["content"] for message in messages] == ["hello", "turn 1"]


def test_rebound_channel_rejects_previous_bot_run_revision(runtime):
    with runtime.metadata.factory.session_scope(write=True) as session:
        session.add(
            ApplicationWecomBotRevision(
                bot_id="bot-rebound",
                last_revision=1,
                updated_at=datetime.now(timezone.utc).isoformat(),
            )
        )
    rebound = runtime.service.channels.update(
        runtime.app_id,
        {"bot_id": "bot-rebound"},
        expected_config_revision=runtime.revision,
    )

    stale = _attempt(runtime, "old-bot-message", "old bot message")
    assert stale.status_code == 409
    assert runtime.calls == []
    assert runtime.service.repository.list(runtime.app_id) == []

    current = _attempt(
        runtime,
        "new-bot-message",
        "new bot message",
        revision=rebound["config_revision"],
    )
    assert current.status_code == 200
    assert _events(current)[-1]["content"] == "turn 1"


def test_same_user_key_reuses_conversation(runtime):
    first = _events(_attempt(runtime, "reuse-1", "hello"))[0]
    second = _events(_attempt(runtime, "reuse-2", "again"))[0]
    assert first["conversation_id"] == second["conversation_id"]
    chat = SQLiteChatRepository(runtime.metadata.factory)
    assert len(chat.list_conversations(runtime.app_id, "wecom")) == 1
    messages = chat.get_messages(runtime.app_id, first["conversation_id"])
    assert [message["content"] for message in messages] == [
        "hello",
        "turn 1",
        "again",
        "turn 3",
    ]


def test_completed_replay_and_fingerprint_conflict(runtime):
    body = _attempt(runtime, "replay", "hello")
    assert body.status_code == 200
    replay = _attempt(runtime, "replay", "hello")
    assert replay.status_code == 204
    assert replay.headers["Cache-Control"] == "no-store"
    conflict = _attempt(runtime, "replay", "different")
    assert conflict.status_code == 409
    assert len(runtime.service.repository.list(runtime.app_id)) == 1


def test_running_and_failed_duplicates_return_conflicts(runtime, monkeypatch):
    started = threading.Event()
    gate = threading.Event()

    def blocking_events(message, thread_id, **kwargs):
        started.set()
        gate.wait(5)
        yield {"type": "done", "data": {"answer": "released"}}

    monkeypatch.setattr(
        runtime.publication.applications,
        "release_runtime",
        lambda *args: SimpleNamespace(iter_events=blocking_events),
    )
    user_key = "wc_" + "b" * 32
    prepared = runtime.service.prepare_channel(
        runtime.channel_id,
        runtime.revision,
        user_key,
        "blocked",
        "blocked-key",
        None,
        time.monotonic(),
    )
    assert started.wait(5)
    with pytest.raises(ApiError) as busy:
        runtime.service.prepare_channel(
            runtime.channel_id,
            runtime.revision,
            user_key,
            "blocked",
            "blocked-key",
            None,
            time.monotonic(),
        )
    assert busy.value.status_code == 409
    with pytest.raises(ApiError) as mismatch:
        runtime.service.prepare_channel(
            runtime.channel_id,
            runtime.revision,
            user_key,
            "other",
            "blocked-key",
            None,
            time.monotonic(),
        )
    assert mismatch.value.status_code == 409
    gate.set()
    assert asyncio.run(runtime.service.wait(prepared)).answer == "released"

    def failing_events(message, thread_id, **kwargs):
        raise RuntimeError("runtime exploded")
        yield

    monkeypatch.setattr(
        runtime.publication.applications,
        "release_runtime",
        lambda *args: SimpleNamespace(iter_events=failing_events),
    )
    failed = runtime.service.prepare_channel(
        runtime.channel_id,
        runtime.revision,
        "wc_" + "c" * 32,
        "fail",
        "failed-key",
        None,
        time.monotonic(),
    )
    with pytest.raises(ApiError) as failure:
        asyncio.run(runtime.service.wait(failed))
    assert failure.value.status_code == 502
    with pytest.raises(ApiError) as terminal:
        runtime.service.prepare_channel(
            runtime.channel_id,
            runtime.revision,
            "wc_" + "c" * 32,
            "fail",
            "failed-key",
            None,
            time.monotonic(),
        )
    assert terminal.value.status_code == 502


def test_channel_state_gates_runs(runtime):
    repository = WecomChannelRepository(runtime.metadata.factory)
    with pytest.raises(ApiError) as stale:
        runtime.service.prepare_channel(
            runtime.channel_id,
            runtime.revision + 1,
            USER_KEY,
            "hello",
            "stale",
            None,
            time.monotonic(),
        )
    assert stale.value.status_code == 409

    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    expired = repository.update(
        runtime.app_id,
        {"expires_at": past},
        expected_config_revision=runtime.revision,
    )
    with pytest.raises(ApiError) as unavailable:
        runtime.service.prepare_channel(
            runtime.channel_id,
            expired["config_revision"],
            USER_KEY,
            "hello",
            "expired",
            None,
            time.monotonic(),
        )
    assert unavailable.value.status_code == 403

    enabled = repository.update(
        runtime.app_id,
        {"expires_at": None},
        expected_config_revision=expired["config_revision"],
    )
    disabled = repository.set_enabled(
        runtime.app_id, False, expected_deployment_version=enabled["deployment_version"]
    )
    with pytest.raises(ApiError) as stopped:
        runtime.service.prepare_channel(
            runtime.channel_id,
            disabled["config_revision"],
            USER_KEY,
            "hello",
            "disabled",
            None,
            time.monotonic(),
        )
    assert stopped.value.status_code == 403

    with pytest.raises(ApiError) as missing:
        runtime.service.prepare_channel(
            "missing",
            1,
            USER_KEY,
            "hello",
            "missing",
            None,
            time.monotonic(),
        )
    assert missing.value.status_code == 404


def test_channel_validation_limits(runtime):
    with pytest.raises(ApiError) as blank:
        runtime.service.prepare_channel(
            runtime.channel_id,
            runtime.revision,
            USER_KEY,
            "   ",
            "blank",
            None,
            time.monotonic(),
        )
    assert blank.value.status_code == 422
    with pytest.raises(ApiError) as oversized:
        runtime.service.prepare_channel(
            runtime.channel_id,
            runtime.revision,
            USER_KEY,
            "x" * (64 * 1024 + 1),
            "large",
            None,
            time.monotonic(),
        )
    assert oversized.value.status_code == 413
    with pytest.raises(ApiError) as invalid_key:
        runtime.service.prepare_channel(
            runtime.channel_id,
            runtime.revision,
            "user-1",
            "hello",
            "invalid",
            None,
            time.monotonic(),
        )
    assert invalid_key.value.status_code == 422


def test_checkpoint_namespace_and_wecom_retention(runtime):
    events = _events(_attempt(runtime, "checkpoint", "hello"))
    conversation_id = events[0]["conversation_id"]
    assert runtime.service.checkpointer is not None
    assert (
        runtime.service.checkpointer.get_tuple(
            {
                "configurable": {
                    "thread_id": f"wecom:{conversation_id}",
                    "checkpoint_ns": "",
                }
            }
        )
        is not None
    )
    assert (
        runtime.service.checkpointer.get_tuple(
            {
                "configurable": {
                    "thread_id": f"api:{conversation_id}",
                    "checkpoint_ns": "",
                }
            }
        )
        is None
    )
    with runtime.metadata.factory.session_scope() as session:
        conversation = session.get(Conversation, conversation_id)
        assert conversation is not None
        expires_at = datetime.fromisoformat(conversation.expires_at)
    remaining = expires_at - datetime.now(timezone.utc)
    assert timedelta(days=1, hours=23) < remaining <= timedelta(days=2, minutes=1)


def test_api_runs_keep_api_threads_and_do_not_consume_wecom(runtime):
    from app.api_server.apis.v1.runtime_schemas import PublicChatRequest

    prepared = runtime.service.prepare(
        runtime.principal,
        PublicChatRequest(message="api turn", user_id="api-user"),
        "api-key",
    )
    result = asyncio.run(runtime.service.wait(prepared))
    assert result.answer == "turn 1"
    assert runtime.service.checkpointer.get_tuple(
        {
            "configurable": {
                "thread_id": f"api:{result.conversation_id}",
                "checkpoint_ns": "",
            }
        }
    )
    rates = dict(runtime.service._rates)
    events = _events(_attempt(runtime, "independent", "wecom turn"))
    assert events[-1]["content"] == "turn 1"
    assert runtime.service.checkpointer.get_tuple(
        {
            "configurable": {
                "thread_id": f"wecom:{events[0]['conversation_id']}",
                "checkpoint_ns": "",
            }
        }
    )
    assert set(runtime.service._rates) == set(rates) == {runtime.client_id}


def test_wecom_global_concurrency_limit(runtime, monkeypatch):
    settings = ApiServerSettings(
        data_dir=runtime.settings.data_dir,
        internal_token=TOKEN,
        wecom_max_concurrent=1,
    )
    service = ApplicationInvocationService(
        runtime.publication,
        runtime.publication.applications,
        runtime.metadata.factory,
        settings,
    )
    started = threading.Event()
    gate = threading.Event()

    def blocking_events(message, thread_id, **kwargs):
        started.set()
        gate.wait(5)
        yield {"type": "done", "data": {"answer": "released"}}

    monkeypatch.setattr(
        runtime.publication.applications,
        "release_runtime",
        lambda *args: SimpleNamespace(iter_events=blocking_events),
    )
    try:
        prepared = service.prepare_channel(
            runtime.channel_id,
            runtime.revision,
            USER_KEY,
            "first",
            "limit-1",
            None,
            time.monotonic(),
        )
        assert started.wait(5)
        with pytest.raises(ApiError) as limited:
            service.prepare_channel(
                runtime.channel_id,
                runtime.revision,
                "wc_" + "d" * 32,
                "second",
                "limit-2",
                None,
                time.monotonic(),
            )
        assert limited.value.status_code == 429
        gate.set()
        assert asyncio.run(service.wait(prepared)).answer == "released"
    finally:
        gate.set()
        service.close()


def test_closing_channel_stream_cancels_run(runtime, monkeypatch):
    started = threading.Event()
    gate = threading.Event()

    def blocking_events(message, thread_id, **kwargs):
        started.set()
        gate.wait(5)
        yield {"type": "done", "data": {"answer": "late"}}

    monkeypatch.setattr(
        runtime.publication.applications,
        "release_runtime",
        lambda *args: SimpleNamespace(iter_events=blocking_events),
    )
    prepared = runtime.service.prepare_channel(
        runtime.channel_id,
        runtime.revision,
        USER_KEY,
        "cancel me",
        "cancel-key",
        None,
        time.monotonic(),
    )
    assert started.wait(5)

    async def consume_and_close():
        stream = runtime.service.stream_channel(prepared)
        first = await stream.__anext__()
        assert first["type"] == "start"
        await stream.aclose()

    asyncio.run(consume_and_close())
    assert prepared.cancellation is not None and prepared.cancellation.is_set()
    gate.set()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        row = runtime.service.repository.get(prepared.row["id"])
        if row is not None and row["status"] == "cancelled":
            break
        time.sleep(0.05)
    row = runtime.service.repository.get(prepared.row["id"])
    assert row is not None and row["status"] == "cancelled"
