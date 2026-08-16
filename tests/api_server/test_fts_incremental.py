"""Stage-1 FTS 增量索引 repository 单测（无真实 Milvus）。

覆盖：脏集查询、置干净、全置脏，以及 ``commit_upload`` 对提交文档置脏。
"""

from __future__ import annotations

from pathlib import Path

from app.api_server.database import SQLiteConnectionFactory, initialize_database
from app.api_server.repositories import SQLiteMetadataRepository

NOW = "2026-08-12T00:00:00+00:00"


def _repository(tmp_path: Path) -> SQLiteMetadataRepository:
    factory = SQLiteConnectionFactory(tmp_path / "api.sqlite3")
    initialize_database(factory)
    return SQLiteMetadataRepository(factory)


def _seed_kb(repository: SQLiteMetadataRepository, kb_id: str, *, fts_revision: int | None = 5) -> None:
    repository.put(
        "knowledge_bases",
        kb_id,
        {
            "id": kb_id,
            "name": f"KB {kb_id}",
            "status": "ready",
            "workspace_relpath": kb_id,
            "document_count": 0,
            "summary_enabled": True,
            "content_version": fts_revision or 0,
            "fts_status": "ready" if fts_revision is not None else "pending",
            "fts_revision": fts_revision,
            "fts_target_revision": fts_revision,
            "fts_collection": f"fts_{kb_id}" if fts_revision is not None else None,
            "fts_error": None,
            "vector_status": "disabled",
            "vector_revision": None,
            "vector_target_revision": None,
            "vector_collection": None,
            "vector_error": None,
            "created_at": NOW,
            "updated_at": NOW,
        },
    )


def _seed_doc(
    repository: SQLiteMetadataRepository,
    kb_id: str,
    doc_id: str,
    *,
    status: str = "ready",
    fts_indexed_version: int | None = None,
) -> None:
    repository.create_document(
        {
            "id": doc_id,
            "knowledge_base_id": kb_id,
            "original_filename": f"{doc_id}.md",
            "file_extension": ".md",
            "mime_type": "text/markdown",
            "size_bytes": 10,
            "source_relpath": f"sources/{doc_id}.md",
            "source_sha256": doc_id,
            "parser": "native_markdown",
            "status": status,
            "fts_indexed_version": fts_indexed_version,
            "created_at": NOW,
            "updated_at": NOW,
            "completed_at": NOW,
        }
    )


# ---------------------------------------------------------------------------
# 脏集查询
# ---------------------------------------------------------------------------


def test_list_fts_dirty_documents_returns_only_ready_null(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _seed_kb(repository, "kb-1")
    _seed_doc(repository, "kb-1", "clean-1", fts_indexed_version=5)
    _seed_doc(repository, "kb-1", "dirty-1", fts_indexed_version=None)
    _seed_doc(repository, "kb-1", "dirty-2", fts_indexed_version=None)
    _seed_doc(repository, "kb-1", "parsing", status="parsing", fts_indexed_version=None)

    dirty = repository.list_fts_dirty_documents("kb-1")

    assert dirty == ["dirty-1", "dirty-2"]


def test_list_fts_dirty_documents_isolates_by_knowledge_base(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _seed_kb(repository, "kb-1")
    _seed_kb(repository, "kb-2")
    _seed_doc(repository, "kb-1", "kb1-dirty", fts_indexed_version=None)
    _seed_doc(repository, "kb-2", "kb2-dirty", fts_indexed_version=None)

    assert repository.list_fts_dirty_documents("kb-1") == ["kb1-dirty"]
    assert repository.list_fts_dirty_documents("kb-2") == ["kb2-dirty"]


# ---------------------------------------------------------------------------
# 置干净 / 全置脏
# ---------------------------------------------------------------------------


def test_mark_documents_fts_indexed_sets_only_specified(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _seed_kb(repository, "kb-1")
    _seed_doc(repository, "kb-1", "doc-1", fts_indexed_version=None)
    _seed_doc(repository, "kb-1", "doc-2", fts_indexed_version=None)

    repository.mark_documents_fts_indexed("kb-1", ["doc-1"], 6, NOW)

    doc1 = repository.get_document("kb-1", "doc-1")
    doc2 = repository.get_document("kb-1", "doc-2")
    assert doc1["fts_indexed_version"] == 6
    assert doc2["fts_indexed_version"] is None
    assert repository.list_fts_dirty_documents("kb-1") == ["doc-2"]


def test_mark_documents_fts_indexed_empty_is_noop(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _seed_kb(repository, "kb-1")
    _seed_doc(repository, "kb-1", "doc-1", fts_indexed_version=None)

    repository.mark_documents_fts_indexed("kb-1", [], 6, NOW)

    assert repository.list_fts_dirty_documents("kb-1") == ["doc-1"]

def test_mark_all_fts_dirty_clears_clean_documents(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _seed_kb(repository, "kb-1")
    _seed_doc(repository, "kb-1", "doc-1", fts_indexed_version=5)
    _seed_doc(repository, "kb-1", "doc-2", fts_indexed_version=5)

    repository.mark_all_fts_dirty("kb-1", NOW)

    assert sorted(repository.list_fts_dirty_documents("kb-1")) == ["doc-1", "doc-2"]


# ---------------------------------------------------------------------------
# commit_upload 置脏
# ---------------------------------------------------------------------------


def test_commit_upload_marks_committed_document_dirty(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _seed_kb(repository, "kb-1", fts_revision=5)
    _seed_doc(repository, "kb-1", "doc-1", fts_indexed_version=5)  # 原本干净

    repository.start_upload("kb-1", "key-1", "req-sha", "doc-1", NOW)
    repository.mark_upload_files_committed(
        "kb-1", "key-1", "sources/doc-1.md", "parsed/doc-1", "src-sha", "art-sha", NOW
    )

    new_version = repository.commit_upload("kb-1", "key-1", 1, NOW)

    assert new_version == 6
    doc = repository.get_document("kb-1", "doc-1")
    assert doc["fts_indexed_version"] is None
    # 提交文档应进入脏集。
    assert repository.list_fts_dirty_documents("kb-1") == ["doc-1"]
