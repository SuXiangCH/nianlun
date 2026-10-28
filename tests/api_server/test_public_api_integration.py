from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient

from app.api_server.apis.v1.schemas import ModelProfileRequest
from app.api_server.common.errors import (
    ApiError,
    api_error_handler,
    validation_error_handler,
)
from app.api_server.config import ApiServerSettings


def test_validation_errors_never_echo_secret_input():
    app = FastAPI()
    app.add_exception_handler(RequestValidationError, validation_error_handler)

    @app.post("/profile")
    def profile(body: ModelProfileRequest):
        return {"ok": True}

    with TestClient(app) as client:
        response = client.post(
            "/profile",
            json={"kind": "llm", "name": "", "api_key": "private-test-value"},
        )
    assert response.status_code == 422
    assert "private-test-value" not in response.text
    assert all("input" not in error for error in response.json()["data"]["errors"])


def test_domain_error_preserves_retry_and_location_headers():
    app = FastAPI()
    app.add_exception_handler(ApiError, api_error_handler)

    @app.get("/run")
    def run():
        raise ApiError(
            "处理中", 409, headers={"Retry-After": "1", "Location": "/run/1"}
        )

    with TestClient(app) as client:
        response = client.get("/run")
    assert response.status_code == 409
    assert response.headers["Retry-After"] == "1"
    assert response.headers["Location"] == "/run/1"


def test_application_api_settings_are_disabled_by_default_and_validate_limits(
    monkeypatch,
):
    assert ApiServerSettings().application_api_enabled is False
    with pytest.raises(ValueError):
        ApiServerSettings(application_api_max_concurrent=0)
    with pytest.raises(ValueError):
        ApiServerSettings(application_api_timeout_seconds=0)
    monkeypatch.setenv("NIANLUN_API_APPLICATION_ENABLED", "true")
    monkeypatch.setenv("NIANLUN_API_APPLICATION_MAX_CONCURRENT", "3")
    settings = ApiServerSettings.from_env()
    assert settings.application_api_enabled is True
    assert settings.application_api_max_concurrent == 3


def test_index_read_guard_rejects_changed_revision_and_active_writer():
    from app.api_server.services.index_access import IndexAccessCoordinator

    record = {"content_version": 1, "fts_revision": 1, "fts_status": "ready"}
    coordinator = IndexAccessCoordinator(lambda _id: record)
    with coordinator.read("kb", "fts", 1) as usable:
        assert usable is True
    record["content_version"] = 2
    with coordinator.read("kb", "fts", 1) as usable:
        assert usable is False
    record["content_version"] = 1
    import threading

    entered = threading.Event()
    release = threading.Event()

    def writer():
        with coordinator.mutation("kb", "fts"):
            entered.set()
            release.wait(2)

    thread = threading.Thread(target=writer)
    thread.start()
    try:
        assert entered.wait(2)
        with coordinator.read("kb", "fts", 1) as usable:
            assert usable is False
    finally:
        release.set()
        thread.join(2)


def test_guarded_retrieval_uses_snapshot_fallback_during_mutation():
    from app.api_server.services.index_access import GuardedFullTextRetriever

    class Hits:
        def __init__(self, label):
            self.label = label

        def search(self, *args, **kwargs):
            return [{"doc_id": self.label}]

    from contextlib import contextmanager

    @contextmanager
    def unavailable():
        yield False

    retriever = GuardedFullTextRetriever(
        SimpleNamespace(read=lambda *args: unavailable()),
        "kb",
        1,
        Hits("remote"),
        Hits("snapshot"),
    )
    assert retriever.search("term")[0]["doc_id"] == "snapshot"


