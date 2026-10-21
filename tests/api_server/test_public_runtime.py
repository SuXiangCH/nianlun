from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from tests.api_server.test_application_publishing import publish
from tests.api_server.test_application_publishing import publishing as publishing


@pytest.fixture
def runtime(publishing, tmp_path, monkeypatch):
    from types import SimpleNamespace
    from langchain_core.messages import AIMessage, HumanMessage
    from langgraph.graph import END, START, MessagesState, StateGraph
    from app.api_server.apis.v1.publishing_schemas import (
        ApiClientCreateRequest,
        ApiKeyCreateRequest,
    )
    from app.api_server.config import ApiServerSettings
    from app.api_server.services.application_invocation_service import (
        ApplicationInvocationService,
    )

    publication, app_id, metadata = publishing
    publish(publication, app_id)
    client = publication.create_client(app_id, ApiClientCreateRequest(name="Caller"))
    token = "nl_app_" + "e" * 32 + "." + "s" * 43
    publication.register_key(
        app_id, client.id, ApiKeyCreateRequest(name="Key", api_key=token)
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
    settings = ApiServerSettings(data_dir=tmp_path, application_api_enabled=True)
    service = ApplicationInvocationService(
        publication, publication.applications, metadata.factory, settings
    )
    principal = service.authenticate(app_id, f"Bearer {token}")
    context = SimpleNamespace(
        service=service,
        principal=principal,
        publication=publication,
        app_id=app_id,
        calls=calls,
        token=token,
        settings=settings,
        metadata=metadata,
    )
    yield context
    context.service.close()


def test_public_response_hides_internal_clarification_fields(runtime, monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from app.api_server.apis.v1.runtime_schemas import PublicChatRequest

    raw_clarification = {
        "question": "哪个季度？",
        "clarification_type": "single_choice",
        "context": "需要明确统计范围",
        "options": ["Q1", "Q2"],
        "status": "waiting",
        "tool_call_id": "call-private",
        "clarification_id": "clar-1",
        "fingerprint": "sha-private",
        "duplicate": False,
    }

    def events(message, **kwargs):
        yield {
            "type": "done",
            "data": {"answer": "请确认季度", "clarification": raw_clarification},
        }

    monkeypatch.setattr(
        runtime.publication.applications,
        "release_runtime",
        lambda *args: SimpleNamespace(iter_events=events),
    )
    prepared = runtime.service.prepare(
        runtime.principal, PublicChatRequest(message="hi", user_id="clar"), "clar-key"
    )
    result = asyncio.run(runtime.service.wait(prepared))
    dumped = result.model_dump(mode="json")["clarification"]
    assert dumped == {
        "clarification_id": "clar-1",
        "clarification_type": "single_choice",
        "status": "waiting",
        "question": "哪个季度？",
        "context": "需要明确统计范围",
        "options": ["Q1", "Q2"],
        "duplicate": False,
    }
    assert "tool_call_id" not in dumped
    assert "fingerprint" not in dumped


def test_submit_failure_releases_concurrency_slot(runtime):
    from app.api_server.apis.v1.runtime_schemas import PublicChatRequest

    service = runtime.service
    service._executor.shutdown()
    with pytest.raises(RuntimeError):
        service.prepare(
            runtime.principal,
            PublicChatRequest(message="x", user_id="submit-fail"),
            "submit-fail-key",
        )
    assert service._active == {}
    # A request that never started must not consume the caller's minute budget.
    assert service._rates == {}


def test_stateless_openai_calls_do_not_persist_checkpoint_threads(runtime, monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from app.api_server.apis.v1.runtime_schemas import PublicChatRequest

    original = runtime.publication.applications.release_runtime

    def bridging(config, checkpointer, record):
        inner = original(config, checkpointer, record)
        return SimpleNamespace(
            iter_events=inner.iter_events,
            iter_public_events=lambda messages, **kwargs: inner.iter_events(
                messages[-1]["content"], **kwargs
            ),
        )

    monkeypatch.setattr(runtime.publication.applications, "release_runtime", bridging)
    service = runtime.service
    threads = []
    for index in range(2):
        prepared = service.prepare(
            runtime.principal,
            PublicChatRequest(message=f"turn-{index}", user_id=f"stateless-{index}"),
            f"stateless-key-{index}",
            input_messages=[{"role": "user", "content": f"turn-{index}"}],
        )
        result = asyncio.run(service.wait(prepared))
        threads.append(
            {
                "configurable": {
                    "thread_id": f"api:{result.conversation_id}",
                    "checkpoint_ns": "",
                }
            }
        )

    assert len(service.list(runtime.app_id)["items"]) == 2
    for thread in threads:
        assert service.checkpointer.get_tuple(thread) is None


def test_atomic_completion_replay_scope_and_persistent_checkpoint(runtime):
    import asyncio
    from app.api_server.apis.v1.runtime_schemas import PublicChatRequest
    from app.api_server.common.errors import ApiError
    from app.api_server.repositories import SQLiteChatRepository
    from app.api_server.services.application_invocation_service import (
        ApplicationInvocationService,
    )

    service = runtime.service
    request = PublicChatRequest(message="first", user_id="u")
    prepared = service.prepare(runtime.principal, request, "key")
    first = asyncio.run(service.wait(prepared))
    assert first.answer == "turn 1"
    assert service.prepare(runtime.principal, request, "key").replay == first
    assert len(runtime.calls) == 1
    assert "private/path" not in first.model_dump_json()
    messages = SQLiteChatRepository(runtime.metadata.factory).get_messages(
        runtime.app_id, first.conversation_id
    )
    assert [item["role"] for item in messages] == ["user", "assistant"]
    assert [item["content"] for item in messages] == ["first", "turn 1"]
    with pytest.raises(ApiError) as error:
        service.prepare(
            runtime.principal, PublicChatRequest(message="changed", user_id="u"), "key"
        )
    assert error.value.status_code == 409
    with pytest.raises(ApiError) as error:
        service.get(runtime.principal, first.run_id, "other-user")
    assert error.value.status_code == 404
    service.close()
    runtime.service = ApplicationInvocationService(
        runtime.publication,
        runtime.publication.applications,
        runtime.metadata.factory,
        runtime.settings,
    )
    second = asyncio.run(
        runtime.service.wait(
            runtime.service.prepare(
                runtime.principal,
                PublicChatRequest(
                    message="second", user_id="u", conversation_id=first.conversation_id
                ),
                "next",
            )
        )
    )
    assert second.answer == "turn 3"


def test_slow_runtime_build_does_not_block_other_app_admission(runtime, monkeypatch):
    import threading

    from app.api_server.apis.v1.publishing_schemas import (
        ApiClientCreateRequest,
        ApiKeyCreateRequest,
    )
    from app.api_server.apis.v1.runtime_schemas import PublicChatRequest
    from app.api_server.apis.v1.schemas import ApplicationCreateRequest

    service = runtime.service
    applications = runtime.publication.applications
    app_a = runtime.app_id

    # A second, independent application that must not be stalled by app A.
    app_b = applications.create(
        ApplicationCreateRequest(
            name="Second",
            knowledge_base_id=applications.get(app_a).knowledge_base_id,
        )
    ).id
    publish(runtime.publication, app_b)
    client_b = runtime.publication.create_client(
        app_b, ApiClientCreateRequest(name="Caller B")
    )
    token_b = "nl_app_" + "b" * 32 + "." + "s" * 43
    runtime.publication.register_key(
        app_b, client_b.id, ApiKeyCreateRequest(name="Key B", api_key=token_b)
    )
    principal_b = service.authenticate(app_b, f"Bearer {token_b}")

    original = applications.release_runtime
    started = threading.Event()
    unblock = threading.Event()

    def slow_build(config, checkpointer, record):
        if config.application_id == app_a:
            started.set()
            assert unblock.wait(5)
        return original(config, checkpointer, record)

    monkeypatch.setattr(applications, "release_runtime", slow_build)
    outcome: dict[str, object] = {}

    def admit_a():
        outcome["a"] = service.prepare(
            runtime.principal,
            PublicChatRequest(message="a", user_id="user-a"),
            "key-a",
        )

    worker_a = threading.Thread(target=admit_a)
    worker_a.start()
    assert started.wait(5)

    def admit_b():
        outcome["b"] = service.prepare(
            principal_b,
            PublicChatRequest(message="b", user_id="user-b"),
            "key-b",
        )

    worker_b = threading.Thread(target=admit_b)
    worker_b.start()
    worker_b.join(3)
    admitted_while_blocked = not worker_b.is_alive()
    unblock.set()
    worker_a.join(5)
    worker_b.join(5)

    assert admitted_while_blocked is True
    assert "b" in outcome
    assert not worker_a.is_alive()


def test_deleting_application_reclaims_api_checkpoint_threads(runtime):
    import asyncio
    from app.api_server.apis.v1.publishing_schemas import PublicationUpdateRequest
    from app.api_server.apis.v1.runtime_schemas import PublicChatRequest
    from app.api_server.common.errors import ApiError

    service = runtime.service
    prepared = service.prepare(
        runtime.principal, PublicChatRequest(message="hi", user_id="u"), "k"
    )
    result = asyncio.run(service.wait(prepared))
    thread = {
        "configurable": {
            "thread_id": f"api:{result.conversation_id}",
            "checkpoint_ns": "",
        }
    }
    assert service.checkpointer.get_tuple(thread) is not None

    # An expired conversation is deletable; its checkpoint thread must be reclaimed.
    from app.api_server.database.models.chat import Conversation

    with runtime.metadata.factory.session_scope(write=True) as session:
        session.get(
            Conversation, result.conversation_id
        ).expires_at = "2000-01-01T00:00:00+00:00"
    runtime.publication.set_enabled(
        runtime.app_id,
        PublicationUpdateRequest(enabled=False, expected_deployment_version=1),
    )
    service.delete_application(runtime.app_id)
    assert service.checkpointer.get_tuple(thread) is None

    with pytest.raises(ApiError) as error:
        service.delete_application(runtime.app_id)
    assert error.value.status_code == 404


def test_public_sse_final_answer_once_replay_and_preflight(runtime):
    from types import SimpleNamespace
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api_server.apis.v1.runtime.api import router
    from app.api_server.common.errors import ApiError, api_error_handler

    app = FastAPI()
    app.state.services = SimpleNamespace(invocations=runtime.service)
    app.add_exception_handler(ApiError, api_error_handler)
    app.include_router(router)
    path = f"/api/v1/runtime/apps/{runtime.app_id}/chat"
    headers = {"Authorization": f"Bearer {runtime.token}", "Idempotency-Key": "stream"}
    body = {"message": "hello", "user_id": "user /?", "response_mode": "streaming"}
    with TestClient(app) as client:
        first = client.post(path, json=body, headers=headers)
        assert first.status_code == 200
        assert "private candidate" not in first.text
        assert first.text.count("event: message") == 1
        assert (
            first.text.index("event: ready")
            < first.text.index("event: message")
            < first.text.index("event: done")
        )
        assert "user_id=user+%2F%3F" in first.headers["Location"]
        replay = client.post(path, json=body, headers=headers)
        assert (
            replay.text.count("event: ready") == replay.text.count("event: done") == 1
        )
        assert "event: message" not in replay.text
        wrong = client.post(path, json=body, headers={"Idempotency-Key": "other"})
        assert wrong.status_code == 401
        assert wrong.headers["content-type"].startswith("application/json")


def test_timeout_keeps_worker_quota_and_rejects_interrupted_context(
    runtime, monkeypatch
):
    import asyncio
    import threading
    from dataclasses import replace
    from app.api_server.apis.v1.runtime_schemas import PublicChatRequest
    from app.api_server.common.errors import ApiError

    service = runtime.service
    service.settings = replace(
        runtime.settings,
        application_api_timeout_seconds=0.02,
        application_api_client_concurrent=1,
    )
    started, release = threading.Event(), threading.Event()

    def blocked(*args, **kwargs):
        started.set()
        assert release.wait(3)
        yield {"type": "done", "data": {"answer": "late"}}

    monkeypatch.setattr(
        "app.api_server.services.application_invocation_service.iter_agent_stream_events",
        blocked,
    )
    prepared = service.prepare(
        runtime.principal, PublicChatRequest(message="a", user_id="u"), "slow"
    )
    assert started.wait(1)
    try:
        with pytest.raises(ApiError) as error:
            asyncio.run(service.wait(prepared))
        assert error.value.status_code == 504
        assert service.repository.get(prepared.row["id"])["status"] == "cancelling"
        with pytest.raises(ApiError) as error:
            service.prepare(
                runtime.principal, PublicChatRequest(message="b", user_id="u"), "quota"
            )
        assert error.value.status_code == 429
    finally:
        release.set()
    with pytest.raises(ApiError) as error:
        prepared.future.result(timeout=2)
    assert error.value.status_code == 504
    assert service.repository.get(prepared.row["id"])["status"] == "timed_out"
    with pytest.raises(ApiError) as error:
        service.prepare(
            runtime.principal,
            PublicChatRequest(
                message="b",
                user_id="u",
                conversation_id=prepared.row["conversation_id"],
            ),
            "continuation",
        )
    assert error.value.status_code == 409


def test_expired_checkpoint_cleanup_retries_and_preserves_messages(
    runtime, monkeypatch
):
    import asyncio
    import sqlite3
    from app.api_server.apis.v1.runtime_schemas import PublicChatRequest
    from app.api_server.database.models import Conversation, Message
    from sqlalchemy import select

    service = runtime.service
    response = asyncio.run(
        service.wait(
            service.prepare(
                runtime.principal, PublicChatRequest(message="old", user_id="u"), "old"
            )
        )
    )
    with runtime.metadata.factory.session_scope(write=True) as session:
        session.get(
            Conversation, response.conversation_id
        ).expires_at = "2000-01-01T00:00:00+00:00"
    original = service.checkpointer.delete_thread

    def fail(thread_id):
        raise sqlite3.OperationalError("locked")

    monkeypatch.setattr(service.checkpointer, "delete_thread", fail)
    service.cleanup_expired_checkpoints()
    with runtime.metadata.factory.session_scope() as session:
        assert (
            session.get(Conversation, response.conversation_id).context_state
            == "cleanup_pending"
        )
    monkeypatch.setattr(service.checkpointer, "delete_thread", original)
    service.cleanup_expired_checkpoints()
    with runtime.metadata.factory.session_scope() as session:
        assert (
            session.get(Conversation, response.conversation_id).context_state
            == "expired"
        )
        assert (
            len(
                session.scalars(
                    select(Message).where(
                        Message.conversation_id == response.conversation_id
                    )
                ).all()
            )
            == 2
        )
    assert (
        service.checkpointer.get_tuple(
            {"configurable": {"thread_id": f"api:{response.conversation_id}"}}
        )
        is None
    )


def test_conversation_pins_release_but_reads_latest_knowledge(runtime, monkeypatch):
    import asyncio
    from app.api_server.apis.v1.publishing_schemas import ApplicationUpdateRequest
    from app.api_server.apis.v1.runtime_schemas import PublicChatRequest

    service = runtime.service
    first = asyncio.run(
        service.wait(
            service.prepare(
                runtime.principal,
                PublicChatRequest(message="first", user_id="u"),
                "first",
            )
        )
    )
    runtime.publication.update(
        runtime.app_id,
        ApplicationUpdateRequest(
            expected_config_version=1, clarification_enabled=False
        ),
    )
    publication = publish(
        runtime.publication, runtime.app_id, config_version=2, deployment_version=1
    )
    original = runtime.publication.applications.knowledge_base_lookup
    monkeypatch.setattr(
        runtime.publication.applications,
        "knowledge_base_lookup",
        lambda kb: {**original(kb), "content_version": 7},
    )
    second = asyncio.run(
        service.wait(
            service.prepare(
                runtime.principal,
                PublicChatRequest(
                    message="second", user_id="u", conversation_id=first.conversation_id
                ),
                "second",
            )
        )
    )
    assert second.release_id == first.release_id
    assert second.knowledge_base_content_version == 7
    fresh = asyncio.run(
        service.wait(
            service.prepare(
                runtime.principal,
                PublicChatRequest(message="new", user_id="u"),
                "fresh",
            )
        )
    )
    assert fresh.release_id == publication.active_release.id != first.release_id


def test_rollback_keeps_existing_conversation_on_its_original_release(runtime):
    import asyncio
    from app.api_server.apis.v1.publishing_schemas import (
        ApplicationDeployRequest,
        ApplicationUpdateRequest,
    )
    from app.api_server.apis.v1.runtime_schemas import PublicChatRequest

    publication = runtime.publication
    first = publication.require_release(runtime.app_id)
    publication.update(
        runtime.app_id,
        ApplicationUpdateRequest(
            expected_config_version=1,
            clarification_enabled=False,
        ),
    )
    second = publish(
        publication, runtime.app_id, config_version=2, deployment_version=1
    ).active_release

    def turn(key, conversation_id=None):
        return asyncio.run(
            runtime.service.wait(
                runtime.service.prepare(
                    runtime.principal,
                    PublicChatRequest(
                        message=key, user_id="u", conversation_id=conversation_id
                    ),
                    key,
                )
            )
        )

    conversation = turn("before-rollback")
    assert conversation.release_id == second.id
    publication.deploy(
        runtime.app_id,
        ApplicationDeployRequest(
            release_id=first.id,
            expected_deployment_version=2,
        ),
    )
    assert (
        turn("continue-after-rollback", conversation.conversation_id).release_id
        == second.id
    )
    assert turn("new-after-rollback").release_id == first.id


def test_failure_is_atomic_sanitized_and_replayed_without_model(runtime, monkeypatch):
    import asyncio
    from app.api_server.apis.v1.runtime_schemas import PublicChatRequest
    from app.api_server.common.errors import ApiError
    from app.api_server.database.models import Conversation, Message

    service = runtime.service

    def failed(*args, **kwargs):
        raise RuntimeError("credential secret and internal endpoint")
        yield

    monkeypatch.setattr(
        "app.api_server.services.application_invocation_service.iter_agent_stream_events",
        failed,
    )
    request = PublicChatRequest(message="fail", user_id="u")
    prepared = service.prepare(runtime.principal, request, "failed")
    with pytest.raises(ApiError) as error:
        asyncio.run(service.wait(prepared))
    assert error.value.status_code == 502
    assert "secret" not in error.value.message
    result = service.get(runtime.principal, prepared.row["id"], "u")
    assert result.status == "failed" and result.error_type == "failed"
    with runtime.metadata.factory.session_scope() as session:
        assert (
            session.get(Conversation, prepared.row["conversation_id"]).context_state
            == "interrupted"
        )
        assert session.get(Message, prepared.row["message_id"]).status == "failed"
    with pytest.raises(ApiError) as error:
        service.prepare(runtime.principal, request, "failed")
    assert error.value.status_code == 502
    assert "Location" in error.value.headers


def test_duplicate_running_key_does_not_create_second_conversation(
    runtime, monkeypatch
):
    import threading
    from app.api_server.apis.v1.runtime_schemas import PublicChatRequest
    from app.api_server.common.errors import ApiError
    from app.api_server.database.models import Conversation
    from sqlalchemy import func, select

    release = threading.Event()

    def blocked(*args, **kwargs):
        assert release.wait(3)
        yield {"type": "done", "data": {"answer": "answer"}}

    monkeypatch.setattr(
        "app.api_server.services.application_invocation_service.iter_agent_stream_events",
        blocked,
    )
    request = PublicChatRequest(message="same", user_id="u")
    prepared = runtime.service.prepare(runtime.principal, request, "same")
    try:
        with pytest.raises(ApiError) as error:
            runtime.service.prepare(runtime.principal, request, "same")
        assert error.value.status_code == 409
        assert error.value.headers["Retry-After"] == "1"
        with runtime.metadata.factory.session_scope() as session:
            assert session.scalar(select(func.count(Conversation.id))) == 1
    finally:
        release.set()
        prepared.future.result(timeout=2)


def test_real_v1_runner_persists_greeting_and_continues(runtime, monkeypatch):
    import asyncio
    from tests.agent.test_retrieval_answer import (
        _RetrievalAgent,
        _AnswerModel,
        _KnowledgeBase,
    )
    from nianlun.agent.retrieval_answer.graph import build_retrieval_answer_graph
    from nianlun.agent.lead_agent.runtime import AgentRuntime
    from app.api_server.apis.v1.runtime_schemas import PublicChatRequest

    def build(config, checkpointer, record):
        graph = build_retrieval_answer_graph(
            retrieval_agent=_RetrievalAgent(),
            answer_model=_AnswerModel(),
            recursion_limit=32,
            context_window_tokens=16000,
            checkpointer=checkpointer,
        )
        return AgentRuntime(
            agent=graph,
            model="fake",
            effective_url="fake",
            tool_logging=False,
            kb=_KnowledgeBase(),
            agent_version="v1",
        )

    monkeypatch.setattr(runtime.publication.applications, "release_runtime", build)
    service = runtime.service
    first = asyncio.run(
        service.wait(
            service.prepare(
                runtime.principal,
                PublicChatRequest(message="你好", user_id="u"),
                "greeting",
            )
        )
    )
    second = asyncio.run(
        service.wait(
            service.prepare(
                runtime.principal,
                PublicChatRequest(
                    message="请检索营收。",
                    user_id="u",
                    conversation_id=first.conversation_id,
                ),
                "retrieval",
            )
        )
    )
    assert first.answer
    assert second.answer and second.retrieved_snippets
    assert service.repository.get(second.run_id)["status"] == "completed"


def test_runtime_authentication_does_not_block_event_loop(runtime, monkeypatch):
    import asyncio
    import threading
    import httpx
    from types import SimpleNamespace
    from fastapi import FastAPI
    from app.api_server.apis.v1.runtime.api import router

    app = FastAPI()
    app.state.services = SimpleNamespace(invocations=runtime.service)
    app.include_router(router)

    @app.get("/health")
    async def health():
        return {"ok": True}

    started, release = threading.Event(), threading.Event()
    original = runtime.service.authenticate

    def blocked(*args):
        started.set()
        assert release.wait(2)
        return original(*args)

    monkeypatch.setattr(runtime.service, "authenticate", blocked)

    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            pending = asyncio.create_task(
                client.post(
                    f"/api/v1/runtime/apps/{runtime.app_id}/chat",
                    json={"message": "hello", "user_id": "u"},
                    headers={
                        "Authorization": f"Bearer {runtime.token}",
                        "Idempotency-Key": "async",
                    },
                )
            )
            try:
                assert await asyncio.to_thread(started.wait, 1)
                assert (
                    await asyncio.wait_for(client.get("/health"), 0.2)
                ).status_code == 200
            finally:
                release.set()
                response = await pending
            assert response.status_code == 200

    asyncio.run(exercise())


def test_missing_checkpoint_marks_context_interrupted(runtime):
    import asyncio
    from app.api_server.apis.v1.runtime_schemas import PublicChatRequest
    from app.api_server.common.errors import ApiError
    from app.api_server.database.models import Conversation

    service = runtime.service
    first = asyncio.run(
        service.wait(
            service.prepare(
                runtime.principal,
                PublicChatRequest(message="first", user_id="u"),
                "first",
            )
        )
    )
    service.checkpointer.delete_thread(f"api:{first.conversation_id}")
    with pytest.raises(ApiError) as error:
        service.prepare(
            runtime.principal,
            PublicChatRequest(
                message="next", user_id="u", conversation_id=first.conversation_id
            ),
            "next",
        )
    assert error.value.status_code == 409
    with runtime.metadata.factory.session_scope() as session:
        assert (
            session.get(Conversation, first.conversation_id).context_state
            == "interrupted"
        )


def test_public_request_rejects_overrides_and_blank_user():
    from app.api_server.apis.v1.runtime_schemas import PublicChatRequest

    assert PublicChatRequest(message="hello", user_id="u").response_mode == "blocking"
    for values in ({"user_id": " "}, {"user_id": "u", "model": "override"}):
        with pytest.raises(ValidationError):
            PublicChatRequest(message="hello", **values)


def test_completed_replay_stops_disconnect_watcher_that_swallows_cancellation():
    import asyncio
    from types import SimpleNamespace
    from fastapi import Response
    from app.api_server.apis.v1.runtime.api import chat
    from app.api_server.apis.v1.runtime_schemas import (
        PublicChatRequest,
        PublicChatResponse,
    )

    async def exercise():
        result = PublicChatResponse(
            app_id="app",
            release_id="release",
            run_id="run",
            conversation_id="conversation",
            message_id="message",
            answer="finished",
            knowledge_base_content_version=1,
        )

        async def wait(prepared):
            return result

        prepared = SimpleNamespace(row={"id": "run"}, replay=result)
        service = SimpleNamespace(
            authenticate=lambda *args: None,
            prepare=lambda *args: prepared,
            wait=wait,
            location=lambda row: "/result",
            cancel=lambda prepared: None,
        )

        class RequestWithSuppressedCancellation:
            app = SimpleNamespace(
                state=SimpleNamespace(services=SimpleNamespace(invocations=service))
            )
            scope = {}
            swallowed = False

            async def is_disconnected(self):
                try:
                    await asyncio.sleep(10)
                except asyncio.CancelledError:
                    if self.swallowed:
                        raise
                    self.swallowed = True
                return False

        request = RequestWithSuppressedCancellation()
        task = asyncio.create_task(
            chat(
                "app",
                PublicChatRequest(message="hello", user_id="u"),
                request,
                Response(),
                authorization="Bearer test",
                idempotency_key="retry",
            )
        )
        done, _ = await asyncio.wait([task], timeout=0.15)
        completed_without_hanging = task in done
        if not completed_without_hanging:
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert completed_without_hanging
        assert task.result()["data"]["answer"] == "finished"

    asyncio.run(exercise())


def test_real_http_completed_replay_finishes(runtime):
    import socket
    import threading
    import time
    from types import SimpleNamespace
    import httpx
    import uvicorn
    from fastapi import FastAPI
    from app.api_server.apis.v1.runtime.api import router

    app = FastAPI()
    app.state.services = SimpleNamespace(invocations=runtime.service)
    app.include_router(router)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, log_level="critical", access_log=False, lifespan="off")
    )
    worker = threading.Thread(
        target=lambda: server.run(sockets=[listener]), daemon=True
    )
    worker.start()
    try:
        deadline = time.monotonic() + 3
        while not server.started and worker.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started
        headers = {
            "Authorization": f"Bearer {runtime.token}",
            "Idempotency-Key": "http-replay",
        }
        body = {"message": "hello", "user_id": "u"}
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=2) as client:
            first = client.post(
                f"/api/v1/runtime/apps/{runtime.app_id}/chat",
                json=body,
                headers=headers,
            )
            replay = client.post(
                f"/api/v1/runtime/apps/{runtime.app_id}/chat",
                json=body,
                headers=headers,
            )
        assert first.status_code == replay.status_code == 200
        assert first.json() == replay.json()
        assert len(runtime.calls) == 1
    finally:
        server.should_exit = True
        worker.join(timeout=3)
        listener.close()
    assert not worker.is_alive()


