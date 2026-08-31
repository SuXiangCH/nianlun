from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.api_server.database import (
    Base,
    Message,
    MessageSource,
    SQLiteConnectionFactory,
    initialize_database,
)
from app.api_server.database import migrations
from app.api_server.database.models import Application, Conversation, KnowledgeBase
from app.api_server.repositories import SQLiteChatRepository, SQLiteMetadataRepository


def _factory(tmp_path):
    factory = SQLiteConnectionFactory(tmp_path / "orm.sqlite3")
    initialize_database(factory)
    return factory


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


def test_orm_models_cover_all_business_tables():
    assert set(Base.metadata.tables) == {
        "knowledge_bases",
        "applications",
        "upload_operations",
        "conversations",
        "messages",
        "message_sources",
        "model_profiles",
        "llm_model_profiles",
        "embedding_model_profiles",
        "parser_model_profiles",
        "documents",
        "document_parse_tasks",
        "document_artifacts",
    }


def test_schema_initialization_records_current_version(tmp_path):
    factory = _factory(tmp_path)
    connection = factory.connect()
    try:
        versions = {
            int(row[0])
            for row in connection.execute("SELECT version FROM schema_migrations")
        }
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    finally:
        connection.close()
    assert migrations.SCHEMA_VERSION == 2
    assert versions == {2}
    assert tables >= set(Base.metadata.tables)

    connection = factory.connect()
    try:
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(knowledge_bases)")
        }
    finally:
        connection.close()
    assert "summary_enabled" in columns
    assert "vector_status" in columns
    assert "vector_progress_stage" in columns

    connection = factory.connect()
    try:
        message_columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(messages)")
        }
    finally:
        connection.close()
    assert "trace_json" in message_columns


def test_schema_initialization_preserves_pre_release_data(tmp_path):
    factory = _factory(tmp_path)
    repository = SQLiteMetadataRepository(factory)
    repository.put(
        "knowledge_bases",
        "kb-existing",
        {
            "id": "kb-existing",
            "name": "已有知识库",
            "workspace_relpath": "existing",
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
        },
    )
    connection = factory.connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("DELETE FROM schema_migrations")
        connection.execute(
            "INSERT INTO schema_migrations(version, name, applied_at) "
            "VALUES (18, 'pre_release_schema', CURRENT_TIMESTAMP)"
        )
        connection.execute("COMMIT")
    finally:
        connection.close()

    initialize_database(factory)

    assert repository.get("knowledge_bases", "kb-existing") is not None
    connection = factory.connect()
    try:
        versions = {
            int(row[0])
            for row in connection.execute("SELECT version FROM schema_migrations")
        }
    finally:
        connection.close()
    assert versions == {2}


def test_schema_initialization_migrates_v1_messages_without_data_loss(tmp_path):
    factory = _factory(tmp_path)
    _seed_application(factory)
    repository = SQLiteChatRepository(factory)
    now = datetime.now(timezone.utc)
    repository.begin_turn(
        application_id="app-1",
        conversation_id="conversation-v1",
        user_message_id="user-v1",
        assistant_message_id="assistant-v1",
        user_content="旧数据库问题",
        now=now,
    )
    repository.complete_turn(
        application_id="app-1",
        conversation_id="conversation-v1",
        assistant_message_id="assistant-v1",
        answer="旧数据库回答",
        route="direct",
        snippets=[],
        now=now,
    )

    connection = factory.connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("ALTER TABLE messages DROP COLUMN trace_json")
        connection.execute("DELETE FROM schema_migrations")
        connection.execute(
            "INSERT INTO schema_migrations(version, name, applied_at) "
            "VALUES (1, 'initial_schema', CURRENT_TIMESTAMP)"
        )
        connection.execute("COMMIT")
    finally:
        connection.close()

    initialize_database(factory)

    messages = repository.get_messages("app-1", "conversation-v1")
    assert messages is not None
    assert messages[1]["content"] == "旧数据库回答"
    assert messages[1]["trace"] == []
    connection = factory.connect()
    try:
        versions = {
            int(row[0])
            for row in connection.execute("SELECT version FROM schema_migrations")
        }
    finally:
        connection.close()
    assert versions == {2}


def test_knowledge_base_settings_update_preserves_concurrent_index_state(tmp_path):
    factory = _factory(tmp_path)
    repository = SQLiteMetadataRepository(factory)
    now = "2026-08-16T00:00:00+00:00"
    repository.put(
        "knowledge_bases",
        "kb-1",
        {
            "id": "kb-1",
            "name": "旧名称",
            "workspace_relpath": "kb-1",
            "document_count": 1,
            "content_version": 7,
            "fts_status": "ready",
            "fts_revision": 7,
            "created_at": now,
            "updated_at": now,
        },
    )
    with factory.session_scope(write=True) as session:
        entity = session.get(KnowledgeBase, "kb-1")
        assert entity is not None
        entity.document_count = 2
        entity.content_version = 8
        entity.fts_status = "pending"
        entity.fts_revision = 7
        entity.fts_target_revision = 8

    repository.update_knowledge_base_settings(
        "kb-1", {"name": "新名称", "updated_at": now}
    )

    item = repository.get("knowledge_bases", "kb-1")
    assert item is not None
    assert item["name"] == "新名称"
    assert item["document_count"] == 2
    assert item["content_version"] == 8
    assert item["fts_status"] == "pending"
    assert item["fts_revision"] == 7
    assert item["fts_target_revision"] == 8


