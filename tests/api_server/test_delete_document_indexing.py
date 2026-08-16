"""Stage-0 delete-path unit tests for ``DocumentIngestionService``.

These cover the surgical-delete orchestration without a real Milvus: the Milvus
stores are replaced by fakes and the rebuild schedules by spies. The real
SQLite repository exercises the version-gated ``advance_*_revision`` methods.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import app.api_server.services.document_ingestion_service as ingestion_module
from app.api_server.config import ApiServerSettings
from app.api_server.database import SQLiteConnectionFactory, initialize_database
from app.api_server.database.models import KnowledgeBase
from app.api_server.repositories import SQLiteMetadataRepository
from app.api_server.services.document_ingestion_service import DocumentIngestionService

FTS_COLLECTION = "fts_coll_1"
VECTOR_COLLECTION = "vec_coll_1"
KB_ID = "kb-1"
DOC_ID = "doc-1"
NOW = "2026-08-12T00:00:00+00:00"


def _repository(tmp_path: Path) -> SQLiteMetadataRepository:
    factory = SQLiteConnectionFactory(tmp_path / "api.sqlite3")
    initialize_database(factory)
    return SQLiteMetadataRepository(factory)


def _seed_ready_knowledge_base(
    repository: SQLiteMetadataRepository,
    *,
    fts_status: str = "ready",
    vector_status: str = "ready",
) -> None:
    repository.put(
        "knowledge_bases",
        KB_ID,
        {
            "id": KB_ID,
            "name": "测试知识库",
            "status": "ready",
            "workspace_relpath": KB_ID,
            "document_count": 1,
            "summary_enabled": True,
            "content_version": 5,
            "fts_status": fts_status,
            "fts_revision": 5 if fts_status == "ready" else None,
            "fts_target_revision": 5 if fts_status == "ready" else None,
            "fts_collection": FTS_COLLECTION,
            "fts_error": None,
            "vector_status": vector_status,
            "vector_revision": 5 if vector_status == "ready" else None,
            "vector_target_revision": 5 if vector_status == "ready" else None,
            "vector_collection": VECTOR_COLLECTION,
            "vector_error": None,
            "vector_model_id": "emb-1",
            "vector_model_updated_at": "2026-08-11T00:00:00+00:00",
            "vector_dimension": 8,
            "vector_progress_stage": "completed",
            "vector_documents_total": 1,
            "vector_documents_completed": 1,
            "vector_records_processed": 3,
            "created_at": NOW,
            "updated_at": NOW,
        },
    )


def _seed_document(repository: SQLiteMetadataRepository) -> None:
    repository.create_document(
        {
            "id": DOC_ID,
            "knowledge_base_id": KB_ID,
            "original_filename": "notes.md",
            "file_extension": ".md",
            "mime_type": "text/markdown",
            "size_bytes": 100,
            "source_relpath": "sources/doc-1-notes.md",
            "source_sha256": "abc",
            "parser": "native_markdown",
            "status": "ready",
            "parsed_markdown_relpath": "sources/doc-1-notes.md",
            "parsed_content_version": 5,
            "created_at": NOW,
            "updated_at": NOW,
            "completed_at": NOW,
        }
    )


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "ws" / KB_ID
    workspace.mkdir(parents=True)
    (workspace / "sources").mkdir(exist_ok=True)
    (workspace / "sources" / "doc-1-notes.md").write_text("# notes", encoding="utf-8")
    (workspace / f"{DOC_ID}.json").write_text(
        json.dumps({"id": DOC_ID, "doc_description": "desc", "structure": []}),
        encoding="utf-8",
    )
    (workspace / "_meta.json").write_text(
        json.dumps(
            {
                DOC_ID: {
                    "type": "markdown",
                    "doc_name": "notes.md",
                    "doc_description": "desc",
                    "path": "sources/doc-1-notes.md",
                    "line_count": 5,
                }
            }
        ),
        encoding="utf-8",
    )
    return workspace


class _FakeBackend:
    """Fake Milvus client recording delete calls; configurable failure modes."""

    def __init__(
        self,
        *,
        collections: set[str] | None = None,
        fail_milvus: bool = False,
        fail_delete: bool = False,
    ) -> None:
        self.collections = set(collections or ())
        self.fail_milvus = fail_milvus
        self.fail_delete = fail_delete
        self.deletes: list[tuple[str, str]] = []

    def has_collection(self, name: str) -> bool:
        if self.fail_milvus:
            raise RuntimeError("milvus unavailable")
        return name in self.collections

    def delete_by_doc(self, collection: str, doc_id: str) -> None:
        if self.fail_delete or self.fail_milvus:
            raise RuntimeError("milvus delete failed")
        self.deletes.append((collection, doc_id))


class _FakeStore:
    """Stand-in for ``NodeFtsStore`` / ``DocVectorStore``; shares a backend."""

    backend: _FakeBackend | None = None

    def __init__(
        self,
        *,
        uri: str | None = None,
        token: str | None = None,
        collection_name: str | None = None,
        dimension: int | None = None,
        knowledge_base_id: str | None = None,
        **_: Any,
    ) -> None:
        self.collection = collection_name
        self.knowledge_base_id = knowledge_base_id
        self.dimension = dimension
        self.client = _FakeStore.backend

    def delete_by_doc(self, doc_id: str) -> None:
        assert self.client is not None
        self.client.delete_by_doc(self.collection or "", doc_id)


class _FakeKnowledgeBases:
    """Return a fixed KB record (with ``workspace_dir``) for the pre-delete snapshot."""

    def __init__(self, record: dict[str, Any]) -> None:
        self._record = record

    def require_record(self, _knowledge_base_id: str) -> dict[str, Any]:
        return self._record


def _build_service(
    repository: SQLiteMetadataRepository,
    workspace: Path,
    tmp_path: Path,
    *,
    fts_schedule: Any,
    vector_schedule: Any,
) -> DocumentIngestionService:
    record = repository.get("knowledge_bases", KB_ID)
    assert record is not None
    record["workspace_dir"] = str(workspace)
    settings = ApiServerSettings(
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "ws",
        fts_enabled=True,
        milvus_uri="http://milvus.invalid:19530",
    )
    return DocumentIngestionService(
        repository,
        _FakeKnowledgeBases(record),
        None,  # models: unused by delete_document
        settings,
        fts_schedule=fts_schedule,
        vector_schedule=vector_schedule,
    )


def _install_fake_stores(monkeypatch, backend: _FakeBackend) -> None:
    _FakeStore.backend = backend
    monkeypatch.setattr(ingestion_module, "NodeFtsStore", _FakeStore)
    monkeypatch.setattr(ingestion_module, "DocVectorStore", _FakeStore)


def _seed_ready(repository: SQLiteMetadataRepository, tmp_path: Path) -> Path:
    _seed_ready_knowledge_base(repository)
    _seed_document(repository)
    return _workspace(tmp_path)


def test_delete_surgically_removes_records_and_advances_revision(
    tmp_path: Path, monkeypatch
) -> None:
    repository = _repository(tmp_path)
    workspace = _seed_ready(repository, tmp_path)
    backend = _FakeBackend(collections={FTS_COLLECTION, VECTOR_COLLECTION})
    _install_fake_stores(monkeypatch, backend)
    scheduled: list[tuple[str, bool]] = []
    service = _build_service(
        repository,
        workspace,
        tmp_path,
        fts_schedule=lambda _kb, *, force=False: scheduled.append(("fts", force)),
        vector_schedule=lambda _kb, *, force=False: scheduled.append(("vector", force)),
    )

    service.delete_document(KB_ID, DOC_ID)

    assert (FTS_COLLECTION, DOC_ID) in backend.deletes
    assert (VECTOR_COLLECTION, DOC_ID) in backend.deletes
    assert scheduled == []  # no rebuild scheduled

    kb = repository.get("knowledge_bases", KB_ID)
    assert kb is not None
    assert kb["content_version"] == 6
    assert kb["fts_status"] == "ready"
    assert kb["fts_revision"] == 6
    assert kb["vector_status"] == "ready"
    assert kb["vector_revision"] == 6
    assert repository.get_document(KB_ID, DOC_ID) is None


def test_delete_falls_back_to_rebuild_when_collection_missing(
    tmp_path: Path, monkeypatch
) -> None:
    repository = _repository(tmp_path)
    workspace = _seed_ready(repository, tmp_path)
    backend = _FakeBackend(collections=set())  # collections gone
    _install_fake_stores(monkeypatch, backend)
    scheduled: list[tuple[str, bool]] = []
    service = _build_service(
        repository,
        workspace,
        tmp_path,
        fts_schedule=lambda _kb, *, force=False: scheduled.append(("fts", force)),
        vector_schedule=lambda _kb, *, force=False: scheduled.append(("vector", force)),
    )

    service.delete_document(KB_ID, DOC_ID)

    assert backend.deletes == []  # surgical delete skipped
    assert scheduled == [("fts", True), ("vector", True)]

    kb = repository.get("knowledge_bases", KB_ID)
    assert kb is not None
    assert kb["content_version"] == 6
    assert kb["fts_status"] == "pending"
    assert kb["vector_status"] == "pending"


def test_delete_falls_back_to_rebuild_when_milvus_delete_fails(
    tmp_path: Path, monkeypatch
) -> None:
    repository = _repository(tmp_path)
    workspace = _seed_ready(repository, tmp_path)
    backend = _FakeBackend(
        collections={FTS_COLLECTION, VECTOR_COLLECTION}, fail_delete=True
    )
    _install_fake_stores(monkeypatch, backend)
    scheduled: list[tuple[str, bool]] = []
    service = _build_service(
        repository,
        workspace,
        tmp_path,
        fts_schedule=lambda _kb, *, force=False: scheduled.append(("fts", force)),
        vector_schedule=lambda _kb, *, force=False: scheduled.append(("vector", force)),
    )

    service.delete_document(KB_ID, DOC_ID)

    assert scheduled == [("fts", True), ("vector", True)]

    kb = repository.get("knowledge_bases", KB_ID)
    assert kb is not None
    assert kb["fts_status"] == "pending"
    assert kb["vector_status"] == "pending"
    assert kb["fts_revision"] == 5  # revision not advanced


def test_delete_falls_back_to_rebuild_when_milvus_unreachable(
    tmp_path: Path, monkeypatch
) -> None:
    repository = _repository(tmp_path)
    workspace = _seed_ready(repository, tmp_path)
    backend = _FakeBackend(
        collections={FTS_COLLECTION, VECTOR_COLLECTION}, fail_milvus=True
    )
    _install_fake_stores(monkeypatch, backend)
    scheduled: list[tuple[str, bool]] = []
    service = _build_service(
        repository,
        workspace,
        tmp_path,
        fts_schedule=lambda _kb, *, force=False: scheduled.append(("fts", force)),
        vector_schedule=lambda _kb, *, force=False: scheduled.append(("vector", force)),
    )

    service.delete_document(KB_ID, DOC_ID)

    assert scheduled == [("fts", True), ("vector", True)]
    kb = repository.get("knowledge_bases", KB_ID)
    assert kb is not None
    assert kb["fts_status"] == "pending"
    assert kb["vector_status"] == "pending"


def test_delete_skips_surgical_when_index_not_ready(
    tmp_path: Path, monkeypatch
) -> None:
    repository = _repository(tmp_path)
    # Index already pending (a rebuild is in flight or queued).
    _seed_ready_knowledge_base(
        repository, fts_status="pending", vector_status="pending"
    )
    _seed_document(repository)
    workspace = _workspace(tmp_path)
    backend = _FakeBackend(collections={FTS_COLLECTION, VECTOR_COLLECTION})
    _install_fake_stores(monkeypatch, backend)
    scheduled: list[tuple[str, bool]] = []
    service = _build_service(
        repository,
        workspace,
        tmp_path,
        fts_schedule=lambda _kb, *, force=False: scheduled.append(("fts", force)),
        vector_schedule=lambda _kb, *, force=False: scheduled.append(("vector", force)),
    )

    service.delete_document(KB_ID, DOC_ID)

    assert backend.deletes == []  # not ready -> no surgical delete
    assert scheduled == [("fts", True), ("vector", True)]


def test_surgical_delete_happens_before_revision_advance(
    tmp_path: Path, monkeypatch
) -> None:
    """Verify the crash-safety invariant: Milvus delete precedes revision advance.

    At the moment ``delete_by_doc`` runs, the KB must already have its
    ``content_version`` bumped (so the delete targets the post-delete revision)
    but its ``*_revision`` NOT yet advanced (advance only follows a successful
    delete). If the order were reversed, a crash mid-delete would leave the index
    advertised as ready with the deleted document's records still present.
    """
    repository = _repository(tmp_path)
    workspace = _seed_ready(repository, tmp_path)
    snapshots: list[tuple[str, str, str, int, int | None, int | None]] = []

    class _OrderBackend(_FakeBackend):
        def __init__(self) -> None:
            super().__init__(collections={FTS_COLLECTION, VECTOR_COLLECTION})

        def delete_by_doc(self, collection: str, doc_id: str) -> None:
            kb = repository.get("knowledge_bases", KB_ID)
            assert kb is not None
            snapshots.append(
                (
                    collection,
                    kb["fts_status"],
                    kb["vector_status"],
                    kb["content_version"],
                    kb["fts_revision"],
                    kb["vector_revision"],
                )
            )
            super().delete_by_doc(collection, doc_id)

    backend = _OrderBackend()
    _install_fake_stores(monkeypatch, backend)
    service = _build_service(
        repository,
        workspace,
        tmp_path,
        fts_schedule=lambda _kb, *, force=False: None,
        vector_schedule=lambda _kb, *, force=False: None,
    )

    service.delete_document(KB_ID, DOC_ID)

    # Two surgical deletes: FTS first, then vector.
    assert [snap[0] for snap in snapshots] == [FTS_COLLECTION, VECTOR_COLLECTION]
    fts_snap = snapshots[0]
    vec_snap = snapshots[1]
    # content_version already bumped to 6 by repository.delete_document ...
    assert fts_snap[3] == 6
    assert vec_snap[3] == 6
    # ... but fts_revision not yet advanced when FTS delete runs ...
    assert fts_snap[4] == 5
    assert fts_snap[1] == "pending"
    # ... and vector_revision not yet advanced when vector delete runs.
    assert vec_snap[5] == 5
    assert vec_snap[2] == "pending"

    kb = repository.get("knowledge_bases", KB_ID)
    assert kb is not None
    assert kb["fts_revision"] == 6  # advanced only after the delete succeeded
    assert kb["vector_revision"] == 6


def test_concurrent_upload_blocks_revision_advance(tmp_path: Path, monkeypatch) -> None:
    """A concurrent upload landing between delete and advance must not set ready.

    The advance's version gate mirrors ``finish_*_build``: only advance when
    ``content_version`` still equals the revision produced by THIS delete. A
    concurrent upload that bumps the version further leaves the index pending so
    that upload's rebuild (which rebuilds from the remaining manifest, excluding
    the deleted doc) handles the index. Otherwise the new document would be
    hidden behind a stale "ready".
    """

    class _RaceBackend(_FakeBackend):
        def __init__(self) -> None:
            super().__init__(collections={FTS_COLLECTION, VECTOR_COLLECTION})

        def delete_by_doc(self, collection: str, doc_id: str) -> None:
            # Simulate a concurrent upload committing between the surgical delete
            # and the revision advance: bump content_version past expected (6->7).
            with repository.factory.session_scope(write=True) as session:
                kb = session.get(KnowledgeBase, KB_ID)
                assert kb is not None
                kb.content_version = 7
                kb.fts_target_revision = 7
                kb.vector_target_revision = 7
            super().delete_by_doc(collection, doc_id)

    repository = _repository(tmp_path)
    workspace = _seed_ready(repository, tmp_path)
    backend = _RaceBackend()
    _install_fake_stores(monkeypatch, backend)
    scheduled: list[tuple[str, bool]] = []
    service = _build_service(
        repository,
        workspace,
        tmp_path,
        fts_schedule=lambda _kb, *, force=False: scheduled.append(("fts", force)),
        vector_schedule=lambda _kb, *, force=False: scheduled.append(("vector", force)),
    )

    service.delete_document(KB_ID, DOC_ID)

    # Surgical delete ran on both collections ...
    assert (FTS_COLLECTION, DOC_ID) in backend.deletes
    assert (VECTOR_COLLECTION, DOC_ID) in backend.deletes
    # ... but the version gate rejected the advance, so a rebuild is scheduled.
    assert scheduled == [("fts", True), ("vector", True)]
    kb = repository.get("knowledge_bases", KB_ID)
    assert kb is not None
    assert kb["content_version"] == 7
    assert kb["fts_status"] == "pending"
    assert kb["vector_status"] == "pending"
    assert kb["fts_revision"] == 5  # not advanced
    assert kb["vector_revision"] == 5
