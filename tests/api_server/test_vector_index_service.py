from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from app.api_server.config import ApiServerSettings
from app.api_server.database import SQLiteConnectionFactory, initialize_database
from app.api_server.repositories import SQLiteMetadataRepository
from app.api_server.services.vector_index_service import VectorIndexService


def _repository(tmp_path: Path) -> SQLiteMetadataRepository:
    factory = SQLiteConnectionFactory(tmp_path / "api.sqlite3")
    initialize_database(factory)
    return SQLiteMetadataRepository(factory)


def _seed_knowledge_base(repository: SQLiteMetadataRepository, workspace: Path) -> None:
    workspace.mkdir()
    (workspace / "_meta.json").write_text(json.dumps({}), encoding="utf-8")
    now = "2026-08-11T00:00:00+00:00"
    repository.put(
        "knowledge_bases",
        "kb-1",
        {
            "id": "kb-1",
            "name": "测试知识库",
            "workspace_relpath": "kb-1",
            "workspace_dir": str(workspace),
            "vector_model_id": "embedding-1",
            "created_at": now,
            "updated_at": now,
        },
    )


def test_vector_service_builds_isolated_collection_without_real_milvus(
    tmp_path: Path, monkeypatch
) -> None:
    repository = _repository(tmp_path)
    workspace = tmp_path / "workspace"
    _seed_knowledge_base(repository, workspace)
    started = threading.Event()
    release = threading.Event()
    captured: dict[str, object] = {}

    def fake_build(_workspace: Path, **kwargs: object) -> object:
        captured.update(kwargs)
        progress = kwargs["progress_callback"]
        assert callable(progress)
        progress("embedding", 2, 3, 42)
        started.set()
        assert release.wait(5)
        return object()

    monkeypatch.setattr(
        "app.api_server.services.vector_index_service.build_doc_vectors",
        fake_build,
    )
    settings = ApiServerSettings(
        data_dir=tmp_path / "data",
        workspace_root=tmp_path,
        vector_collection="nianlun_vectors",
    )
    def repository_lookup(_id: str) -> dict[str, object]:
        item = repository.get("knowledge_bases", "kb-1")
        assert item is not None
        return {**item, "workspace_dir": str(workspace)}
    service = VectorIndexService(
        repository,
        repository_lookup,
        lambda _profile_id: {
            "enabled": True,
            "profile_id": "embedding-1",
            "profile_updated_at": "2026-08-11T00:00:01+00:00",
            "model": "embedding-model",
            "base_url": "https://embedding.example/v1",
            "api_key": "secret",
            "dimension": 768,
        },
        settings,
    )

    try:
        service.schedule("kb-1", activate=True)
        future = service._jobs["kb-1"]  # pyright: ignore[reportPrivateUsage]
        assert started.wait(5)
        assert repository.get("knowledge_bases", "kb-1")["vector_status"] == "building"
        release.set()
        future.result(timeout=5)
    finally:
        release.set()
        service.shutdown()

    item = repository.get("knowledge_bases", "kb-1")
    assert item is not None
    assert item["vector_status"] == "ready"
    assert item["vector_revision"] == item["content_version"]
    assert item["vector_collection"].startswith("nianlun_vectors_")
    assert item["vector_progress_stage"] == "completed"
    assert item["vector_documents_total"] == 3
    assert item["vector_documents_completed"] == 3
    assert item["vector_records_processed"] == 42
    assert captured["embedding_model"] == "embedding-model"
    assert captured["embedding_dim"] == 768
    assert captured["api_key"] == "secret"
    assert captured["allow_env_fallback"] is False