def test_schema_initialization_rejects_tables_with_missing_columns(tmp_path):
    factory = _factory(tmp_path)
    connection = factory.connect()
    try:
        connection.execute("ALTER TABLE parser_model_profiles DROP COLUMN api_mode")
        connection.execute("DELETE FROM schema_migrations")
        connection.execute(
            "INSERT INTO schema_migrations(version, name, applied_at) "
            "VALUES (18, 'pre_release_schema', CURRENT_TIMESTAMP)"
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(RuntimeError, match="parser_model_profiles.api_mode"):
        initialize_database(factory)

    connection = factory.connect()
    try:
        versions = {
            int(row[0])
            for row in connection.execute("SELECT version FROM schema_migrations")
        }
    finally:
        connection.close()
    assert versions == {18}


def test_orm_metadata_round_trip_preserves_typed_values(tmp_path):
    factory = _factory(tmp_path)
    repository = SQLiteMetadataRepository(factory)
    _seed_application(factory)

    operation = repository.start_upload(
        "kb-1", "request-1", "hash-1", "doc-1", "2026-01-01T00:00:00+00:00"
    )
    assert operation["status"] == "started"
    repository.mark_upload_files_committed(
        "kb-1",
        "request-1",
        "sources/doc-1.md",
        "doc-1.json",
        "source-hash",
        "artifact-hash",
        "2026-01-01T00:00:01+00:00",
    )
    assert (
        repository.commit_upload("kb-1", "request-1", 1, "2026-01-01T00:00:02+00:00")
        == 1
    )
    stored = repository.get_upload("kb-1", "request-1")
    assert stored is not None
    assert stored["status"] == "committed"
    assert stored["artifact_relpath"] == "doc-1.json"


def test_repeated_upload_persistence_does_not_advance_content_version(tmp_path):
    factory = _factory(tmp_path)
    repository = SQLiteMetadataRepository(factory)
    _seed_application(factory)
    now = "2026-01-01T00:00:00+00:00"
    repository.create_document(
        {
            "id": "doc-1",
            "knowledge_base_id": "kb-1",
            "original_filename": "input.pdf",
            "file_extension": ".pdf",
            "mime_type": "application/pdf",
            "size_bytes": 100,
            "source_relpath": "sources/doc-1.pdf",
            "source_sha256": "source-hash",
            "parser": "mineru",
            "status": "parsing",
            "created_at": now,
            "updated_at": now,
        }
    )
    repository.start_upload("kb-1", "request-1", "request-hash", "doc-1", now)
    repository.mark_upload_files_committed(
        "kb-1",
        "request-1",
        "sources/doc-1.pdf",
        "doc-1.json",
        "source-hash",
        "artifact-hash",
        now,
    )
    assert repository.commit_upload("kb-1", "request-1", 1, now) == 1

    repository.mark_upload_files_committed(
        "kb-1",
        "request-1",
        "sources/doc-1.pdf",
        "doc-1.json",
        "source-hash",
        "artifact-hash",
        now,
    )
    assert repository.commit_upload("kb-1", "request-1", 1, now) == 1

    knowledge_base = repository.get("knowledge_bases", "kb-1")
    document = repository.get_document("kb-1", "doc-1")
    operation = repository.get_upload("kb-1", "request-1")
    assert knowledge_base is not None
    assert document is not None
    assert operation is not None
    assert knowledge_base["content_version"] == 1
    assert document["parsed_content_version"] == 1
    assert operation["status"] == "committed"


def test_model_profile_repository_manages_defaults_and_deletion(tmp_path):
    repository = SQLiteMetadataRepository(_factory(tmp_path))
    now = "2026-01-01T00:00:00+00:00"

    assert repository.list_model_profiles() == []
    first = repository.create_model_profile(
        {
            "id": "llm-1",
            "kind": "llm",
            "name": "主模型",
            "base_url": "https://llm.example/v1",
            "api_key": "secret-1",
            "model": "model-1",
        },
        now,
    )
    second = repository.create_model_profile(
        {
            "id": "llm-2",
            "kind": "llm",
            "name": "备用模型",
            "base_url": "https://backup.example/v1",
            "api_key": "secret-2",
            "model": "model-2",
        },
        now,
    )

    assert first["is_default"] is True
    assert second["is_default"] is False
    repository.set_default_model_profile("llm-2", now)
    assert repository.get_default_model_profile("llm")["id"] == "llm-2"
    assert repository.get_model_profile("llm-1")["is_default"] is False

    repository.delete_model_profile("llm-1")
    assert repository.get_model_profile("llm-1") is None
    assert [item["id"] for item in repository.list_model_profiles()] == ["llm-2"]


def test_parse_retry_restores_upload_state_and_artifact_upsert(tmp_path):
    factory = _factory(tmp_path)
    repository = SQLiteMetadataRepository(factory)
    _seed_application(factory)
    now = "2026-01-01T00:00:00+00:00"
    repository.create_document(
        {
            "id": "doc-1",
            "knowledge_base_id": "kb-1",
            "original_filename": "input.pdf",
            "file_extension": ".pdf",
            "mime_type": "application/pdf",
            "size_bytes": 100,
            "source_relpath": "sources/doc-1.pdf",
            "source_sha256": "source-hash",
            "parser": "mineru",
            "status": "failed",
            "error_code": "parse_failed",
            "error_message": "first attempt failed",
            "created_at": now,
            "updated_at": now,
        }
    )
    repository.start_upload("kb-1", "request-1", "request-hash", "doc-1", now)
    repository.mark_upload_files_committed(
        "kb-1",
        "request-1",
        "sources/doc-1.pdf",
        "doc-1.json",
        "source-hash",
        "artifact-hash",
        now,
    )
    repository.fail_upload("kb-1", "request-1", "parse failed", now)
    repository.create_parse_task(
        {
            "id": "task-1",
            "document_id": "doc-1",
            "provider": "mineru",
            "api_mode": "self_hosted",
            "attempt": 1,
            "data_id": "doc-1",
            "model_version": "vlm",
            "request_json": "{}",
            "state": "failed",
            "created_at": now,
            "updated_at": now,
        }
    )

    retry = repository.retry_parse_task(
        "doc-1", "self_hosted", "vlm", "{}", "2026-01-01T00:01:00+00:00"
    )

    assert retry["attempt"] == 2
    assert retry["state"] == "created"
    assert repository.get_document("kb-1", "doc-1")["status"] == "parsing"
    upload = repository.get_upload("kb-1", "request-1")
    assert upload["status"] == "files_committed"
    assert upload["error_message"] is None

    artifact = {
        "id": "artifact-1",
        "document_id": "doc-1",
        "kind": "result_zip",
        "relpath": "artifacts/result.zip",
        "mime_type": "application/zip",
        "size_bytes": 10,
        "sha256": "hash-1",
        "created_at": now,
    }
    created = repository.put_document_artifact(artifact)
    artifact.update(size_bytes=20, sha256="hash-2")
    updated = repository.put_document_artifact(artifact)

    assert updated["id"] == created["id"]
    assert updated["size_bytes"] == 20
    assert updated["sha256"] == "hash-2"
    assert len(repository.list_document_artifacts("doc-1")) == 1


def test_orm_transaction_rolls_back_constraint_failure(tmp_path):
    factory = _factory(tmp_path)
    repository = SQLiteMetadataRepository(factory)
    with pytest.raises(IntegrityError):
        repository.put(
            "knowledge_bases",
            "kb-invalid",
            {
                "id": "kb-invalid",
                "name": "非法状态",
                "status": "invalid",
                "created_at": "2026-01-01T00:00:00+00:00",
                "updated_at": "2026-01-01T00:00:00+00:00",
            },
        )
    assert repository.get("knowledge_bases", "kb-invalid") is None


def test_conversation_delete_cascades_messages_and_sources(tmp_path):
    factory = _factory(tmp_path)
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
        snippets=[{"doc_id": "doc-1", "text": "实际检索文本"}],
        now=timestamp,
    )

    messages = repository.get_messages("app-1", "conversation-1")
    assert messages is not None
    assert repository.delete_conversation("app-1", "conversation-1") is True
    assert repository.get_messages("app-1", "conversation-1") is None

    with factory.session_scope() as session:
        assert (
            session.scalar(
                select(Conversation).where(Conversation.id == "conversation-1")
            )
            is None
        )
        assert (
            session.scalar(select(Message).where(Message.id == "assistant-1")) is None
        )
        assert (
            session.scalar(
                select(MessageSource).where(MessageSource.message_id == "assistant-1")
            )
            is None
        )