def test_repository_reads_api_conversation_across_channels(tmp_path):
    from app.api_server.database import SQLiteConnectionFactory, initialize_database
    from app.api_server.database.models import Conversation
    from app.api_server.repositories import SQLiteChatRepository
    from app.api_server.repositories import SQLiteMetadataRepository

    factory = SQLiteConnectionFactory(tmp_path / "runtime.sqlite3")
    initialize_database(factory)
    now = datetime.now(timezone.utc).isoformat()
    metadata = SQLiteMetadataRepository(factory)
    metadata.put(
        "knowledge_bases",
        "kb",
        {
            "id": "kb",
            "name": "kb",
            "workspace_relpath": "kb",
            "created_at": now,
            "updated_at": now,
        },
    )
    metadata.put(
        "applications",
        "app-1",
        {
            "id": "app-1",
            "name": "app",
            "knowledge_base_id": "kb",
            "created_at": now,
            "updated_at": now,
        },
    )
    with factory.session_scope(write=True) as session:
        session.add(
            Conversation(
                id="api-c",
                application_id="app-1",
                channel="api",
                api_client_id="client",
                external_user_id="user",
                release_id="release",
                context_state="ready",
                created_at=now,
                updated_at=now,
            )
        )
    repository = SQLiteChatRepository(factory)
    assert repository.list_conversations("app-1") == []
    api_items = repository.list_conversations("app-1", "api")
    assert [(item["id"], item["channel"]) for item in api_items] == [("api-c", "api")]
    assert api_items[0]["external_user_id"] == "user"
    assert repository.get_messages("app-1", "api-c") == []
    assert not repository.delete_conversation("app-1", "api-c")
    with pytest.raises(ValueError):
        repository.begin_turn(
            application_id="app-1",
            conversation_id="api-c",
            user_message_id="u",
            assistant_message_id="a",
            user_content="x",
            now=datetime.now(timezone.utc),
        )
