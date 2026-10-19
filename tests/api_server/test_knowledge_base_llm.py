from pathlib import Path
import time
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Column, MetaData, Table

import app.api_server.services.model_config_service as model_module
from app.api_server.apis.v1.schemas import KnowledgeBaseUpdateRequest
from app.api_server.common.errors import ApiError
from app.api_server.config import ApiServerSettings
from app.api_server.database import SQLiteConnectionFactory, initialize_database
from app.api_server.database.models import (
    Document,
    DocumentEnrichmentTask,
    KnowledgeBase,
)
from app.api_server.main import create_app
from app.api_server.repositories import SQLiteMetadataRepository
from app.api_server.services.document_pipeline_service import DocumentPipelineService


def _client(tmp_path: Path) -> TestClient:
    return TestClient(
        create_app(
            ApiServerSettings(
                data_dir=tmp_path / "api",
                workspace_root=tmp_path / "workspaces",
                fts_enabled=False,
            )
        )
    )


def _profile(client: TestClient, name: str, kind: str = "llm") -> str:
    response = client.post(
        "/api/v1/models",
        json={
            "kind": kind,
            "name": name,
            "model": name,
            "base_url": "https://model.test/v1",
            "api_key": "test-only-key",
        },
    )
    assert response.status_code == 200
    return str(response.json()["data"]["id"])


def test_knowledge_base_llm_selection_is_persisted_and_can_follow_default(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)
    _profile(client, "default")
    first = _profile(client, "first")
    second = _profile(client, "second")
    response = client.post(
        "/api/v1/knowledge-bases", json={"name": "独立模型", "llm_model_id": first}
    )
    assert response.status_code == 200
    kb = response.json()["data"]
    assert kb["llm_model_id"] == first
    path = f"/api/v1/knowledge-bases/{kb['id']}"
    assert client.get(path).json()["data"]["llm_model_id"] == first
    assert (
        client.get("/api/v1/knowledge-bases").json()["data"][0]["llm_model_id"] == first
    )
    assert (
        client.patch(path, json={"name": "改名"}).json()["data"]["llm_model_id"]
        == first
    )
    changed = client.patch(path, json={"llm_model_id": second}).json()["data"]
    assert changed["llm_model_id"] == second
    assert changed["content_version"] == kb["content_version"]
    assert changed["vector_status"] == kb["vector_status"]
    assert changed["fts_status"] == kb["fts_status"]
    assert (
        client.patch(path, json={"llm_model_id": None}).json()["data"]["llm_model_id"]
        is None
    )
    assert client.get(path).json()["data"]["llm_model_id"] is None
    created_default = client.post("/api/v1/knowledge-bases", json={"name": "跟随默认"})
    assert created_default.json()["data"]["llm_model_id"] is None


def test_knowledge_base_rejects_missing_or_non_llm_profiles(tmp_path: Path) -> None:
    client = _client(tmp_path)
    embedding_id = _profile(client, "embedding", "embedding")
    kb = client.post("/api/v1/knowledge-bases", json={"name": "模型校验"}).json()[
        "data"
    ]
    for profile_id in ("missing-profile", embedding_id):
        assert (
            client.post(
                "/api/v1/knowledge-bases",
                json={"name": "无效", "llm_model_id": profile_id},
            ).status_code
            == 422
        )
        assert (
            client.patch(
                f"/api/v1/knowledge-bases/{kb['id']}", json={"llm_model_id": profile_id}
            ).status_code
            == 422
        )
    assert (
        client.get(f"/api/v1/knowledge-bases/{kb['id']}").json()["data"]["llm_model_id"]
        is None
    )


def test_knowledge_base_llm_cannot_be_deleted_until_unbound(tmp_path: Path) -> None:
    client = _client(tmp_path)
    _profile(client, "default")
    selected = _profile(client, "selected")
    kb = client.post(
        "/api/v1/knowledge-bases", json={"name": "引用模型", "llm_model_id": selected}
    ).json()["data"]
    denied = client.delete(f"/api/v1/models/{selected}")
    assert denied.status_code == 409
    assert "知识库" in denied.json()["message"]
    assert (
        client.patch(
            f"/api/v1/knowledge-bases/{kb['id']}", json={"llm_model_id": None}
        ).status_code
        == 200
    )
    assert client.delete(f"/api/v1/models/{selected}").status_code == 200


