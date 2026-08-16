import asyncio
import threading
from datetime import datetime, timezone
from fastapi.testclient import TestClient

from app.api_server.config import ApiServerSettings
from app.api_server.database import SQLiteConnectionFactory, initialize_database
from app.api_server.main import create_app
from app.api_server.repositories import SQLiteChatRepository, SQLiteMetadataRepository
from app.api_server.services.container import build_services
from app.api_server.services.chat_service import ChatService


def _seed_application(factory: SQLiteConnectionFactory) -> None:
    metadata = SQLiteMetadataRepository(factory)
    now = datetime.now(timezone.utc).isoformat()
    metadata.put(
        "knowledge_bases",
        "kb-1",
        {
            "id": "kb-1",
            "name": "测试知识库",
            "workspace_relpath": "kb-1",
            "created_at": now,
            "updated_at": now,
        },
    )
    metadata.put(
        "applications",
        "app-1",
        {
            "id": "app-1",
            "name": "测试应用",
            "knowledge_base_id": "kb-1",
            "created_at": now,
            "updated_at": now,
        },
    )


def test_chat_repository_persists_actual_retrieved_window(tmp_path):
    factory = SQLiteConnectionFactory(tmp_path / "chat.sqlite3")
    initialize_database(factory)
    _seed_application(factory)
    repository = SQLiteChatRepository(factory)
    timestamp = datetime.now(timezone.utc)

    repository.begin_turn(
        application_id="app-1",
        conversation_id="conversation-1",
        user_message_id="user-1",
        assistant_message_id="assistant-1",
        user_content="问题",
        now=timestamp,
    )
    retrieved_window = "x" * 4_000
    repository.complete_turn(
        application_id="app-1",
        conversation_id="conversation-1",
        assistant_message_id="assistant-1",
        answer="回答",
        route="retrieval",
        snippets=[
            {
                "citation_id": 1,
                "doc_id": "doc-1",
                "node_id": "node-1",
                "line_spec": "12",
                "line_num": 12,
                "title": "章节",
                "text": retrieved_window,
                "char_offset": 0,
                "char_limit": 4_000,
                "total_chars": 8_000,
                "text_truncated": True,
            }
        ],
        now=timestamp,
    )

    conversations = repository.list_conversations("app-1")
    assert conversations[0]["message_count"] == 2

    messages = repository.get_messages("app-1", "conversation-1")
    assert messages is not None
    assert [item["role"] for item in messages] == ["user", "assistant"]
    source = messages[1]["sources"][0]
    assert source["citation_id"] == 1
    assert source["text"] == retrieved_window
    assert len(source["text"]) == 4_000
    assert source["char_limit"] == 4_000
    assert source["text_truncated"] is True

    source_detail = repository.get_source(
        "app-1", "conversation-1", "assistant-1", source["id"]
    )
    assert source_detail is not None
    assert source_detail["text"] == retrieved_window


def test_chat_repository_persists_turn_diagnostics(tmp_path):
    factory = SQLiteConnectionFactory(tmp_path / "chat.sqlite3")
    initialize_database(factory)
    _seed_application(factory)
    repository = SQLiteChatRepository(factory)
    timestamp = datetime.now(timezone.utc)

    repository.begin_turn(
        application_id="app-1",
        conversation_id="conversation-1",
        user_message_id="user-1",
        assistant_message_id="assistant-1",
        user_content="问题",
        now=timestamp,
    )
    repository.complete_turn(
        application_id="app-1",
        conversation_id="conversation-1",
        assistant_message_id="assistant-1",
        answer="回答",
        route="retrieval",
        snippets=[],
        tool_calls=[
            {
                "name": "search_document_nodes",
                "args": {"query": "营收"},
                "elapsed_ms": 120,
            },
            {
                "name": "get_line_content",
                "args": {"doc_id": "doc-1", "line_spec": "5-7"},
                "elapsed_ms": 45,
            },
        ],
        usage={
            "input_tokens": 100,
            "output_tokens": 50,
            "total_tokens": 150,
            "cached_tokens": 80,
        },
        ttft_ms=4321,
        now=timestamp,
    )

    messages = repository.get_messages("app-1", "conversation-1")
    assert messages is not None
    assistant = messages[1]
    assert assistant["tool_calls"] == [
        {"name": "search_document_nodes", "args": {"query": "营收"}, "elapsed_ms": 120},
        {
            "name": "get_line_content",
            "args": {"doc_id": "doc-1", "line_spec": "5-7"},
            "elapsed_ms": 45,
        },
    ]
    assert assistant["usage"] == {
        "input_tokens": 100,
        "output_tokens": 50,
        "total_tokens": 150,
        "cached_tokens": 80,
    }
    assert assistant["ttft_ms"] == 4321


