from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import time

import pytest
from sqlalchemy import select

from app.api_server.database import SQLiteConnectionFactory, initialize_database
from app.api_server.database.models import (
    Document,
    DocumentEnrichmentTask,
    DocumentNormalizationTask,
    DocumentParseTask,
    DocumentStagingAllocation,
)
from app.api_server.repositories import SQLiteMetadataRepository
from app.api_server.repositories.metadata.publishing import PublishConflictError
from app.api_server.services.workspace_snapshot import (
    cleanup_superseded_snapshots,
    cleanup_unreferenced_generation_artifacts,
    load_committed_manifest,
    publish_document_deletion_snapshot,
    publish_document_snapshot,
    quarantine_uncommitted_snapshot,
    reconcile_knowledge_base_workspace,
    reconcile_staging_allocations,
)
from nianlun.knowledgebase.workspace_view import WorkspaceView, WorkspaceViewError


NOW = "2026-09-05T00:00:00+00:00"


def _repository(tmp_path: Path) -> SQLiteMetadataRepository:
    factory = SQLiteConnectionFactory(tmp_path / "api.sqlite3")
    initialize_database(factory)
    return SQLiteMetadataRepository(factory)


def _seed_knowledge_base(repository: SQLiteMetadataRepository, workspace: Path) -> None:
    workspace.mkdir()
    (workspace / "_meta.json").write_text("{}", encoding="utf-8")
    repository.put(
        "knowledge_bases",
        "kb-1",
        {
            "id": "kb-1",
            "name": "测试知识库",
            "status": "ready",
            "workspace_relpath": "kb-1",
            "document_count": 0,
            "summary_enabled": True,
            "content_version": 0,
            "fts_status": "disabled",
            "vector_status": "disabled",
            "created_at": NOW,
            "updated_at": NOW,
        },
    )
    assert (
        reconcile_knowledge_base_workspace(repository, "kb-1", workspace, now=NOW)
        == "bootstrapped"
    )