@pytest.fixture
def queued_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        SQLiteMetadataRepository,
        "claim_enrichment_task",
        lambda *_args, **_kwargs: None,
    )
    client = _client(tmp_path)
    _profile(client, "default")
    selected = _profile(client, "selected")
    kb = client.post(
        "/api/v1/knowledge-bases", json={"name": "任务模型", "llm_model_id": selected}
    ).json()["data"]
    repository = client.app.state.services.models.repository
    repository.create_document(
        {
            "id": "doc",
            "knowledge_base_id": kb["id"],
            "original_filename": "queued.md",
            "file_extension": ".md",
            "size_bytes": 1,
            "source_relpath": "queued.md",
            "source_sha256": "hash",
            "parser": "native_markdown",
            "created_at": "2026-10-19",
            "updated_at": "2026-10-19",
        }
    )
    task = repository.create_enrichment_task(
        document_id="doc",
        pipeline_generation=1,
        input_sha256="hash",
        llm_profile_id=selected,
        llm_profile_updated_at=None,
        prompt_schema_version=1,
        options_json='{"summary_enabled":true}',
        now="2026-10-19",
    )
    assert (
        client.patch(
            f"/api/v1/knowledge-bases/{kb['id']}", json={"llm_model_id": None}
        ).status_code
        == 200
    )
    try:
        yield client, repository, selected, task
    finally:
        client.app.state.services.documents.shutdown()


@pytest.mark.parametrize("state", ["queued", "running", "failed"])
def test_queued_or_retryable_enrichment_keeps_its_model(
    queued_model, state: str
) -> None:
    client, repository, selected, task = queued_model
    with repository.factory.session_scope(write=True) as session:
        session.get(DocumentEnrichmentTask, task["id"]).state = state
    response = client.delete(f"/api/v1/models/{selected}")
    assert response.status_code == 409
    assert "文档处理任务" in response.json()["message"]

    # A successful retry supersedes the failed attempt and releases its model.
    with repository.factory.session_scope(write=True) as session:
        session.get(DocumentEnrichmentTask, task["id"]).state = "failed"
        document = session.get(Document, "doc")
        document.status = "failed"
        document.stage_state = "failed"
        document.failed_stage = "enrich"
    retried = repository.retry_failed_pipeline_task("doc", 1, "enrich", "2026-10-20")
    assert client.delete(f"/api/v1/models/{selected}").status_code == 409
    with repository.factory.session_scope(write=True) as session:
        session.get(DocumentEnrichmentTask, retried["id"]).state = "succeeded"
    assert client.delete(f"/api/v1/models/{selected}").status_code == 200


@pytest.mark.parametrize(
    "reason", ["succeeded", "partial", "canceled", "deleted", "generation"]
)
def test_inactive_enrichment_does_not_keep_its_model(queued_model, reason: str) -> None:
    client, repository, selected, task = queued_model
    with repository.factory.session_scope(write=True) as session:
        if reason == "deleted":
            session.get(Document, "doc").status = "deleted"
        elif reason == "generation":
            session.get(Document, "doc").pipeline_generation = 2
        else:
            session.get(DocumentEnrichmentTask, task["id"]).state = reason
    assert client.delete(f"/api/v1/models/{selected}").status_code == 200


def test_enrichment_cannot_queue_a_deleted_model(queued_model) -> None:
    client, repository, selected, _task = queued_model
    with repository.factory.session_scope(write=True) as session:
        session.get(Document, "doc").pipeline_generation = 2
    assert client.delete(f"/api/v1/models/{selected}").status_code == 200
    with pytest.raises(ApiError, match="模型"):
        repository.create_enrichment_task(
            document_id="doc",
            pipeline_generation=2,
            input_sha256="hash",
            llm_profile_id=selected,
            llm_profile_updated_at=None,
            prompt_schema_version=1,
            options_json='{"summary_enabled":true}',
            now="2026-10-20",
        )
    assert repository.get_latest_enrichment_task("doc", 2) is None