def test_chat_repository_marks_failed_assistant_message(tmp_path):
    factory = SQLiteConnectionFactory(tmp_path / "chat.sqlite3")
    initialize_database(factory)
    _seed_application(factory)
    repository = SQLiteChatRepository(factory)
    timestamp = datetime.now(timezone.utc)

    repository.begin_turn(
        application_id="app-1",
        conversation_id="conversation-1",
        user_message_id="user-1",
        assistant_message_id="assistant-1",
        user_content="问题",
        now=timestamp,
    )
    repository.fail_turn(
        application_id="app-1",
        conversation_id="conversation-1",
        assistant_message_id="assistant-1",
        error_message="模型调用失败",
        now=timestamp,
    )

    messages = repository.get_messages("app-1", "conversation-1")
    assert messages is not None
    assert messages[1]["status"] == "failed"
    assert messages[1]["error_message"] == "模型调用失败"


def test_chat_repository_does_not_regress_a_final_message(tmp_path):
    factory = SQLiteConnectionFactory(tmp_path / "chat.sqlite3")
    initialize_database(factory)
    _seed_application(factory)
    repository = SQLiteChatRepository(factory)
    timestamp = datetime.now(timezone.utc)

    repository.begin_turn(
        application_id="app-1",
        conversation_id="conversation-1",
        user_message_id="user-1",
        assistant_message_id="assistant-1",
        user_content="问题",
        now=timestamp,
    )
    repository.complete_turn(
        application_id="app-1",
        conversation_id="conversation-1",
        assistant_message_id="assistant-1",
        answer="第一次回答",
        route="retrieval",
        snippets=[{"doc_id": "doc-1", "text": "第一次片段"}],
        now=timestamp,
    )
    repository.complete_turn(
        application_id="app-1",
        conversation_id="conversation-1",
        assistant_message_id="assistant-1",
        answer="不应覆盖",
        route="direct",
        snippets=[{"doc_id": "doc-2", "text": "不应覆盖"}],
        now=timestamp,
    )
    repository.fail_turn(
        application_id="app-1",
        conversation_id="conversation-1",
        assistant_message_id="assistant-1",
        error_message="不应回退",
        now=timestamp,
    )

    messages = repository.get_messages("app-1", "conversation-1")
    assert messages is not None
    assert messages[1]["status"] == "completed"
    assert messages[1]["content"] == "第一次回答"
    assert messages[1]["sources"][0]["text"] == "第一次片段"


def test_chat_repository_recovers_pending_messages_after_restart(tmp_path):
    factory = SQLiteConnectionFactory(tmp_path / "chat.sqlite3")
    initialize_database(factory)
    _seed_application(factory)
    repository = SQLiteChatRepository(factory)
    timestamp = datetime.now(timezone.utc)

    repository.begin_turn(
        application_id="app-1",
        conversation_id="conversation-1",
        user_message_id="user-1",
        assistant_message_id="assistant-1",
        user_content="问题",
        now=timestamp,
    )

    assert repository.fail_pending_messages(timestamp) == 1
    messages = repository.get_messages("app-1", "conversation-1")
    assert messages is not None
    assert messages[1]["status"] == "failed"
    assert messages[1]["error_message"] == "服务进程重启，流式回答未完成"


class _FakeApplications:
    def require_record(self, application_id):
        assert application_id == "app-1"
        return {"id": application_id}

    def runtime(self, application_id):
        return object()


def test_chat_async_stream_uses_one_dedicated_worker(tmp_path):
    factory = SQLiteConnectionFactory(tmp_path / "chat.sqlite3")
    initialize_database(factory)
    service = ChatService(_FakeApplications(), SQLiteChatRepository(factory))
    main_thread = threading.get_ident()
    worker_threads: list[int] = []

    def events():
        for index in range(3):
            worker_threads.append(threading.get_ident())
            yield {"type": "message", "data": {"delta": str(index)}}

    async def collect():
        return [event async for event in service.iterate_stream_async(events())]

    try:
        emitted = asyncio.run(collect())
    finally:
        service.shutdown()

    assert len(emitted) == 3
    assert len(set(worker_threads)) == 1
    assert worker_threads[0] != main_thread