def test_application_delete_cascades_conversations_and_messages(tmp_path):
    factory = _factory(tmp_path)
    _seed_application(factory)
    chat = SQLiteChatRepository(factory)
    timestamp = datetime.now(timezone.utc)
    chat.begin_turn(
        application_id="app-1",
        conversation_id="conversation-1",
        user_message_id="user-1",
        assistant_message_id="assistant-1",
        user_content="问题",
        now=timestamp,
    )
    chat.complete_turn(
        application_id="app-1",
        conversation_id="conversation-1",
        assistant_message_id="assistant-1",
        answer="回答",
        route="retrieval",
        snippets=[{"doc_id": "doc-1", "text": "检索内容"}],
        now=timestamp,
    )

    metadata = SQLiteMetadataRepository(factory)
    assert metadata.delete_application("app-1") is True
    with factory.session_scope() as session:
        assert (
            session.scalar(select(Application).where(Application.id == "app-1")) is None
        )
        assert (
            session.scalar(
                select(Conversation).where(Conversation.id == "conversation-1")
            )
            is None
        )
        assert (
            session.scalar(select(Message).where(Message.id == "assistant-1")) is None
        )
        assert (
            session.scalar(
                select(MessageSource).where(MessageSource.message_id == "assistant-1")
            )
            is None
        )