@pytest.mark.parametrize("effort", [None, "high"])
@pytest.mark.parametrize("sampling_version", [1, 2])
def test_release_runtime_uses_frozen_model_settings_and_current_snapshot(
    tmp_path, monkeypatch, effort, sampling_version
):
    from app.api_server.services.application_service import ApplicationService
    from nianlun.knowledgebase.factory import KnowledgeBaseFactory

    captured = {}
    knowledge = {
        "id": "kb",
        "workspace_dir": str(tmp_path),
        "content_version": 2,
        "snapshot_relpath": "snapshots/r2",
        "snapshot_manifest_sha256": "a" * 64,
    }
    fake_kb = SimpleNamespace(has_fts=True, has_vector=False)
    monkeypatch.setattr(KnowledgeBaseFactory, "create", lambda *args, **kwargs: fake_kb)

    def runtime_factory(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(kb=kwargs["knowledge_base"])

    service = ApplicationService(
        SimpleNamespace(),
        lambda _id: knowledge,
        runtime_factory=runtime_factory,
        model_config_service=SimpleNamespace(
            require_llm_profile=lambda _id: {
                "api_key": "internal",
                "reasoning_effort": "low",
            }
        ),
    )
    config = SimpleNamespace(
        knowledge_base_id="kb",
        llm_model_id="model-profile",
        model="frozen",
        base_url="https://model.test",
        context_window_tokens=2048,
        agent_version="v1",
        temperature=0.0,
        sampling_config_version=sampling_version,
        thinking=None,
        reasoning_effort=effort,
        loop_guard=SimpleNamespace(model_dump=lambda: {}),
    )
    from nianlun.agent.prompt_registry import CURRENT_PROMPT_BUNDLE

    config.tool_schema_version = CURRENT_PROMPT_BUNDLE.tool_schema_version
    config.prompt_version = CURRENT_PROMPT_BUNDLE.prompt_version
    config.v1_prompt_hash = CURRENT_PROMPT_BUNDLE.v1_prompt_hash
    config.checkpoint_schema_version = CURRENT_PROMPT_BUNDLE.checkpoint_schema_version
    record = dict(knowledge, content_version=1)
    checkpointer = object()
    service.release_runtime(config, checkpointer, record, image_capability=True)
    assert record["content_version"] == 2
    assert captured["checkpointer"] is checkpointer
    assert captured["temperature"] == (
        None if sampling_version == 1 and effort else 0.0
    )
    assert captured["legacy_temperature"] is (sampling_version == 1)
    assert captured["model"] == "frozen"
    assert captured["reasoning_effort"] == effort
    assert captured["allow_env_fallback"] is False
    assert captured["prompt_bundle"] is CURRENT_PROMPT_BUNDLE
    assert captured["image_capability"] is True


def test_console_runtime_uses_env_sampling_defaults(tmp_path, monkeypatch):
    from app.api_server.services import application_service as module
    from app.api_server.services.application_service import ApplicationService

    monkeypatch.setattr(module, "get_openai_temperature", lambda: 0.42, raising=False)
    monkeypatch.setattr(module, "get_enable_thinking", lambda: True, raising=False)

    knowledge = {
        "id": "kb",
        "workspace_dir": str(tmp_path),
        "content_version": 2,
        "snapshot_relpath": "snapshots/r2",
        "snapshot_manifest_sha256": "a" * 64,
    }
    item = {
        "id": "app-1",
        "name": "App",
        "description": "",
        "knowledge_base_id": "kb",
        "model": "console-model",
        "provider": "default",
        "search_mode": "fts",
        "config_version": 1,
        "agent_version": "v0",
        "created_at": "now",
        "updated_at": "now",
        "clarification_enabled": True,
    }
    captured = {}

    def runtime_factory(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(kb=SimpleNamespace(has_vector=False))

    service = ApplicationService(
        SimpleNamespace(get=lambda _table, _id: dict(item)),
        lambda _id: dict(knowledge),
        runtime_factory=runtime_factory,
    )
    service.runtime("app-1")
    assert captured["temperature"] == 0.42
    assert captured["enable_thinking"] is True
    assert captured["allow_env_fallback"] is False


def test_v8_migration_adds_console_defaults_and_is_repeatable(tmp_path):
    import sqlite3

    from app.api_server.database import SQLiteConnectionFactory, initialize_database
    from app.api_server.database.models import Base
    from app.api_server.repositories import SQLiteMetadataRepository

    factory = SQLiteConnectionFactory(tmp_path / "legacy.sqlite3")
    migrated_tables = {
        "applications",
        "conversations",
        "messages",
        "application_runs",
        "application_releases",
        "application_deployments",
        "application_api_clients",
        "application_api_keys",
    }
    Base.metadata.create_all(
        factory.engine,
        tables=[
            table
            for table in Base.metadata.sorted_tables
            if table.name not in migrated_tables
        ],
    )
    metadata = SQLiteMetadataRepository(factory)
    metadata.put(
        "knowledge_bases",
        "kb",
        {
            "id": "kb",
            "name": "KB",
            "workspace_relpath": "kb",
            "created_at": "now",
            "updated_at": "now",
        },
    )
    connection = factory.connect()
    try:
        connection.executescript("""
            CREATE TABLE applications (
                id VARCHAR PRIMARY KEY NOT NULL, name VARCHAR NOT NULL,
                description TEXT NOT NULL, knowledge_base_id VARCHAR NOT NULL,
                model VARCHAR, provider VARCHAR NOT NULL, search_mode VARCHAR NOT NULL,
                config_version INTEGER NOT NULL, agent_version VARCHAR(8) NOT NULL,
                created_at VARCHAR(64) NOT NULL, updated_at VARCHAR(64) NOT NULL,
                CHECK(length(trim(name)) > 0), CHECK(search_mode = 'fts'),
                CHECK(agent_version IN ('v0', 'v1')),
                FOREIGN KEY(knowledge_base_id) REFERENCES knowledge_bases(id)
                    ON DELETE RESTRICT ON UPDATE CASCADE
            );
            CREATE TABLE conversations (
                id VARCHAR PRIMARY KEY NOT NULL, application_id VARCHAR NOT NULL,
                title TEXT NOT NULL, status VARCHAR NOT NULL,
                created_at VARCHAR(64) NOT NULL, updated_at VARCHAR(64) NOT NULL,
                last_message_at VARCHAR(64),
                CONSTRAINT ck_conversations_status CHECK(status IN ('active', 'archived', 'deleted')),
                FOREIGN KEY(application_id) REFERENCES applications(id) ON DELETE CASCADE
            );
            CREATE INDEX idx_conversations_application_updated
                ON conversations(application_id, updated_at DESC);
            CREATE INDEX idx_conversations_legacy_title ON conversations(title);
            CREATE TABLE messages (
                id VARCHAR PRIMARY KEY NOT NULL, conversation_id VARCHAR NOT NULL,
                seq_no INTEGER NOT NULL, role VARCHAR NOT NULL, content TEXT NOT NULL,
                status VARCHAR NOT NULL, route VARCHAR, idempotency_key VARCHAR,
                error_message TEXT, tool_calls_json TEXT NOT NULL, trace_json TEXT NOT NULL,
                usage_json TEXT, ttft_ms INTEGER,
                created_at VARCHAR(64) NOT NULL, updated_at VARCHAR(64) NOT NULL,
                UNIQUE(conversation_id, seq_no), UNIQUE(conversation_id, idempotency_key),
                CHECK(role IN ('user', 'assistant')),
                CHECK(status IN ('pending', 'completed', 'failed')),
                FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
            );
            CREATE TABLE schema_migrations (
                version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL
            );
            INSERT INTO schema_migrations VALUES (8, 'application_agent_version', 'now');
            INSERT INTO applications VALUES ('app','App','','kb',NULL,'default','fts',1,'v0','now','now');
            INSERT INTO conversations VALUES ('conv','app','Original title','active','now','now','now');
            INSERT INTO messages VALUES ('message','conv',1,'assistant','Existing answer','completed',NULL,NULL,NULL,'[]','[]',NULL,NULL,'now','now');
        """)
    finally:
        connection.close()
    initialize_database(factory)
    initialize_database(factory)
    connection = factory.connect()
    try:
        row = connection.execute(
            "SELECT channel,context_state,api_client_id FROM conversations"
        ).fetchone()
        assert tuple(row) == ("console", "ready", None)
        assert (
            connection.execute(
                "SELECT clarification_enabled FROM applications"
            ).fetchone()[0]
            == 1
        )
        assert tuple(
            connection.execute(
                "SELECT id,conversation_id,content FROM messages"
            ).fetchone()
        ) == ("message", "conv", "Existing answer")
        index_names = {
            row[1] for row in connection.execute("PRAGMA index_list(conversations)")
        }
        assert "idx_conversations_legacy_title" in index_names
        assert "idx_conversations_application_updated" in index_names
        for update in (
            "channel='api'",
            "channel='invalid'",
            "context_state='invalid'",
            "api_client_id='client'",
            "channel='api',api_client_id='client',external_user_id=' ',release_id='release'",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(f"UPDATE conversations SET {update} WHERE id='conv'")
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()