def test_publish_creates_immutable_revision_and_commits_upload_atomically(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    workspace = tmp_path / "kb-1"
    _seed_knowledge_base(repository, workspace)

    source = b"# Title\n\nbody\n"
    source_relpath = "sources/doc-1.md"
    (workspace / "sources").mkdir()
    (workspace / source_relpath).write_bytes(source)
    tree = {
        "id": "doc-1",
        "doc_name": "doc-1.md",
        "doc_description": "description",
        "type": "md",
        "line_count": 3,
        "structure": [],
    }
    (workspace / "doc-1.json").write_text(json.dumps(tree), encoding="utf-8")
    repository.start_upload("kb-1", "upload-1", "request-hash", "doc-1", NOW)
    repository.create_document(
        {
            "id": "doc-1",
            "knowledge_base_id": "kb-1",
            "original_filename": "doc-1.md",
            "file_extension": ".md",
            "mime_type": "text/markdown",
            "size_bytes": len(source),
            "source_relpath": source_relpath,
            "source_sha256": hashlib.sha256(source).hexdigest(),
            "parser": "native_markdown",
            "status": "uploaded",
            "created_at": NOW,
            "updated_at": NOW,
        }
    )
    repository.mark_upload_files_committed(
        "kb-1",
        "upload-1",
        source_relpath,
        "doc-1.json",
        hashlib.sha256(source).hexdigest(),
        hashlib.sha256(json.dumps(tree).encode()).hexdigest(),
        NOW,
    )

    revision = publish_document_snapshot(
        repository,
        workspace=workspace,
        knowledge_base_id="kb-1",
        document_id="doc-1",
        generation=1,
        content=source,
        source_relpath=source_relpath,
        source_mime_type="text/markdown",
        idempotency_key="upload-1",
        fts_enabled=True,
        now=NOW,
    )

    assert revision == 1
    knowledge_base = repository.get("knowledge_bases", "kb-1")
    document = repository.get_document("kb-1", "doc-1")
    operation = repository.get_upload("kb-1", "upload-1")
    assert knowledge_base is not None and knowledge_base["content_version"] == 1
    assert knowledge_base["fts_status"] == "pending"
    assert document is not None and document["parsed_content_version"] == 1
    assert document["parsed_markdown_relpath"].endswith("/g1/enriched/full.md")
    assert operation is not None and operation["status"] == "committed"
    assert repository.get_revision("kb-1", 0)["state"] == "superseded"
    current = repository.get_committed_revision("kb-1", 1)
    assert current is not None
    manifest = load_committed_manifest(workspace, current)
    assert [item.document_id for item in manifest.documents] == ["doc-1"]

    # V2 readers remain pinned even if the best-effort root projection drifts.
    (workspace / "doc-1.json").write_text('{"doc_name":"corrupt root"}')
    view = WorkspaceView.revision(
        workspace,
        current["snapshot_relpath"],
        current["manifest_sha256"],
        knowledge_base_id="kb-1",
        content_version=1,
    )
    assert view.load_document("doc-1")["doc_name"] == "doc-1.md"
    assert view.read_content("doc-1") == source.decode()


def test_publish_conflict_rolls_back_all_sqlite_state(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    workspace = tmp_path / "kb-1"
    _seed_knowledge_base(repository, workspace)
    repository.create_document(
        {
            "id": "doc-1",
            "knowledge_base_id": "kb-1",
            "original_filename": "doc.md",
            "file_extension": ".md",
            "mime_type": "text/markdown",
            "size_bytes": 1,
            "source_relpath": "sources/doc.md",
            "source_sha256": "hash",
            "parser": "native_markdown",
            "status": "uploaded",
            "created_at": NOW,
            "updated_at": NOW,
        }
    )

    with pytest.raises(PublishConflictError, match="content_version"):
        repository.publish_document_revision(
            knowledge_base_id="kb-1",
            document_id="doc-1",
            document_count=1,
            expected_content_version=9,
            next_content_version=10,
            snapshot_relpath="snapshots/r10",
            manifest_sha256="0" * 64,
            artifacts=[],
            parsed_markdown_relpath="artifacts/doc-1/g1/enriched/full.md",
            generation=1,
            idempotency_key=None,
            fts_collection="fts",
            index_statuses=(True, False),
            now=NOW,
        )

    assert repository.get("knowledge_bases", "kb-1")["content_version"] == 0
    assert repository.get_revision("kb-1", 10) is None
    assert repository.get_document("kb-1", "doc-1")["status"] == "uploaded"


def test_uncommitted_snapshot_is_quarantined_to_free_its_version_slot(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    workspace = tmp_path / "kb-1"
    _seed_knowledge_base(repository, workspace)
    uncommitted = workspace / "snapshots/r1"
    uncommitted.mkdir()
    (uncommitted / "manifest.json").write_text("{}", encoding="utf-8")

    quarantine_uncommitted_snapshot(
        repository, workspace, "kb-1", 1
    )

    assert not uncommitted.exists()
    assert list((workspace / "snapshots").glob(".orphan-r1-*"))


def test_startup_quarantines_fresh_untracked_snapshot_before_retention(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    workspace = tmp_path / "kb-1"
    _seed_knowledge_base(repository, workspace)
    untracked = workspace / "snapshots/r1"
    untracked.mkdir()
    (untracked / "manifest.json").write_text("{}", encoding="utf-8")

    assert reconcile_knowledge_base_workspace(repository, "kb-1", workspace, now=NOW) == "ok"

    assert not untracked.exists()
    assert list((workspace / "snapshots").glob(".orphan-r1-*"))


def test_deletion_retains_tombstone_and_cancels_active_pipeline_tasks(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    workspace = tmp_path / "kb-1"
    _seed_knowledge_base(repository, workspace)
    source = b"# Title\n"
    source_hash = hashlib.sha256(source).hexdigest()
    source_relpath = "sources/doc-1.md"
    (workspace / "sources").mkdir()
    (workspace / source_relpath).write_bytes(source)
    tree_raw = json.dumps(
        {"id": "doc-1", "doc_name": "doc-1.md", "line_count": 1, "structure": []}
    ).encode()
    (workspace / "doc-1.json").write_bytes(tree_raw)
    repository.create_document(
        {
            "id": "doc-1",
            "knowledge_base_id": "kb-1",
            "original_filename": "doc-1.md",
            "file_extension": ".md",
            "mime_type": "text/markdown",
            "size_bytes": len(source),
            "source_relpath": source_relpath,
            "source_sha256": source_hash,
            "parser": "native_markdown",
            "status": "uploaded",
            "created_at": NOW,
            "updated_at": NOW,
        }
    )
    assert (
        publish_document_snapshot(
            repository,
            workspace=workspace,
            knowledge_base_id="kb-1",
            document_id="doc-1",
            generation=1,
            content=source,
            source_relpath=source_relpath,
            source_mime_type="text/markdown",
            idempotency_key=None,
            fts_enabled=False,
            now=NOW,
        )
        == 1
    )
    parse = repository.create_parse_task(
        {
            "id": "parse-1",
            "document_id": "doc-1",
            "attempt": 1,
            "data_id": "doc-1:g1:c0000",
            "model_version": "vlm",
            "state": "pending",
            "created_at": NOW,
            "updated_at": NOW,
        }
    )
    assert (
        repository.claim_parse_task(
            parse["id"],
            owner="worker",
            mode="submit",
            now=NOW,
            lease_expires_at="2026-09-05T00:01:00+00:00",
        )
        is not None
    )
    repository.create_normalization_task(
        document_id="doc-1",
        pipeline_generation=1,
        input_manifest_json="{}",
        now=NOW,
    )
    assert (
        repository.claim_normalization_task(
            owner="worker",
            now=NOW,
            lease_expires_at="2026-09-05T00:01:00+00:00",
        )
        is not None
    )
    repository.create_enrichment_task(
        document_id="doc-1",
        pipeline_generation=1,
        input_sha256=source_hash,
        llm_profile_id=None,
        llm_profile_updated_at=None,
        prompt_schema_version=1,
        options_json="{}",
        now=NOW,
    )
    assert (
        repository.claim_enrichment_task(
            owner="worker",
            now=NOW,
            lease_expires_at="2026-09-05T00:01:00+00:00",
        )
        is not None
    )
    assert repository.reserve_staging_bytes(
        document_id="doc-1",
        pipeline_generation=1,
        allocation_key="parse:0000:zip",
        kind="result_zip",
        bytes_reserved=10,
        maximum_bytes=100,
        now=NOW,
    )

    revision = publish_document_deletion_snapshot(
        repository,
        workspace=workspace,
        knowledge_base_id="kb-1",
        document_id="doc-1",
        fts_enabled=False,
        now=NOW,
    )

    assert revision == 2
    assert repository.get_document("kb-1", "doc-1") is None
    assert repository.list_documents("kb-1") == []
    with repository.factory.session_scope() as session:
        tombstone = session.get(Document, "doc-1")
        assert tombstone is not None
        assert tombstone.status == "deleted"
        assert tombstone.deleted_content_version == 2
        assert tombstone.fts_indexed_version == 2
        assert tombstone.vector_indexed_version == 2
        for task_model in (
            DocumentParseTask,
            DocumentNormalizationTask,
            DocumentEnrichmentTask,
        ):
            task = session.scalar(
                select(task_model).where(task_model.document_id == "doc-1")
            )
            assert task is not None
            assert task.state == "canceled"
            assert task.lease_token is None
            assert task.error_code == "DOCUMENT_DELETED"
        allocation = session.scalar(
            select(DocumentStagingAllocation).where(
                DocumentStagingAllocation.document_id == "doc-1"
            )
        )
        assert allocation is not None and allocation.state == "released"
    replacement = repository.create_document(
        {
            "id": "doc-2",
            "knowledge_base_id": "kb-1",
            "original_filename": "doc-2.md",
            "file_extension": ".md",
            "mime_type": "text/markdown",
            "size_bytes": len(source),
            "source_relpath": "sources/doc-2.md",
            "source_sha256": source_hash,
            "parser": "native_markdown",
            "status": "uploaded",
            "created_at": NOW,
            "updated_at": NOW,
        }
    )
    assert replacement["id"] == "doc-2"


def test_deleted_document_cannot_be_requeued_by_delayed_pipeline_handoff(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    workspace = tmp_path / "kb-1"
    _seed_knowledge_base(repository, workspace)
    repository.create_document(
        {
            "id": "doc-1",
            "knowledge_base_id": "kb-1",
            "original_filename": "doc.md",
            "file_extension": ".md",
            "mime_type": "text/markdown",
            "size_bytes": 1,
            "source_relpath": "sources/doc.md",
            "source_sha256": "hash",
            "parser": "native_markdown",
            "status": "deleted",
            "pipeline_generation": 2,
            "current_stage": "complete",
            "stage_state": "succeeded",
            "deleted_content_version": 1,
            "created_at": NOW,
            "updated_at": NOW,
        }
    )

    with pytest.raises(ValueError, match="已删除"):
        repository.create_normalization_task(
            document_id="doc-1",
            pipeline_generation=2,
            input_manifest_json="{}",
            now=NOW,
        )
    with pytest.raises(ValueError, match="已删除"):
        repository.create_enrichment_task(
            document_id="doc-1",
            pipeline_generation=2,
            input_sha256="hash",
            llm_profile_id=None,
            llm_profile_updated_at=None,
            prompt_schema_version=1,
            options_json="{}",
            now=NOW,
        )

    with repository.factory.session_scope() as session:
        tombstone = session.get(Document, "doc-1")
        assert tombstone is not None and tombstone.status == "deleted"


def test_pipeline_progress_update_is_fenced_and_renews_lease(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    workspace = tmp_path / "kb-1"
    _seed_knowledge_base(repository, workspace)
    repository.create_document(
        {
            "id": "doc-1",
            "knowledge_base_id": "kb-1",
            "original_filename": "doc.md",
            "file_extension": ".md",
            "mime_type": "text/markdown",
            "size_bytes": 4,
            "source_relpath": "sources/doc.md",
            "source_sha256": "hash",
            "parser": "native_markdown",
            "status": "parsing",
            "created_at": NOW,
            "updated_at": NOW,
        }
    )
    task = repository.create_normalization_task(
        document_id="doc-1",
        pipeline_generation=1,
        input_manifest_json='{"chunks": [{}, {}]}',
        now=NOW,
    )
    claimed = repository.claim_normalization_task(
        owner="worker",
        now=NOW,
        lease_expires_at="2026-09-05T00:01:00+00:00",
    )
    assert claimed is not None
    token = str(claimed["lease_token"])

    assert repository.update_pipeline_task_progress(
        "normalize",
        str(task["id"]),
        token,
        progress_completed=1,
        progress_total=2,
        lease_expires_at="2026-09-05T00:03:00+00:00",
        now="2026-09-05T00:00:30+00:00",
    )
    document = repository.get_document("kb-1", "doc-1")
    assert document is not None
    assert (
        document["progress_completed"],
        document["progress_total"],
        document["progress_unit"],
    ) == (1, 2, "chunks")
    renewed = repository.get_normalization_task(str(task["id"]))
    assert renewed is not None
    assert renewed["lease_expires_at"] == "2026-09-05T00:03:00+00:00"

    assert repository.renew_pipeline_task_lease(
        "normalize",
        str(task["id"]),
        token,
        lease_expires_at="2026-09-05T00:05:00+00:00",
        now="2026-09-05T00:02:00+00:00",
    )
    heartbeat = repository.get_normalization_task(str(task["id"]))
    assert heartbeat is not None
    assert heartbeat["lease_expires_at"] == "2026-09-05T00:05:00+00:00"

    assert not repository.renew_pipeline_task_lease(
        "normalize",
        str(task["id"]),
        "stale-token",
        lease_expires_at="2026-09-05T00:06:00+00:00",
        now="2026-09-05T00:03:00+00:00",
    )
    assert not repository.update_pipeline_task_progress(
        "normalize",
        str(task["id"]),
        token,
        progress_completed=2,
        progress_total=2,
        lease_expires_at="2026-09-05T00:07:00+00:00",
        now="2026-09-05T00:06:00+00:00",
    )


def test_parse_completion_updates_document_page_progress(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    workspace = tmp_path / "kb-1"
    _seed_knowledge_base(repository, workspace)
    repository.create_document(
        {
            "id": "doc-1",
            "knowledge_base_id": "kb-1",
            "original_filename": "doc.pdf",
            "file_extension": ".pdf",
            "mime_type": "application/pdf",
            "size_bytes": 4,
            "source_relpath": "sources/doc.pdf",
            "source_sha256": "hash",
            "parser": "mineru",
            "status": "parsing",
            "current_stage": "parse",
            "stage_state": "running",
            "created_at": NOW,
            "updated_at": NOW,
        }
    )
    tasks = [
        repository.create_parse_task(
            {
                "id": f"parse-{index}",
                "document_id": "doc-1",
                "attempt": 1,
                "pipeline_generation": 1,
                "chunk_index": index,
                "chunk_count": 2,
                "source_page_start": start,
                "source_page_end": end,
                "data_id": f"doc-1:g1:c{index:04d}",
                "model_version": "vlm",
                "state": "pending",
                "created_at": NOW,
                "updated_at": NOW,
            }
        )
        for index, (start, end) in enumerate(((1, 180), (181, 401)))
    ]

    for index, task in enumerate(tasks):
        claimed = repository.claim_parse_task(
            str(task["id"]),
            owner="worker",
            mode="submit",
            now=NOW,
            lease_expires_at="2026-09-05T00:01:00+00:00",
        )
        assert claimed is not None
        assert repository.update_claimed_parse_task(
            str(task["id"]),
            str(claimed["lease_token"]),
            {"state": "done", "updated_at": NOW},
            release=True,
        )
        document = repository.get_document("kb-1", "doc-1")
        assert document is not None
        assert document["progress_completed"] == (180 if index == 0 else 401)
        assert document["progress_total"] == 401
        assert document["progress_unit"] == "pages"


def test_workspace_view_rejects_tampered_artifact(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    workspace = tmp_path / "kb-1"
    _seed_knowledge_base(repository, workspace)
    # Empty r0 proves the revision itself is valid; tamper the manifest hash input.
    revision = repository.get_committed_revision("kb-1", 0)
    assert revision is not None
    with pytest.raises(WorkspaceViewError, match="manifest hash"):
        WorkspaceView.revision(
            workspace,
            revision["snapshot_relpath"],
            "f" * 64,
            knowledge_base_id="kb-1",
            content_version=0,
        )


def test_workspace_view_rejects_unregistered_content_artifact(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "kb-1"
    snapshot = workspace / "snapshots" / "r1"
    snapshot.mkdir(parents=True)
    tree = b'{"doc_name":"doc.md","structure":[]}'
    content = b"# Title\n"
    (workspace / "tree.json").write_bytes(tree)
    (workspace / "content.md").write_bytes(content)
    manifest = {
        "schema_version": 2,
        "knowledge_base_id": "kb-1",
        "content_version": 1,
        "documents": [
            {
                "document_id": "doc-1",
                "index_relpath": "tree.json",
                "content_relpath": "content.md",
                "generation": 1,
            }
        ],
        "artifact_files": [
            {
                "relpath": "tree.json",
                "size_bytes": len(tree),
                "sha256": hashlib.sha256(tree).hexdigest(),
            }
        ],
    }
    raw = json.dumps(manifest).encode()
    (snapshot / "manifest.json").write_bytes(raw)

    with pytest.raises(WorkspaceViewError, match="未登记的 artifact"):
        WorkspaceView.revision(
            workspace,
            "snapshots/r1",
            hashlib.sha256(raw).hexdigest(),
            knowledge_base_id="kb-1",
            content_version=1,
        )


def test_superseded_snapshot_gc_waits_for_reader_handle(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    workspace = tmp_path / "kb-1"
    _seed_knowledge_base(repository, workspace)
    revision_zero = repository.get_committed_revision("kb-1", 0)
    assert revision_zero is not None
    view = WorkspaceView.revision(
        workspace,
        revision_zero["snapshot_relpath"],
        revision_zero["manifest_sha256"],
        knowledge_base_id="kb-1",
        content_version=0,
    )
    source = b"# Title"
    (workspace / "sources").mkdir()
    (workspace / "sources/doc.md").write_bytes(source)
    tree_raw = json.dumps(
        {"id": "doc-1", "doc_name": "doc.md", "line_count": 1, "structure": []}
    ).encode()
    (workspace / "doc-1.json").write_bytes(tree_raw)
    repository.create_document(
        {
            "id": "doc-1",
            "knowledge_base_id": "kb-1",
            "original_filename": "doc.md",
            "file_extension": ".md",
            "mime_type": "text/markdown",
            "size_bytes": len(source),
            "source_relpath": "sources/doc.md",
            "source_sha256": hashlib.sha256(source).hexdigest(),
            "parser": "native_markdown",
            "status": "uploaded",
            "created_at": NOW,
            "updated_at": NOW,
        }
    )
    publish_document_snapshot(
        repository,
        workspace=workspace,
        knowledge_base_id="kb-1",
        document_id="doc-1",
        generation=1,
        content=source,
        source_relpath="sources/doc.md",
        source_mime_type="text/markdown",
        idempotency_key=None,
        fts_enabled=False,
        now=NOW,
    )
    old_snapshot = workspace / "snapshots/r0"
    old_time = time.time() - 60
    os.utime(old_snapshot, (old_time, old_time))

    revisions = repository.list_revisions("kb-1")
    assert (
        cleanup_superseded_snapshots(
            repository, workspace, "kb-1", revisions, retention_seconds=1
        )
        == []
    )
    assert old_snapshot.exists()

    view.close()
    assert cleanup_superseded_snapshots(
        repository, workspace, "kb-1", revisions, retention_seconds=1
    ) == [0]
    assert not old_snapshot.exists()
    assert repository.get_revision("kb-1", 0) is None


def test_staging_reconciliation_rebuilds_actual_bytes_and_removes_expired_data(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    workspace = tmp_path / "kb-1"
    _seed_knowledge_base(repository, workspace)
    repository.create_document(
        {
            "id": "doc-1",
            "knowledge_base_id": "kb-1",
            "original_filename": "doc.pdf",
            "file_extension": ".pdf",
            "mime_type": "application/pdf",
            "size_bytes": 4,
            "source_relpath": "sources/doc.pdf",
            "source_sha256": "source-hash",
            "parser": "mineru",
            "status": "parsing",
            "current_stage": "parse",
            "stage_state": "queued",
            "created_at": NOW,
            "updated_at": NOW,
        }
    )
    generation_dir = workspace / "staging/doc-1/g1"
    generation_dir.mkdir(parents=True)
    (generation_dir / "payload.bin").write_bytes(b"four")
    assert repository.reserve_staging_bytes(
        document_id="doc-1",
        pipeline_generation=1,
        allocation_key="stale",
        kind="temp_output",
        bytes_reserved=999,
        maximum_bytes=2_000,
        now=NOW,
    )
    orphan = workspace / "staging/missing/g9"
    orphan.mkdir(parents=True)
    (orphan / "payload.bin").write_bytes(b"orphan")
    old_time = time.time() - 60
    os.utime(orphan, (old_time, old_time))

    removed = reconcile_staging_allocations(
        repository,
        workspace,
        "kb-1",
        now=NOW,
        retention_seconds=1,
    )

    assert "staging/missing/g9" in removed
    assert not orphan.exists()
    with repository.factory.session_scope() as session:
        allocations = session.scalars(
            select(DocumentStagingAllocation).where(
                DocumentStagingAllocation.document_id == "doc-1",
                DocumentStagingAllocation.pipeline_generation == 1,
            )
        ).all()
        active = [item for item in allocations if item.state == "active"]
        assert [(item.allocation_key, item.bytes_reserved) for item in active] == [
            ("reconcile:filesystem", 4)
        ]


def test_generation_artifact_gc_respects_current_task_and_allocation_refs(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    workspace = tmp_path / "kb-1"
    _seed_knowledge_base(repository, workspace)
    repository.create_document(
        {
            "id": "doc-1",
            "knowledge_base_id": "kb-1",
            "original_filename": "doc.pdf",
            "file_extension": ".pdf",
            "mime_type": "application/pdf",
            "size_bytes": 4,
            "source_relpath": "sources/doc.pdf",
            "source_sha256": "source-hash",
            "parser": "mineru",
            "status": "failed",
            "pipeline_generation": 1,
            "created_at": NOW,
            "updated_at": NOW,
        }
    )
    repository.create_parse_task(
        {
            "id": "parse-g3",
            "document_id": "doc-1",
            "attempt": 1,
            "pipeline_generation": 3,
            "data_id": "doc-1:g3:c0000",
            "model_version": "vlm",
            "state": "pending",
            "created_at": NOW,
            "updated_at": NOW,
        }
    )
    assert repository.reserve_staging_bytes(
        document_id="doc-1",
        pipeline_generation=4,
        allocation_key="keep",
        kind="temp_output",
        bytes_reserved=1,
        maximum_bytes=10,
        now=NOW,
    )
    old_time = time.time() - 60
    for generation in (1, 2, 3, 4):
        path = workspace / f"artifacts/doc-1/g{generation}"
        path.mkdir(parents=True)
        (path / "value.bin").write_bytes(b"x")
        os.utime(path, (old_time, old_time))

    removed = cleanup_unreferenced_generation_artifacts(
        repository,
        workspace,
        "kb-1",
        retention_seconds=1,
    )

    assert removed == ["artifacts/doc-1/g2"]
    assert (workspace / "artifacts/doc-1/g1").is_dir()
    assert (workspace / "artifacts/doc-1/g3").is_dir()
    assert (workspace / "artifacts/doc-1/g4").is_dir()


def test_claimed_parse_staging_reservation_is_fenced_and_cumulative(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    workspace = tmp_path / "kb-1"
    _seed_knowledge_base(repository, workspace)
    repository.create_document(
        {
            "id": "doc-1",
            "knowledge_base_id": "kb-1",
            "original_filename": "doc.pdf",
            "file_extension": ".pdf",
            "mime_type": "application/pdf",
            "size_bytes": 4,
            "source_relpath": "sources/doc.pdf",
            "source_sha256": "source-hash",
            "parser": "mineru",
            "status": "parsing",
            "created_at": NOW,
            "updated_at": NOW,
        }
    )
    repository.create_parse_task(
        {
            "id": "parse-1",
            "document_id": "doc-1",
            "attempt": 1,
            "data_id": "doc-1:g1:c0000",
            "model_version": "vlm",
            "state": "pending",
            "created_at": NOW,
            "updated_at": NOW,
        }
    )
    task = repository.claim_parse_task(
        "parse-1",
        owner="worker",
        mode="submit",
        now=NOW,
        lease_expires_at="2026-09-05T00:01:00+00:00",
    )
    assert task is not None
    token = str(task["lease_token"])
    assert repository.reserve_claimed_parse_staging_bytes(
        task_id="parse-1",
        lease_token=token,
        allocation_key="zip",
        kind="result_zip",
        bytes_reserved=6,
        maximum_bytes=10,
        now=NOW,
    )
    assert not repository.reserve_claimed_parse_staging_bytes(
        task_id="parse-1",
        lease_token=token,
        allocation_key="extracted",
        kind="extracted",
        bytes_reserved=5,
        maximum_bytes=10,
        now=NOW,
    )
    assert not repository.reserve_claimed_parse_staging_bytes(
        task_id="parse-1",
        lease_token=token,
        allocation_key="zip",
        kind="result_zip",
        bytes_reserved=7,
        maximum_bytes=10,
        now="2026-09-05T00:02:00+00:00",
    )
    repository.update_parse_task("parse-1", {"lease_token": None, "updated_at": NOW})
    assert not repository.reserve_claimed_parse_staging_bytes(
        task_id="parse-1",
        lease_token=token,
        allocation_key="zip",
        kind="result_zip",
        bytes_reserved=7,
        maximum_bytes=10,
        now=NOW,
    )
