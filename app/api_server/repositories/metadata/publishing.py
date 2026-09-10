"""Revision publication and workspace-revision persistence (§11.2)."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import delete, select, update

from app.api_server.database.connection import SQLiteConnectionFactory
from app.api_server.database.models import (
    Document,
    DocumentArtifact,
    DocumentEnrichmentTask,
    DocumentNormalizationTask,
    DocumentParseTask,
    DocumentStagingAllocation,
    KnowledgeBase,
    KnowledgeBaseWorkspaceRevision,
    UploadOperation,
)


def _revision_dict(item: KnowledgeBaseWorkspaceRevision) -> dict[str, Any]:
    return {
        "id": item.id,
        "knowledge_base_id": item.knowledge_base_id,
        "content_version": item.content_version,
        "snapshot_relpath": item.snapshot_relpath,
        "manifest_sha256": item.manifest_sha256,
        "state": item.state,
        "created_at": item.created_at,
    }


class WorkspaceRevisionRepositoryMixin:
    """Immutable snapshot revisions backing the knowledge-base visible view."""

    factory: SQLiteConnectionFactory

    def get_revision(
        self, knowledge_base_id: str, content_version: int
    ) -> dict[str, Any] | None:
        with self.factory.session_scope() as session:
            item = session.scalar(
                select(KnowledgeBaseWorkspaceRevision).where(
                    KnowledgeBaseWorkspaceRevision.knowledge_base_id
                    == knowledge_base_id,
                    KnowledgeBaseWorkspaceRevision.content_version == content_version,
                )
            )
            return _revision_dict(item) if item is not None else None

    def list_revisions(self, knowledge_base_id: str) -> list[dict[str, Any]]:
        with self.factory.session_scope() as session:
            items = session.scalars(
                select(KnowledgeBaseWorkspaceRevision)
                .where(
                    KnowledgeBaseWorkspaceRevision.knowledge_base_id
                    == knowledge_base_id
                )
                .order_by(KnowledgeBaseWorkspaceRevision.content_version.desc())
            ).all()
            return [_revision_dict(item) for item in items]

    def delete_superseded_revision(
        self, knowledge_base_id: str, content_version: int
    ) -> bool:
        with self.factory.session_scope(write=True) as session:
            item = session.scalar(
                select(KnowledgeBaseWorkspaceRevision).where(
                    KnowledgeBaseWorkspaceRevision.knowledge_base_id
                    == knowledge_base_id,
                    KnowledgeBaseWorkspaceRevision.content_version == content_version,
                    KnowledgeBaseWorkspaceRevision.state == "superseded",
                )
            )
            if item is None:
                return False
            session.delete(item)
            return True

    def get_committed_revision(
        self, knowledge_base_id: str, content_version: int
    ) -> dict[str, Any] | None:
        with self.factory.session_scope() as session:
            item = session.scalar(
                select(KnowledgeBaseWorkspaceRevision).where(
                    KnowledgeBaseWorkspaceRevision.knowledge_base_id
                    == knowledge_base_id,
                    KnowledgeBaseWorkspaceRevision.content_version == content_version,
                    KnowledgeBaseWorkspaceRevision.state == "committed",
                )
            )
            return _revision_dict(item) if item is not None else None

    def initialize_kb_revision(
        self,
        knowledge_base_id: str,
        content_version: int,
        snapshot_relpath: str,
        manifest_sha256: str,
        now: str,
    ) -> bool:
        """Insert the very first revision for a KB without advancing anything.

        Returns ``False`` when the KB's ``content_version`` moved concurrently
        (the staged snapshot then becomes an orphan and is cleaned up later).
        """
        with self.factory.session_scope(write=True) as session:
            knowledge_base = session.get(KnowledgeBase, knowledge_base_id)
            if knowledge_base is None:
                return False
            if knowledge_base.content_version != content_version:
                return False
            existing = session.scalar(
                select(KnowledgeBaseWorkspaceRevision).where(
                    KnowledgeBaseWorkspaceRevision.knowledge_base_id
                    == knowledge_base_id,
                    KnowledgeBaseWorkspaceRevision.content_version == content_version,
                )
            )
            if existing is not None:
                return bool(
                    existing.state == "committed"
                    and existing.snapshot_relpath == snapshot_relpath
                    and existing.manifest_sha256 == manifest_sha256
                )
            session.add(
                KnowledgeBaseWorkspaceRevision(
                    id=str(uuid.uuid4()),
                    knowledge_base_id=knowledge_base_id,
                    content_version=content_version,
                    snapshot_relpath=snapshot_relpath,
                    manifest_sha256=manifest_sha256,
                    state="committed",
                    created_at=now,
                )
            )
            return True

    def publish_document_revision(
        self,
        *,
        knowledge_base_id: str,
        document_id: str,
        document_count: int,
        expected_content_version: int,
        next_content_version: int,
        snapshot_relpath: str,
        manifest_sha256: str,
        artifacts: list[dict[str, Any]],
        parsed_markdown_relpath: str,
        generation: int,
        idempotency_key: str | None,
        fts_collection: str,
        index_statuses: tuple[bool, bool],
        now: str,
        document_values: dict[str, Any] | None = None,
        enrichment_completion: dict[str, Any] | None = None,
    ) -> int:
        """Commit one published document as revision ``next_content_version``.

        §11.2 步骤 7（不可拆分）：revision 行插入、前一条 superseded、文档
        artifact 指针与 ``parsed_content_version``、KB ``content_version`` CAS、
        索引 pending、``upload_operations -> committed`` 全部在同一事务中完成。
        抛出 :class:`PublishConflictError` 表示 CAS 失败（调用方基于最新
        snapshot 重新发布），事务回滚、状态保持原样。
        """
        fts_enabled, vector_enabled = index_statuses
        with self.factory.session_scope(write=True) as session:
            knowledge_base = session.get(KnowledgeBase, knowledge_base_id)
            if knowledge_base is None:
                raise KeyError(knowledge_base_id)
            if knowledge_base.content_version != expected_content_version:
                raise PublishConflictError(
                    f"content_version 已推进: {knowledge_base.content_version}"
                    f" != {expected_content_version}"
                )
            document = session.get(Document, document_id)
            if document is None and document_values is not None:
                document = Document(**document_values)
                session.add(document)
                session.flush()
            if document is None or document.status == "deleted":
                raise PublishConflictError("文档已删除或不存在")
            if int(document.pipeline_generation) != generation:
                raise PublishConflictError("文档 pipeline generation 已推进")
            enrichment_task: DocumentEnrichmentTask | None = None
            if enrichment_completion is not None:
                enrichment_task = session.get(
                    DocumentEnrichmentTask, str(enrichment_completion["task_id"])
                )
                if (
                    enrichment_task is None
                    or enrichment_task.document_id != document_id
                    or enrichment_task.pipeline_generation != generation
                    or enrichment_task.state != "running"
                    or enrichment_task.lease_token
                    != str(enrichment_completion["lease_token"])
                    or enrichment_task.lease_expires_at is None
                    or enrichment_task.lease_expires_at <= now
                ):
                    raise PublishConflictError("增强任务租约已失效")
            existing = session.scalar(
                select(KnowledgeBaseWorkspaceRevision).where(
                    KnowledgeBaseWorkspaceRevision.knowledge_base_id
                    == knowledge_base_id,
                    KnowledgeBaseWorkspaceRevision.content_version
                    == next_content_version,
                )
            )
            if existing is not None:
                raise PublishConflictError(f"revision {next_content_version} 已存在")
            previous = session.scalar(
                select(KnowledgeBaseWorkspaceRevision).where(
                    KnowledgeBaseWorkspaceRevision.knowledge_base_id
                    == knowledge_base_id,
                    KnowledgeBaseWorkspaceRevision.content_version
                    == expected_content_version,
                    KnowledgeBaseWorkspaceRevision.state == "committed",
                )
            )
            if previous is None:
                raise PublishConflictError("知识库当前 committed revision 不存在")
            previous.state = "superseded"
            session.flush()
            session.add(
                KnowledgeBaseWorkspaceRevision(
                    id=str(uuid.uuid4()),
                    knowledge_base_id=knowledge_base_id,
                    content_version=next_content_version,
                    snapshot_relpath=snapshot_relpath,
                    manifest_sha256=manifest_sha256,
                    state="committed",
                    created_at=now,
                )
            )
            for values in artifacts:
                item = session.scalar(
                    select(DocumentArtifact).where(
                        DocumentArtifact.document_id == document_id,
                        DocumentArtifact.kind == str(values["kind"]),
                        DocumentArtifact.relpath == str(values["relpath"]),
                    )
                )
                if item is None:
                    item = DocumentArtifact(
                        id=str(uuid.uuid4()),
                        document_id=document_id,
                        kind=str(values["kind"]),
                        relpath=str(values["relpath"]),
                        mime_type=str(values["mime_type"]),
                        size_bytes=int(values["size_bytes"]),
                        sha256=str(values["sha256"]),
                        pipeline_generation=generation,
                        created_at=now,
                    )
                    session.add(item)
                else:
                    item.mime_type = str(values["mime_type"])
                    item.size_bytes = int(values["size_bytes"])
                    item.sha256 = str(values["sha256"])
                    item.pipeline_generation = generation
            document.pipeline_generation = generation
            document.parsed_markdown_relpath = parsed_markdown_relpath
            document.parsed_content_version = next_content_version
            document.fts_indexed_version = None
            document.vector_indexed_version = None
            document.status = "ready"
            document.error_code = None
            document.error_message = None
            document.updated_at = now
            document.completed_at = now
            session.execute(
                update(DocumentStagingAllocation)
                .where(
                    DocumentStagingAllocation.document_id == document_id,
                    DocumentStagingAllocation.pipeline_generation == generation,
                    DocumentStagingAllocation.state == "active",
                )
                .values(state="released", updated_at=now)
            )
            if enrichment_task is not None:
                completion_state = str(enrichment_completion["state"])
                if completion_state not in {"succeeded", "partial"}:
                    raise ValueError("增强完成状态无效")
                enrichment_task.state = completion_state
                enrichment_task.node_count = int(enrichment_completion["node_count"])
                enrichment_task.nodes_completed = int(
                    enrichment_completion["nodes_completed"]
                )
                enrichment_task.nodes_failed = int(
                    enrichment_completion["nodes_failed"]
                )
                enrichment_task.output_markdown_relpath = str(
                    enrichment_completion["output_markdown_relpath"]
                )
                enrichment_task.output_tree_relpath = str(
                    enrichment_completion["output_tree_relpath"]
                )
                enrichment_task.output_sha256 = str(
                    enrichment_completion["output_sha256"]
                )
                enrichment_task.warning_json = str(
                    enrichment_completion["warning_json"]
                )
                enrichment_task.lease_owner = None
                enrichment_task.lease_token = None
                enrichment_task.lease_expires_at = None
                enrichment_task.updated_at = now
                enrichment_task.completed_at = now
                document.stage_state = completion_state
                document.failed_stage = None
                document.warning_json = enrichment_task.warning_json
            knowledge_base.document_count = document_count
            knowledge_base.content_version = next_content_version
            if fts_enabled:
                knowledge_base.fts_status = "pending"
                knowledge_base.fts_target_revision = next_content_version
            if fts_enabled and fts_collection:
                knowledge_base.fts_collection = fts_collection
            if vector_enabled:
                knowledge_base.vector_status = "pending"
                knowledge_base.vector_target_revision = next_content_version
                knowledge_base.vector_progress_stage = "queued"
                knowledge_base.vector_documents_total = document_count
                knowledge_base.vector_documents_completed = 0
                knowledge_base.vector_records_processed = 0
            if fts_enabled or vector_enabled:
                document.status = "indexing"
                document.current_stage = "index"
                document.progress_completed = 0
                document.progress_total = 1
                document.progress_unit = "documents"
            else:
                document.status = "ready"
                document.current_stage = "complete"
                document.progress_completed = 1
                document.progress_total = 1
                document.progress_unit = "documents"
            knowledge_base.updated_at = now
            if idempotency_key is not None:
                operation = session.get(
                    UploadOperation,
                    {
                        "knowledge_base_id": knowledge_base_id,
                        "idempotency_key": idempotency_key,
                    },
                )
                if operation is None:
                    raise PublishConflictError("上传 operation 不存在")
                if operation.status not in {"files_committed", "committed"}:
                    raise PublishConflictError(
                        f"上传 operation 当前状态不可发布: {operation.status}"
                    )
                if operation.status != "committed":
                    operation.status = "committed"
                    operation.updated_at = now
            return next_content_version

    def mark_knowledge_base_unavailable(
        self, knowledge_base_id: str, error_message: str, now: str
    ) -> None:
        """Startup reconciliation: committed snapshot broke; refuse runtimes."""
        del error_message  # surfaced in logs by the caller; status drives gating.
        with self.factory.session_scope(write=True) as session:
            knowledge_base = session.get(KnowledgeBase, knowledge_base_id)
            if knowledge_base is None:
                return
            knowledge_base.status = "error"
            knowledge_base.updated_at = now

    def publish_document_deletion_revision(
        self,
        *,
        knowledge_base_id: str,
        document_id: str,
        expected_content_version: int,
        next_content_version: int,
        snapshot_relpath: str,
        manifest_sha256: str,
        document_count: int,
        index_statuses: tuple[bool, bool],
        now: str,
    ) -> int:
        """Atomically publish a snapshot tombstone and fence active stage tasks."""
        fts_enabled, vector_enabled = index_statuses
        with self.factory.session_scope(write=True) as session:
            knowledge_base = session.get(KnowledgeBase, knowledge_base_id)
            if knowledge_base is None:
                raise KeyError(knowledge_base_id)
            if knowledge_base.content_version != expected_content_version:
                raise PublishConflictError(
                    f"content_version 已推进: {knowledge_base.content_version}"
                    f" != {expected_content_version}"
                )
            document = session.get(Document, document_id)
            if document is None or document.knowledge_base_id != knowledge_base_id:
                raise PublishConflictError("文档已删除或不存在")
            previous = session.scalar(
                select(KnowledgeBaseWorkspaceRevision).where(
                    KnowledgeBaseWorkspaceRevision.knowledge_base_id
                    == knowledge_base_id,
                    KnowledgeBaseWorkspaceRevision.content_version
                    == expected_content_version,
                    KnowledgeBaseWorkspaceRevision.state == "committed",
                )
            )
            if previous is None:
                raise PublishConflictError("知识库当前 committed revision 不存在")
            previous.state = "superseded"
            session.flush()
            session.add(
                KnowledgeBaseWorkspaceRevision(
                    id=str(uuid.uuid4()),
                    knowledge_base_id=knowledge_base_id,
                    content_version=next_content_version,
                    snapshot_relpath=snapshot_relpath,
                    manifest_sha256=manifest_sha256,
                    state="committed",
                    created_at=now,
                )
            )
            cancellation = {
                "lease_owner": None,
                "lease_token": None,
                "lease_expires_at": None,
                "error_code": "DOCUMENT_DELETED",
                "error_message": "文档已删除",
                "updated_at": now,
                "completed_at": now,
            }
            session.execute(
                update(DocumentParseTask)
                .where(
                    DocumentParseTask.document_id == document_id,
                    DocumentParseTask.state.in_(
                        (
                            "created",
                            "uploading",
                            "waiting-file",
                            "pending",
                            "running",
                            "converting",
                        )
                    ),
                )
                .values(state="canceled", dispatch_state="canceled", **cancellation)
            )
            for task_model in (DocumentNormalizationTask, DocumentEnrichmentTask):
                session.execute(
                    update(task_model)
                    .where(
                        task_model.document_id == document_id,
                        task_model.state.in_(("queued", "running")),
                    )
                    .values(state="canceled", **cancellation)
                )
            session.execute(
                delete(UploadOperation).where(
                    UploadOperation.knowledge_base_id == knowledge_base_id,
                    UploadOperation.document_id == document_id,
                )
            )
            document.status = "deleted"
            document.current_stage = "complete"
            document.stage_state = "canceled"
            document.failed_stage = None
            document.progress_completed = None
            document.progress_total = None
            document.progress_unit = None
            document.deleted_content_version = next_content_version
            document.fts_indexed_version = (
                None
                if fts_enabled and document.parsed_content_version is not None
                else next_content_version
            )
            document.vector_indexed_version = (
                None
                if vector_enabled and document.parsed_content_version is not None
                else next_content_version
            )
            document.error_code = None
            document.error_message = None
            document.updated_at = now
            document.completed_at = now
            session.execute(
                update(DocumentStagingAllocation)
                .where(
                    DocumentStagingAllocation.document_id == document_id,
                    DocumentStagingAllocation.state == "active",
                )
                .values(state="released", updated_at=now)
            )
            knowledge_base.document_count = document_count
            knowledge_base.content_version = next_content_version
            if fts_enabled:
                knowledge_base.fts_status = "pending"
                knowledge_base.fts_target_revision = next_content_version
            if vector_enabled:
                knowledge_base.vector_status = "pending"
                knowledge_base.vector_target_revision = next_content_version
                knowledge_base.vector_progress_stage = "queued"
                knowledge_base.vector_documents_total = document_count
                knowledge_base.vector_documents_completed = 0
                knowledge_base.vector_records_processed = 0
            knowledge_base.updated_at = now
            return next_content_version


class PublishConflictError(Exception):
    """Revision CAS failed; the caller must re-publish from the latest state."""