def test_vector_service_persists_build_failure_without_real_milvus(
    tmp_path: Path, monkeypatch
) -> None:
    repository = _repository(tmp_path)
    workspace = tmp_path / "workspace"
    _seed_knowledge_base(repository, workspace)

    def failed_build(_workspace: Path, **_kwargs: object) -> object:
        raise RuntimeError("embedding 服务不可用")

    monkeypatch.setattr(
        "app.api_server.services.vector_index_service.build_doc_vectors",
        failed_build,
    )
    settings = ApiServerSettings(
        data_dir=tmp_path / "data",
        workspace_root=tmp_path,
        vector_collection="nianlun_vectors",
    )

    service = VectorIndexService(
        repository,
        lambda _id: {
            **(repository.get("knowledge_bases", "kb-1") or {}),
            "workspace_dir": str(workspace),
        },
        lambda _profile_id: {
            "enabled": True,
            "profile_id": "embedding-1",
            "profile_updated_at": "2026-08-11T00:00:01+00:00",
            "model": "embedding-model",
            "base_url": "https://embedding.example/v1",
            "api_key": "secret",
            "dimension": 768,
        },
        settings,
    )

    try:
        service.schedule("kb-1", activate=True)
        future = service._jobs["kb-1"]  # pyright: ignore[reportPrivateUsage]
        future.result(timeout=5)
    finally:
        service.shutdown()

    item = repository.get("knowledge_bases", "kb-1")
    assert item is not None
    assert item["vector_status"] == "failed"
    assert item["vector_error"] == "RuntimeError: embedding 服务不可用"


# ---------------------------------------------------------------------------
# Stage-2 增量编排
# ---------------------------------------------------------------------------

BUILT_NOW = "2026-08-11T00:00:00+00:00"
PROFILE_UPDATED = "2026-08-11T00:00:00+00:00"


def _seed_built_kb(
    repository: SQLiteMetadataRepository,
    workspace: Path,
    *,
    content_version: int = 6,
    vector_status: str = "pending",
    vector_model_updated_at: str = PROFILE_UPDATED,
    vector_dimension: int = 8,
) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "_meta.json").write_text(json.dumps({}), encoding="utf-8")
    repository.put(
        "knowledge_bases",
        "kb-1",
        {
            "id": "kb-1",
            "name": "测试知识库",
            "status": "ready",
            "workspace_relpath": "kb-1",
            "document_count": 2,
            "summary_enabled": True,
            "content_version": content_version,
            "fts_status": "disabled",
            "vector_status": vector_status,
            "vector_revision": content_version - 1,
            "vector_target_revision": content_version,
            "vector_collection": "vec-coll",
            "vector_error": None,
            "embedding_model_id": "embedding-1",
            "vector_model_id": "embedding-1",
            "vector_model_updated_at": vector_model_updated_at,
            "vector_dimension": vector_dimension,
            "vector_progress_stage": "completed",
            "vector_documents_total": 1,
            "vector_documents_completed": 1,
            "vector_records_processed": 1,
            "created_at": BUILT_NOW,
            "updated_at": BUILT_NOW,
        },
    )


def _seed_vector_doc(
    repository: SQLiteMetadataRepository,
    doc_id: str,
    *,
    vector_indexed_version: int | None,
) -> None:
    repository.create_document(
        {
            "id": doc_id,
            "knowledge_base_id": "kb-1",
            "original_filename": f"{doc_id}.md",
            "file_extension": ".md",
            "mime_type": "text/markdown",
            "size_bytes": 10,
            "source_relpath": f"sources/{doc_id}.md",
            "source_sha256": doc_id,
            "parser": "native_markdown",
            "status": "ready",
            "vector_indexed_version": vector_indexed_version,
            "created_at": BUILT_NOW,
            "updated_at": BUILT_NOW,
            "completed_at": BUILT_NOW,
        }
    )


class _FakeVecClient:
    def __init__(self, *, exists: bool) -> None:
        self.exists = exists

    def has_collection(self, _name: str) -> bool:
        return self.exists


class _FakeVecStore:
    """仅满足 ``_vector_collection_exists`` 探测的假 store。"""

    _client: _FakeVecClient = _FakeVecClient(exists=True)

    def __init__(self, *, uri=None, token=None, collection_name=None, dimension=None, knowledge_base_id=None, **_: object) -> None:
        self.collection = collection_name or "vec-coll"
        self.client = _FakeVecStore._client