def test_model_deleted_during_normalization_handoff_uses_current_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    class FakeLLM:
        async def ainvoke(self, _prompt: Any, **_kwargs: Any) -> str:
            return "文档摘要"

    def build_llm(**kwargs: Any) -> FakeLLM:
        calls.append(kwargs["model"])
        return FakeLLM()

    monkeypatch.setattr(model_module, "build_chat_model", build_llm)
    create_task = SQLiteMetadataRepository.create_enrichment_task
    switched = False

    def delete_selected_before_enqueue(
        repository: SQLiteMetadataRepository, **kwargs: Any
    ) -> dict[str, Any]:
        nonlocal switched
        if not switched:
            with repository.factory.session_scope(write=True) as session:
                session.get(KnowledgeBase, kb_id).llm_model_id = None
            repository.delete_model_profile(selected_id)
            switched = True
        return create_task(repository, **kwargs)

    monkeypatch.setattr(
        SQLiteMetadataRepository,
        "create_enrichment_task",
        delete_selected_before_enqueue,
    )
    with _client(tmp_path) as client:
        default_id = _profile(client, "default")
        selected_id = _profile(client, "selected")
        kb_id = client.post(
            "/api/v1/knowledge-bases",
            json={"name": "阶段切换", "llm_model_id": selected_id},
        ).json()["data"]["id"]
        upload = client.post(
            f"/api/v1/knowledge-bases/{kb_id}/documents",
            files={"file": ("handoff.md", ("# Title\n\n" + "content " * 400).encode())},
        )
        assert upload.status_code == 200
        document_id = upload.json()["data"]["document_id"]
        for _ in range(300):
            doc = client.get(
                f"/api/v1/knowledge-bases/{kb_id}/documents/{document_id}"
            ).json()["data"]
            if doc["status"] in {"ready", "failed"}:
                break
            time.sleep(0.01)
        assert switched
        assert doc["status"] == "ready", doc
        repository = client.app.state.services.models.repository
        task = repository.get_latest_enrichment_task(document_id, 1)
        assert task["llm_profile_id"] == default_id
        assert calls == ["default"]


@pytest.mark.parametrize("follow_default", [False, True])
def test_enrichment_uses_queued_model_after_knowledge_base_and_default_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, follow_default: bool
) -> None:
    calls: list[str] = []

    class FakeLLM:
        async def ainvoke(self, _prompt: Any, **_kwargs: Any) -> str:
            return "文档摘要"

    def build_llm(**kwargs: Any) -> FakeLLM:
        calls.append(kwargs["model"])
        return FakeLLM()

    monkeypatch.setattr(model_module, "build_chat_model", build_llm)
    original_enrich = DocumentPipelineService._enrich
    queued_profiles: list[str | None] = []

    async def change_settings_then_enrich(
        service: DocumentPipelineService, task: dict[str, Any]
    ) -> None:
        queued_profiles.append(task["llm_profile_id"])
        service.models.set_default_profile(other_id)
        service.knowledge_bases.update(
            kb_id, KnowledgeBaseUpdateRequest(llm_model_id=other_id)
        )
        await original_enrich(service, task)

    monkeypatch.setattr(DocumentPipelineService, "_enrich", change_settings_then_enrich)
    with _client(tmp_path) as client:
        default_id = _profile(client, "default")
        selected_id = _profile(client, "selected")
        other_id = _profile(client, "other")
        kb_id = client.post(
            "/api/v1/knowledge-bases",
            json={
                "name": "处理模型",
                "llm_model_id": None if follow_default else selected_id,
            },
        ).json()["data"]["id"]
        upload = client.post(
            f"/api/v1/knowledge-bases/{kb_id}/documents",
            files={"file": ("model.md", ("# Title\n\n" + "content " * 400).encode())},
        )
        assert upload.status_code == 200
        document_id = upload.json()["data"]["document_id"]
        for _ in range(300):
            doc = client.get(
                f"/api/v1/knowledge-bases/{kb_id}/documents/{document_id}"
            ).json()["data"]
            if doc["status"] in {"ready", "failed"}:
                break
            time.sleep(0.01)
        assert doc["status"] == "ready", doc
        assert queued_profiles == [default_id if follow_default else selected_id]
        assert calls == ["default" if follow_default else "selected"]


def test_llm_selection_upgrade_is_repeatable_and_preserves_existing_data(
    tmp_path: Path,
) -> None:
    factory = SQLiteConnectionFactory(tmp_path / "upgrade.sqlite3")
    legacy = Table(
        "knowledge_bases",
        MetaData(),
        *[
            Column(
                column.name,
                column.type,
                primary_key=column.primary_key,
                nullable=column.nullable,
                default=column.default,
                server_default=column.server_default,
            )
            for column in KnowledgeBase.__table__.columns
            if column.name != "llm_model_id"
        ],
    )
    legacy.create(factory.engine)
    with factory.engine.begin() as connection:
        connection.execute(
            legacy.insert().values(
                id="kb-old",
                name="已有知识库",
                workspace_relpath="kb-old",
                created_at="2026-10-19",
                updated_at="2026-10-19",
            )
        )
    connection = factory.connect()
    try:
        connection.execute(
            "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, name TEXT, applied_at TEXT)"
        )
        connection.execute(
            "INSERT INTO schema_migrations VALUES (10, 'previous', CURRENT_TIMESTAMP)"
        )
        connection.commit()
    finally:
        connection.close()
    initialize_database(factory)
    initialize_database(factory)
    repository = SQLiteMetadataRepository(factory)
    kb = repository.get("knowledge_bases", "kb-old")
    assert kb["name"] == "已有知识库"
    assert kb["llm_model_id"] is None
