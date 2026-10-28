from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.api_server.database import SQLiteConnectionFactory, initialize_database
from app.api_server.database.models import Conversation
from app.api_server.repositories.sqlite_chat import SQLiteChatRepository
from app.api_server.services.chat_service import ChatService
from nianlun.agent.prompt_registry import CURRENT_PROMPT_BUNDLE, LEGACY_PROMPT_BUNDLE
from tests.api_server.test_chat_persistence import _seed_application


@pytest.fixture
def repository(tmp_path):
    factory = SQLiteConnectionFactory(tmp_path / "chat.db")
    initialize_database(factory)
    _seed_application(factory)
    yield SQLiteChatRepository(factory)
    factory.dispose()


def begin(repository, conversation="conversation", turn=1):
    return repository.begin_turn(
        application_id="app-1",
        conversation_id=conversation,
        user_message_id=f"u{turn}",
        assistant_message_id=f"a{turn}",
        user_content="Show diagram",
        now=datetime.now(timezone.utc),
    )


def test_new_console_conversation_pins_current_complete_contract(repository):
    from nianlun.agent.prompt_registry import resolve_prompt_contract_json

    contract = begin(repository)
    assert resolve_prompt_contract_json(contract) is CURRENT_PROMPT_BUNDLE
    assert begin(repository, turn=2) == contract


def test_schema14_upgrade_pins_existing_console_to_legacy_without_changing_messages(
    repository,
):
    from nianlun.agent.prompt_registry import resolve_prompt_contract_json

    begin(repository)
    connection = repository.factory.connect()
    try:
        connection.execute("ALTER TABLE conversations DROP COLUMN agent_contract_json")
        connection.commit()
    finally:
        connection.close()
    initialize_database(repository.factory)
    initialize_database(repository.factory)
    with repository.factory.session_scope() as session:
        old = session.get(Conversation, "conversation")
        assert (
            resolve_prompt_contract_json(old.agent_contract_json)
            is LEGACY_PROMPT_BUNDLE
        )
    assert len(repository.get_messages("app-1", "conversation")) == 2
    assert (
        resolve_prompt_contract_json(begin(repository, turn=2)) is LEGACY_PROMPT_BUNDLE
    )


@pytest.mark.parametrize("legacy", [False, True])
def test_chat_selects_pinned_bundle_and_channel_capability(
    repository, monkeypatch, legacy
):
    begin(repository)
    if legacy:
        with repository.factory.session_scope(write=True) as session:
            session.get(
                Conversation, "conversation"
            ).agent_contract_json = LEGACY_PROMPT_BUNDLE.contract_json()
    captured = {}

    def runtime(_id, **kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(
        "app.api_server.services.chat_service.run_agent",
        lambda *_, **__: {"answer": "Text"},
    )
    service = ChatService(
        SimpleNamespace(require_record=lambda _: {}, runtime=runtime), repository
    )
    response = service.complete("app-1", "Explain", "conversation")
    assert response.answer == "Text"
    assert captured["prompt_bundle"] is (
        LEGACY_PROMPT_BUNDLE if legacy else CURRENT_PROMPT_BUNDLE
    )
    assert captured["image_capability"] is False


def test_console_unknown_contract_is_rejected(repository):
    from app.api_server.common.errors import ApiError

    begin(repository)
    with repository.factory.session_scope(write=True) as session:
        session.get(
            Conversation, "conversation"
        ).agent_contract_json = '{"tool_schema_version": 99}'
    service = ChatService(
        SimpleNamespace(
            require_record=lambda _: {},
            runtime=lambda *_, **__: pytest.fail("invalid contract reached runtime"),
        ),
        repository,
    )
    with pytest.raises(ApiError, match="新建会话"):
        service.complete("app-1", "Explain", "conversation")


def test_console_done_preserves_unavailable_preview_count():
    import json
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api_server.apis.v1.chat.api import router

    async def events(_events):
        yield {
            "type": "done",
            "data": {"answer": "Text", "image_previews_unavailable": 3},
        }

    app = FastAPI()
    app.state.services = SimpleNamespace(
        chat=SimpleNamespace(
            stream=lambda *args, **kwargs: ("conversation", "message", None),
            iterate_stream_async=events,
        )
    )
    app.include_router(router)
    response = TestClient(app).post(
        "/api/v1/apps/app-1/chat",
        json={"message": "Explain", "response_mode": "streaming"},
    )
    assert response.status_code == 200
    payloads = [
        json.loads(line[6:])
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]
    assert payloads[-1]["image_previews_unavailable"] == 3