def test_chat_service_persists_blocking_result(monkeypatch, tmp_path):
    factory = SQLiteConnectionFactory(tmp_path / "chat.sqlite3")
    initialize_database(factory)
    _seed_application(factory)
    repository = SQLiteChatRepository(factory)
    monkeypatch.setattr(
        "app.api_server.services.chat_service.run_agent",
        lambda *_args, **_kwargs: {
            "answer": "基于片段的回答",
            "route": "retrieval",
            "retrieved_snippets": [
                {"doc_id": "doc-1", "line_num": 7, "text": "实际检索文本"}
            ],
            "status_events": [],
            "ttft_ms": 321,
        },
    )

    response = ChatService(_FakeApplications(), repository).complete(
        "app-1", "问题", "conversation-1"
    )

    assert response.message_id
    messages = repository.get_messages("app-1", "conversation-1")
    assert messages is not None
    assert messages[1]["content"] == "基于片段的回答"
    assert messages[1]["sources"][0]["text"] == "实际检索文本"
    assert messages[1]["sources"][0]["citation_id"] == 1
    assert messages[1]["ttft_ms"] == 321
    assert response.ttft_ms == 321


def test_chat_service_persists_streaming_diagnostics(monkeypatch, tmp_path):
    factory = SQLiteConnectionFactory(tmp_path / "chat.sqlite3")
    initialize_database(factory)
    _seed_application(factory)
    repository = SQLiteChatRepository(factory)

    def fake_events(*_args, **_kwargs):
        yield {"type": "message", "data": {"delta": "回"}}
        yield {
            "type": "done",
            "data": {
                "answer": "回答",
                "route": "retrieval",
                "retrieved_snippets": [],
                "tool_calls": [
                    {
                        "name": "search_document_nodes",
                        "args": {"query": "营收"},
                        "elapsed_ms": 120,
                    }
                ],
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "total_tokens": 15,
                    "cached_tokens": 0,
                },
                "ttft_ms": 2500,
                "status_events": [],
            },
        }

    monkeypatch.setattr(
        "app.api_server.services.chat_service.iter_agent_stream_events", fake_events
    )

    _conversation_id, _message_id, events = ChatService(
        _FakeApplications(), repository
    ).stream("app-1", "问题", "conversation-1")
    list(events)

    messages = repository.get_messages("app-1", "conversation-1")
    assert messages is not None
    assistant = messages[1]
    assert assistant["tool_calls"][0]["name"] == "search_document_nodes"
    assert assistant["tool_calls"][0]["elapsed_ms"] == 120
    assert assistant["usage"]["total_tokens"] == 15
    assert assistant["ttft_ms"] == 2500


def test_chat_service_forwards_request_clarification_and_persists_question(
    monkeypatch, tmp_path
):
    factory = SQLiteConnectionFactory(tmp_path / "chat.sqlite3")
    initialize_database(factory)
    _seed_application(factory)
    repository = SQLiteChatRepository(factory)
    seen: dict[str, object] = {}

    def fake_events(*_args, **kwargs):
        seen.update(kwargs)
        clarification = {
            "question": "请说明要比较的两个文档。",
            "clarification_type": "missing_info",
            "options": [],
        }
        yield {"type": "clarification", "data": clarification}
        yield {
            "type": "done",
            "data": {
                "answer": clarification["question"],
                "route": "direct",
                "retrieved_snippets": [],
                "status_events": [],
                "clarification": clarification,
            },
        }

    monkeypatch.setattr(
        "app.api_server.services.chat_service.iter_agent_stream_events", fake_events
    )
    _conversation_id, _message_id, events = ChatService(
        _FakeApplications(), repository
    ).stream("app-1", "比较一下", "conversation-1", clarification_enabled=True)
    emitted = list(events)

    assert seen["clarification_enabled"] is True
    assert emitted[0]["type"] == "clarification"
    messages = repository.get_messages("app-1", "conversation-1")
    assert messages is not None
    assert messages[1]["status"] == "completed"
    assert messages[1]["content"] == "请说明要比较的两个文档。"


def test_chat_service_marks_stream_without_done_as_failed(monkeypatch, tmp_path):
    factory = SQLiteConnectionFactory(tmp_path / "chat.sqlite3")
    initialize_database(factory)
    _seed_application(factory)
    repository = SQLiteChatRepository(factory)

    def incomplete_events(*_args, **_kwargs):
        yield {"type": "message", "data": {"delta": "半截回答"}}

    monkeypatch.setattr(
        "app.api_server.services.chat_service.iter_agent_stream_events",
        incomplete_events,
    )

    _conversation_id, _message_id, events = ChatService(
        _FakeApplications(), repository
    ).stream("app-1", "问题", "conversation-1")
    list(events)

    messages = repository.get_messages("app-1", "conversation-1")
    assert messages is not None
    assert messages[1]["status"] == "failed"
    assert messages[1]["error_message"] == "流式请求未正常结束"