def _embedding_config(profile_updated_at: str = PROFILE_UPDATED, dimension: int = 8) -> dict:
    return {
        "enabled": True,
        "profile_id": "embedding-1",
        "profile_updated_at": profile_updated_at,
        "model": "embedding-model",
        "base_url": "https://embedding.example/v1",
        "api_key": "secret",
        "dimension": dimension,
    }


def _make_vec_settings(tmp_path: Path) -> ApiServerSettings:
    return ApiServerSettings(
        data_dir=tmp_path / "data",
        workspace_root=tmp_path,
        vector_collection="nianlun_vectors",
    )


def _run_vector_service(
    repository: SQLiteMetadataRepository,
    workspace: Path,
    *,
    collection_exists: bool,
    force: bool,
    activate: bool = False,
    embedding_config: dict,
    monkeypatch,
) -> dict:
    captured: dict = {}

    def fake_build(_workspace, **kwargs: object) -> object:
        captured["kwargs"] = kwargs
        return object()

    monkeypatch.setattr(
        "app.api_server.services.vector_index_service.build_doc_vectors", fake_build
    )
    _FakeVecStore._client = _FakeVecClient(exists=collection_exists)
    monkeypatch.setattr(
        "app.api_server.services.vector_index_service.DocVectorStore", _FakeVecStore
    )
    settings = _make_vec_settings(workspace.parent)
    service = VectorIndexService(
        repository,
        lambda _id: {
            **(repository.get("knowledge_bases", "kb-1") or {}),
            "workspace_dir": str(workspace),
        },
        lambda _profile_id: embedding_config,
        settings,
    )
    try:
        service.schedule("kb-1", force=force, activate=activate)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            item = repository.get("knowledge_bases", "kb-1")
            if item is not None and item["vector_status"] in {"ready", "failed"}:
                break
            time.sleep(0.01)
        else:
            raise AssertionError("vector service build did not finish")
    finally:
        service.shutdown()
    return captured


def test_vector_incremental_processes_only_dirty_set(tmp_path: Path, monkeypatch) -> None:
    repository = _repository(tmp_path)
    workspace = tmp_path / "workspace"
    _seed_built_kb(repository, workspace, content_version=6)
    _seed_vector_doc(repository, "doc-1", vector_indexed_version=5)  # 干净
    _seed_vector_doc(repository, "doc-2", vector_indexed_version=None)  # 脏

    captured = _run_vector_service(
        repository, workspace,
        collection_exists=True, force=False,
        embedding_config=_embedding_config(),
        monkeypatch=monkeypatch,
    )

    assert captured["kwargs"]["doc_ids"] == ["doc-2"]
    assert captured["kwargs"]["force"] is False
    item = repository.get("knowledge_bases", "kb-1")
    assert item["vector_status"] == "ready"
    assert item["vector_revision"] == 6
    assert repository.get_document("kb-1", "doc-2")["vector_indexed_version"] == 6
    assert repository.get_document("kb-1", "doc-1")["vector_indexed_version"] == 5


def test_vector_model_change_triggers_full_rebuild(tmp_path: Path, monkeypatch) -> None:
    repository = _repository(tmp_path)
    workspace = tmp_path / "workspace"
    # 已用 embedding-1 / dim 8 / updated_at=PROFILE_UPDATED 建过索引。
    _seed_built_kb(repository, workspace, content_version=5, vector_status="ready")
    _seed_vector_doc(repository, "doc-1", vector_indexed_version=5)
    _seed_vector_doc(repository, "doc-2", vector_indexed_version=5)
    # 模型配置变更：profile_updated_at 推进（指纹不同）。
    changed_config = _embedding_config(profile_updated_at="2026-08-12T00:00:00+00:00")

    captured = _run_vector_service(
        repository, workspace,
        collection_exists=True, force=False,
        embedding_config=changed_config,
        monkeypatch=monkeypatch,
    )

    assert captured["kwargs"]["force"] is True
    # 模型变更全量后两篇都重写并置干净。
    assert repository.get_document("kb-1", "doc-1")["vector_indexed_version"] == 5
    assert repository.get_document("kb-1", "doc-2")["vector_indexed_version"] == 5
    item = repository.get("knowledge_bases", "kb-1")
    assert item["vector_status"] == "ready"
    assert item["vector_revision"] == 5


