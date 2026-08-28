"""FTSIndexService 增量编排单测（无真实 Milvus）。

fake ``build_node_fts`` 与 ``NodeFtsStore``（仅探测 ``has_collection``），验证：
- 增量仅处理脏集、置干净、推进 revision；
- force 全量（mark_all_dirty + force 构建）；
- 空脏集不构建、直接 finish；
- collection 缺失回退全量。
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from app.api_server.config import ApiServerSettings
from app.api_server.database import SQLiteConnectionFactory, initialize_database
from app.api_server.repositories import SQLiteMetadataRepository
from app.api_server.services.fts_index_service import FTSIndexService

NOW = "2026-08-12T00:00:00+00:00"


def _repository(tmp_path: Path) -> SQLiteMetadataRepository:
    factory = SQLiteConnectionFactory(tmp_path / "api.sqlite3")
    initialize_database(factory)
    return SQLiteMetadataRepository(factory)


def _seed_kb(
    repository: SQLiteMetadataRepository,
    workspace: Path,
    *,
    content_version: int = 6,
    fts_status: str = "pending",
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
            "fts_status": fts_status,
            "fts_revision": content_version
            if fts_status == "ready"
            else content_version - 1,
            "fts_target_revision": None if fts_status == "ready" else content_version,
            "fts_collection": "fts-coll",
            "fts_error": None,
            "vector_status": "disabled",
            "created_at": NOW,
            "updated_at": NOW,
        },
    )


def _seed_doc(
    repository: SQLiteMetadataRepository,
    doc_id: str,
    *,
    fts_indexed_version: int | None,
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
            "fts_indexed_version": fts_indexed_version,
            "created_at": NOW,
            "updated_at": NOW,
            "completed_at": NOW,
        }
    )


class _FakeFtsClient:
    def __init__(self, *, exists: bool, current_schema: bool = True) -> None:
        self.exists = exists
        self.current_schema = current_schema

    def has_collection(self, _name: str) -> bool:
        return self.exists


class _FakeFtsStore:
    """仅满足 FTS collection schema 探测的假 store。"""

    def __init__(
        self,
        *,
        uri=None,
        token=None,
        collection_name=None,
        knowledge_base_id=None,
        **_: Any,
    ) -> None:
        self.collection = collection_name or "fts-coll"
        self.client = _FakeFtsStore._client

    _client: _FakeFtsClient = _FakeFtsClient(exists=True)

    def has_current_schema(self, *, timeout: float | None = None) -> bool:
        assert timeout is not None and timeout > 0
        return self.client.exists and self.client.current_schema


def _make_settings(tmp_path: Path) -> ApiServerSettings:
    return ApiServerSettings(
        data_dir=tmp_path / "data",
        workspace_root=tmp_path,
        fts_collection="nianlun_fts",
        fts_enabled=True,
    )


def _run_service(
    repository: SQLiteMetadataRepository,
    workspace: Path,
    *,
    collection_exists: bool,
    collection_current_schema: bool = True,
    force: bool,
    monkeypatch,
) -> dict[str, Any]:
    captured: dict[str, Any] = {}
    build_started = threading.Event()
    release_build = threading.Event()

    def fake_build(_workspace, **kwargs: Any) -> Any:
        captured["kwargs"] = kwargs
        build_started.set()
        assert release_build.wait(timeout=5)
        return object()

    _FakeFtsStore._client = _FakeFtsClient(
        exists=collection_exists,
        current_schema=collection_current_schema,
    )
    monkeypatch.setattr(
        "app.api_server.services.fts_index_service.build_node_fts", fake_build
    )
    monkeypatch.setattr(
        "app.api_server.services.fts_index_service.NodeFtsStore", _FakeFtsStore
    )

    settings = _make_settings(workspace.parent)
    service = FTSIndexService(
        repository,
        lambda _id: {
            **(repository.get("knowledge_bases", "kb-1") or {}),
            "workspace_dir": str(workspace),
        },
        settings,
    )
    try:
        service.schedule("kb-1", force=force)
        assert build_started.wait(timeout=5)
        future = service._jobs["kb-1"]  # pyright: ignore[reportPrivateUsage]
        release_build.set()
        future.result(timeout=5)
    finally:
        release_build.set()
        service.shutdown()
    return captured


def test_incremental_processes_only_dirty_set(tmp_path: Path, monkeypatch) -> None:
    repository = _repository(tmp_path)
    workspace = tmp_path / "workspace"
    _seed_kb(repository, workspace, content_version=6)
    _seed_doc(repository, "doc-1", fts_indexed_version=5)  # 干净
    _seed_doc(repository, "doc-2", fts_indexed_version=None)  # 脏

    captured = _run_service(
        repository,
        workspace,
        collection_exists=True,
        force=False,
        monkeypatch=monkeypatch,
    )

    assert captured["kwargs"]["doc_ids"] == ["doc-2"]
    assert captured["kwargs"]["force"] is False
    item = repository.get("knowledge_bases", "kb-1")
    assert item["fts_status"] == "ready"
    assert item["fts_revision"] == 6
    # 脏文档已置干净；干净文档不受影响。
    assert repository.get_document("kb-1", "doc-2")["fts_indexed_version"] == 6
    assert repository.get_document("kb-1", "doc-1")["fts_indexed_version"] == 5


def test_force_rebuild_marks_all_dirty_and_full(tmp_path: Path, monkeypatch) -> None:
    repository = _repository(tmp_path)
    workspace = tmp_path / "workspace"
    _seed_kb(repository, workspace, content_version=6)
    _seed_doc(repository, "doc-1", fts_indexed_version=5)
    _seed_doc(repository, "doc-2", fts_indexed_version=None)

    captured = _run_service(
        repository,
        workspace,
        collection_exists=True,
        force=True,
        monkeypatch=monkeypatch,
    )

    assert captured["kwargs"]["force"] is True
    # force 后两篇都被置脏再全量，最终都置干净。
    assert repository.get_document("kb-1", "doc-1")["fts_indexed_version"] == 6
    assert repository.get_document("kb-1", "doc-2")["fts_indexed_version"] == 6
    item = repository.get("knowledge_bases", "kb-1")
    assert item["fts_status"] == "ready"
    assert item["fts_revision"] == 6


def test_empty_dirty_set_finishes_without_build(tmp_path: Path, monkeypatch) -> None:
    repository = _repository(tmp_path)
    workspace = tmp_path / "workspace"
    _seed_kb(repository, workspace, content_version=6)
    _seed_doc(repository, "doc-1", fts_indexed_version=5)  # 全干净

    built = threading.Event()

    def fake_build(*_args: Any, **_kwargs: Any) -> Any:
        built.set()
        return object()

    monkeypatch.setattr(
        "app.api_server.services.fts_index_service.build_node_fts", fake_build
    )
    _FakeFtsStore._client = _FakeFtsClient(exists=True)
    monkeypatch.setattr(
        "app.api_server.services.fts_index_service.NodeFtsStore", _FakeFtsStore
    )
    settings = _make_settings(tmp_path)
    service = FTSIndexService(
        repository,
        lambda _id: {
            **(repository.get("knowledge_bases", "kb-1") or {}),
            "workspace_dir": str(workspace),
        },
        settings,
    )
    try:
        service.schedule("kb-1")
        service._jobs["kb-1"].result(timeout=5)  # pyright: ignore[reportPrivateUsage]
    finally:
        service.shutdown()

    assert built.is_set() is False
    item = repository.get("knowledge_bases", "kb-1")
    assert item["fts_status"] == "ready"
    assert item["fts_revision"] == 6


def test_missing_collection_falls_back_to_full(tmp_path: Path, monkeypatch) -> None:
    repository = _repository(tmp_path)
    workspace = tmp_path / "workspace"
    _seed_kb(repository, workspace, content_version=6)
    _seed_doc(repository, "doc-1", fts_indexed_version=5)  # 标干净但 collection 已丢
    _seed_doc(repository, "doc-2", fts_indexed_version=None)

    captured = _run_service(
        repository,
        workspace,
        collection_exists=False,
        force=False,
        monkeypatch=monkeypatch,
    )

    # collection 缺失 -> mark_all_dirty -> 两篇都进脏集 -> force 全量。
    assert captured["kwargs"]["force"] is True
    assert sorted(captured["kwargs"]["doc_ids"]) == ["doc-1", "doc-2"]
    assert repository.get_document("kb-1", "doc-1")["fts_indexed_version"] == 6
    assert repository.get_document("kb-1", "doc-2")["fts_indexed_version"] == 6


def test_obsolete_collection_schema_falls_back_to_full(
    tmp_path: Path, monkeypatch
) -> None:
    repository = _repository(tmp_path)
    workspace = tmp_path / "workspace"
    _seed_kb(repository, workspace, content_version=6)
    _seed_doc(repository, "doc-1", fts_indexed_version=5)
    _seed_doc(repository, "doc-2", fts_indexed_version=None)

    captured = _run_service(
        repository,
        workspace,
        collection_exists=True,
        collection_current_schema=False,
        force=False,
        monkeypatch=monkeypatch,
    )

    assert captured["kwargs"]["force"] is True
    assert sorted(captured["kwargs"]["doc_ids"]) == ["doc-1", "doc-2"]


def test_recover_pending_rebuilds_ready_collection_with_obsolete_schema(
    tmp_path: Path, monkeypatch
) -> None:
    repository = _repository(tmp_path)
    workspace = tmp_path / "workspace"
    _seed_kb(repository, workspace, fts_status="ready")
    service = FTSIndexService(
        repository,
        lambda _id: {
            **(repository.get("knowledge_bases", "kb-1") or {}),
            "workspace_dir": str(workspace),
        },
        _make_settings(tmp_path),
    )
    scheduled: list[tuple[str, bool]] = []
    monkeypatch.setattr(service, "_fts_collection_is_current", lambda _name: False)
    monkeypatch.setattr(
        service,
        "schedule",
        lambda knowledge_base_id, *, force=False: scheduled.append(
            (knowledge_base_id, force)
        ),
    )
    try:
        service.recover_pending()
        recovery = service._schema_recovery  # pyright: ignore[reportPrivateUsage]
        assert recovery is not None
        recovery.result(timeout=5)
    finally:
        service.shutdown()

    assert scheduled == [("kb-1", True)]


def test_ready_collection_schema_recovery_does_not_block_startup(
    tmp_path: Path, monkeypatch
) -> None:
    repository = _repository(tmp_path)
    workspace = tmp_path / "workspace"
    _seed_kb(repository, workspace, fts_status="ready")
    service = FTSIndexService(
        repository,
        lambda _id: {
            **(repository.get("knowledge_bases", "kb-1") or {}),
            "workspace_dir": str(workspace),
        },
        _make_settings(tmp_path),
    )
    check_started = threading.Event()
    release_check = threading.Event()

    def blocking_schema_check(_collection_name: str) -> bool:
        check_started.set()
        assert release_check.wait(timeout=5)
        return True

    monkeypatch.setattr(service, "_fts_collection_is_current", blocking_schema_check)
    try:
        service.recover_pending()
        assert check_started.wait(timeout=1)
        recovery = service._schema_recovery  # pyright: ignore[reportPrivateUsage]
        assert recovery is not None and not recovery.done()
        release_check.set()
        recovery.result(timeout=5)
    finally:
        release_check.set()
        service.shutdown()


def test_ready_collection_schema_recovery_retries_transient_failure(
    tmp_path: Path, monkeypatch
) -> None:
    repository = _repository(tmp_path)
    workspace = tmp_path / "workspace"
    _seed_kb(repository, workspace, fts_status="ready")
    service = FTSIndexService(
        repository,
        lambda _id: {
            **(repository.get("knowledge_bases", "kb-1") or {}),
            "workspace_dir": str(workspace),
        },
        _make_settings(tmp_path),
    )
    checks = 0
    scheduled: list[tuple[str, bool]] = []

    def flaky_schema_check(_collection_name: str) -> bool:
        nonlocal checks
        checks += 1
        if checks == 1:
            raise ConnectionError("Milvus temporarily unavailable")
        return False

    monkeypatch.setattr(
        "app.api_server.services.fts_index_service.FTS_SCHEMA_RETRY_INITIAL_SECONDS",
        0.001,
    )
    monkeypatch.setattr(service, "_fts_collection_is_current", flaky_schema_check)
    monkeypatch.setattr(
        service,
        "schedule",
        lambda knowledge_base_id, *, force=False: scheduled.append(
            (knowledge_base_id, force)
        ),
    )
    try:
        service.recover_pending()
        recovery = service._schema_recovery  # pyright: ignore[reportPrivateUsage]
        assert recovery is not None
        recovery.result(timeout=5)
    finally:
        service.shutdown()

    assert checks == 2
    assert scheduled == [("kb-1", True)]