def test_chat_service_marks_disconnected_stream_as_failed(monkeypatch, tmp_path):
    factory = SQLiteConnectionFactory(tmp_path / "chat.sqlite3")
    initialize_database(factory)
    _seed_application(factory)
    repository = SQLiteChatRepository(factory)

    def open_events(*_args, **_kwargs):
        yield {"type": "message", "data": {"delta": "半截回答"}}
        yield {"type": "message", "data": {"delta": "不会到达客户端"}}

    monkeypatch.setattr(
        "app.api_server.services.chat_service.iter_agent_stream_events", open_events
    )

    _conversation_id, _message_id, events = ChatService(
        _FakeApplications(), repository
    ).stream("app-1", "问题", "conversation-1")
    next(events)
    events.close()

    messages = repository.get_messages("app-1", "conversation-1")
    assert messages is not None
    assert messages[1]["status"] == "failed"
    assert messages[1]["error_message"] == "流式请求未正常结束"


def test_doc_name_round_trips_through_message_sources(monkeypatch, tmp_path):
    factory = SQLiteConnectionFactory(tmp_path / "chat.sqlite3")
    initialize_database(factory)
    _seed_application(factory)
    repository = SQLiteChatRepository(factory)
    monkeypatch.setattr(
        "app.api_server.services.chat_service.run_agent",
        lambda *_args, **_kwargs: {
            "answer": "回答",
            "route": "retrieval",
            "retrieved_snippets": [
                {
                    "doc_id": "doc-1",
                    "doc_name": "营收季报.md",
                    "line_num": 7,
                    "text": "片段",
                }
            ],
            "status_events": [],
        },
    )

    ChatService(_FakeApplications(), repository).complete(
        "app-1", "问题", "conversation-1"
    )
    messages = repository.get_messages("app-1", "conversation-1")
    assert messages is not None
    source = messages[1]["sources"][0]
    assert source["doc_name"] == "营收季报.md"
    assert source["text"] == "片段"


def test_chat_history_endpoints_return_persisted_messages_and_sources(tmp_path):
    settings = ApiServerSettings(
        data_dir=tmp_path / "api",
        workspace_root=tmp_path / "workspaces",
        # Chat persistence tests do not exercise FTS; never schedule a Milvus build.
        fts_enabled=False,
    )
    services = build_services(settings)
    _seed_application(services.chat.repository.factory)
    client = TestClient(create_app(settings, services))
    repository = services.chat.repository
    timestamp = datetime.now(timezone.utc)
    repository.begin_turn(
        application_id="app-1",
        conversation_id="conversation-1",
        user_message_id="user-1",
        assistant_message_id="assistant-1",
        user_content="问题",
        now=timestamp,
    )
    repository.complete_turn(
        application_id="app-1",
        conversation_id="conversation-1",
        assistant_message_id="assistant-1",
        answer="回答",
        route="retrieval",
        snippets=[{"doc_id": "doc-1", "text": "实际片段"}],
        tool_calls=[
            {
                "name": "get_line_content",
                "args": {"doc_id": "doc-1", "line_spec": "12"},
                "elapsed_ms": 45,
            }
        ],
        usage={
            "input_tokens": 100,
            "output_tokens": 50,
            "total_tokens": 150,
            "cached_tokens": 0,
        },
        ttft_ms=4321,
        now=timestamp,
    )

    conversations = client.get("/api/v1/apps/app-1/conversations")
    assert conversations.status_code == 200
    assert conversations.json()["data"][0]["id"] == "conversation-1"

    messages = client.get("/api/v1/apps/app-1/conversations/conversation-1/messages")
    assert messages.status_code == 200
    assistant = messages.json()["data"][1]
    source = assistant["sources"][0]
    assert source["text"] == "实际片段"
    assert assistant["tool_calls"][0]["name"] == "get_line_content"
    assert assistant["tool_calls"][0]["elapsed_ms"] == 45
    assert assistant["usage"]["total_tokens"] == 150
    assert assistant["ttft_ms"] == 4321

    source_response = client.get(
        "/api/v1/apps/app-1/conversations/conversation-1/messages/"
        f"assistant-1/sources/{source['id']}"
    )
    assert source_response.status_code == 200
    assert source_response.json()["data"]["text"] == "实际片段"

    deleted = client.delete("/api/v1/apps/app-1/conversations/conversation-1")
    assert deleted.status_code == 200
    assert client.get("/api/v1/apps/app-1/conversations").json()["data"] == []