def test_vector_empty_dirty_set_finishes_without_build(tmp_path: Path, monkeypatch) -> None:
    repository = _repository(tmp_path)
    workspace = tmp_path / "workspace"
    _seed_built_kb(repository, workspace, content_version=6)
    _seed_vector_doc(repository, "doc-1", vector_indexed_version=5)  # 全干净

    built = threading.Event()

    def fake_build(*_args: object, **_kwargs: object) -> object:
        built.set()
        return object()

    monkeypatch.setattr(
        "app.api_server.services.vector_index_service.build_doc_vectors", fake_build
    )
    _FakeVecStore._client = _FakeVecClient(exists=True)
    monkeypatch.setattr(
        "app.api_server.services.vector_index_service.DocVectorStore", _FakeVecStore
    )
    settings = _make_vec_settings(tmp_path)
    service = VectorIndexService(
        repository,
        lambda _id: {
            **(repository.get("knowledge_bases", "kb-1") or {}),
            "workspace_dir": str(workspace),
        },
        lambda _profile_id: _embedding_config(),
        settings,
    )
    try:
        service.schedule("kb-1")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            item = repository.get("knowledge_bases", "kb-1")
            if item is not None and item["vector_status"] in {"ready", "failed"}:
                break
            time.sleep(0.01)
        else:
            raise AssertionError("vector service build did not finish")
    finally:
        service.shutdown()

    assert built.is_set() is False
    item = repository.get("knowledge_bases", "kb-1")
    assert item["vector_status"] == "ready"
    assert item["vector_revision"] == 6


def test_reenable_preserves_collection_without_embedding_calls(
    tmp_path: Path, monkeypatch
) -> None:
    repository = _repository(tmp_path)
    workspace = tmp_path / "workspace"
    _seed_built_kb(
        repository,
        workspace,
        content_version=6,
        vector_status="disabled",
    )
    record = repository.get("knowledge_bases", "kb-1")
    assert record is not None
    record["vector_revision"] = 6
    repository.put("knowledge_bases", "kb-1", record)
    _seed_vector_doc(repository, "doc-1", vector_indexed_version=6)

    captured = _run_vector_service(
        repository,
        workspace,
        collection_exists=True,
        force=False,
        activate=True,
        embedding_config=_embedding_config(),
        monkeypatch=monkeypatch,
    )

    assert "kwargs" not in captured
    item = repository.get("knowledge_bases", "kb-1")
    assert item is not None
    assert item["vector_status"] == "ready"
    assert item["vector_revision"] == 6
    assert item["vector_collection"] == "vec-coll"


def test_vector_missing_collection_falls_back_to_full_incremental(
    tmp_path: Path, monkeypatch
) -> None:
    """collection 被外部 drop 但模型未变：建空表后全量写入（增量路径，无蓝绿）。"""
    repository = _repository(tmp_path)
    workspace = tmp_path / "workspace"
    _seed_built_kb(repository, workspace, content_version=6)
    _seed_vector_doc(repository, "doc-1", vector_indexed_version=5)  # 标干净但 collection 已丢
    _seed_vector_doc(repository, "doc-2", vector_indexed_version=None)

    captured = _run_vector_service(
        repository, workspace,
        collection_exists=False, force=False,
        embedding_config=_embedding_config(),
        monkeypatch=monkeypatch,
    )

    # collection 缺失 -> mark_all_dirty -> 两篇都进脏集 -> 增量写入（force=False）。
    assert captured["kwargs"]["force"] is False
    assert sorted(captured["kwargs"]["doc_ids"]) == ["doc-1", "doc-2"]
    assert repository.get_document("kb-1", "doc-1")["vector_indexed_version"] == 6
    assert repository.get_document("kb-1", "doc-2")["vector_indexed_version"] == 6
