"""Upload-operation persistence for document ingestion."""

from __future__ import annotations

from typing import Any

from app.api_server.database.connection import SQLiteConnectionFactory
from app.api_server.database.models import Document, KnowledgeBase, UploadOperation


class UploadOperationRepositoryMixin:
    """Idempotent file-write state and its associated content revision update."""

    factory: SQLiteConnectionFactory

    def get_upload(
        self, knowledge_base_id: str, idempotency_key: str
    ) -> dict[str, Any] | None:
        with self.factory.session_scope() as session:
            item = session.get(
                UploadOperation,
                {
                    "knowledge_base_id": knowledge_base_id,
                    "idempotency_key": idempotency_key,
                },
            )
            return _upload_dict(item) if item is not None else None

    def start_upload(
        self,
        knowledge_base_id: str,
        idempotency_key: str,
        request_sha256: str,
        document_id: str,
        now: str,
    ) -> dict[str, Any]:
        with self.factory.session_scope(write=True) as session:
            item = session.get(
                UploadOperation,
                {
                    "knowledge_base_id": knowledge_base_id,
                    "idempotency_key": idempotency_key,
                },
            )
            if item is None:
                item = UploadOperation(
                    knowledge_base_id=knowledge_base_id,
                    idempotency_key=idempotency_key,
                    request_sha256=request_sha256,
                    document_id=document_id,
                    status="started",
                    created_at=now,
                    updated_at=now,
                )
                session.add(item)
            return _upload_dict(item)

    def mark_upload_files_committed(
        self,
        knowledge_base_id: str,
        idempotency_key: str,
        source_relpath: str,
        artifact_relpath: str,
        source_sha256: str,
        artifact_sha256: str,
        now: str,
    ) -> None:
        with self.factory.session_scope(write=True) as session:
            operation = session.get(
                UploadOperation,
                {
                    "knowledge_base_id": knowledge_base_id,
                    "idempotency_key": idempotency_key,
                },
            )
            if operation is None:
                raise KeyError((knowledge_base_id, idempotency_key))
            if operation.status == "committed":
                return
            operation.source_relpath = source_relpath
            operation.artifact_relpath = artifact_relpath
            operation.source_sha256 = source_sha256
            operation.artifact_sha256 = artifact_sha256
            operation.status = "files_committed"
            operation.updated_at = now

    def commit_upload(
        self,
        knowledge_base_id: str,
        idempotency_key: str,
        document_count: int,
        now: str,
    ) -> int:
        with self.factory.session_scope(write=True) as session:
            knowledge_base = session.get(KnowledgeBase, knowledge_base_id)
            if knowledge_base is None:
                raise KeyError(knowledge_base_id)
            operation = session.get(
                UploadOperation,
                {
                    "knowledge_base_id": knowledge_base_id,
                    "idempotency_key": idempotency_key,
                },
            )
            if operation is None:
                raise KeyError((knowledge_base_id, idempotency_key))
            if operation.status == "committed":
                document = session.get(Document, operation.document_id)
                if document is not None and document.parsed_content_version is not None:
                    return int(document.parsed_content_version)
                return knowledge_base.content_version
            if operation.status != "files_committed":
                raise ValueError(f"上传 operation 当前状态不可提交: {operation.status}")
            content_version = knowledge_base.content_version + 1
            knowledge_base.document_count = document_count
            knowledge_base.content_version = content_version
            knowledge_base.fts_status = "pending"
            knowledge_base.fts_target_revision = content_version
            if knowledge_base.vector_status != "disabled":
                knowledge_base.vector_status = "pending"
                knowledge_base.vector_target_revision = content_version
                knowledge_base.vector_progress_stage = "queued"
                knowledge_base.vector_documents_total = document_count
                knowledge_base.vector_documents_completed = 0
                knowledge_base.vector_records_processed = 0
            knowledge_base.updated_at = now
            document = session.get(Document, operation.document_id)
            if document is not None:
                document.parsed_content_version = content_version
                document.fts_indexed_version = None
                document.vector_indexed_version = None
                document.updated_at = now
            operation.status = "committed"
            operation.updated_at = now
            return content_version

    def fail_upload(
        self, knowledge_base_id: str, idempotency_key: str, error_message: str, now: str
    ) -> None:
        with self.factory.session_scope(write=True) as session:
            operation = session.get(
                UploadOperation,
                {
                    "knowledge_base_id": knowledge_base_id,
                    "idempotency_key": idempotency_key,
                },
            )
            if operation is None:
                raise KeyError((knowledge_base_id, idempotency_key))
            operation.status = "failed"
            operation.error_message = error_message[:2_000]
            operation.updated_at = now


def _upload_dict(item: UploadOperation) -> dict[str, Any]:
    return {
        "knowledge_base_id": item.knowledge_base_id,
        "idempotency_key": item.idempotency_key,
        "request_sha256": item.request_sha256,
        "document_id": item.document_id,
        "source_relpath": item.source_relpath,
        "artifact_relpath": item.artifact_relpath,
        "source_sha256": item.source_sha256,
        "artifact_sha256": item.artifact_sha256,
        "status": item.status,
        "error_message": item.error_message,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
    }
