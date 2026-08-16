"""Document parse-task and parsed-artifact persistence."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select

from app.api_server.database.connection import SQLiteConnectionFactory
from app.api_server.database.models import (
    Document,
    DocumentArtifact,
    DocumentParseTask,
    UploadOperation,
)


_ACTIVE_PARSE_STATES = (
    "created",
    "uploading",
    "waiting-file",
    "pending",
    "running",
    "converting",
)


def _parse_task_dict(item: DocumentParseTask) -> dict[str, Any]:
    return {
        "id": item.id,
        "document_id": item.document_id,
        "provider": item.provider,
        "api_mode": "saas_precision" if item.api_mode == "precision" else item.api_mode,
        "attempt": item.attempt,
        "data_id": item.data_id,
        "batch_id": item.batch_id,
        "task_id": item.task_id,
        "model_version": item.model_version,
        "request_json": item.request_json,
        "state": item.state,
        "extracted_pages": item.extracted_pages,
        "total_pages": item.total_pages,
        "result_zip_url": item.result_zip_url,
        "error_code": item.error_code,
        "error_message": item.error_message,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
        "started_at": item.started_at,
        "completed_at": item.completed_at,
    }


def _artifact_dict(item: DocumentArtifact) -> dict[str, Any]:
    return {
        "id": item.id,
        "document_id": item.document_id,
        "kind": item.kind,
        "relpath": item.relpath,
        "mime_type": item.mime_type,
        "size_bytes": item.size_bytes,
        "sha256": item.sha256,
        "created_at": item.created_at,
    }


class DocumentParseRepositoryMixin:
    """Persist MinerU task attempts and their generated workspace artifacts."""

    factory: SQLiteConnectionFactory

    def create_parse_task(self, values: dict[str, Any]) -> dict[str, Any]:
        with self.factory.session_scope(write=True) as session:
            task = DocumentParseTask(
                id=str(values["id"]),
                document_id=str(values["document_id"]),
                provider=str(values.get("provider", "mineru")),
                api_mode=str(values.get("api_mode", "saas_precision")),
                attempt=int(values["attempt"]),
                data_id=str(values["data_id"]),
                batch_id=values.get("batch_id"),
                task_id=values.get("task_id"),
                model_version=str(values["model_version"]),
                request_json=str(values.get("request_json", "{}")),
                state=str(values.get("state", "created")),
                created_at=str(values["created_at"]),
                updated_at=str(values["updated_at"]),
            )
            session.add(task)
            session.flush()
            return _parse_task_dict(task)

    def retry_parse_task(
        self,
        document_id: str,
        api_mode: str,
        model_version: str,
        request_json: str,
        now: str,
    ) -> dict[str, Any]:
        with self.factory.session_scope(write=True) as session:
            document = session.get(Document, document_id)
            if document is None:
                raise KeyError(document_id)
            if document.parser != "mineru":
                raise ValueError("仅支持重试 PDF 或 Word 文档解析")
            if document.status != "failed":
                raise ValueError("只有解析失败的文档可以重试")
            latest = session.scalars(
                select(DocumentParseTask)
                .where(DocumentParseTask.document_id == document_id)
                .order_by(DocumentParseTask.attempt.desc())
                .limit(1)
            ).first()
            if latest is not None and latest.state in _ACTIVE_PARSE_STATES:
                raise ValueError("文档解析任务正在处理中")
            operation = session.scalars(
                select(UploadOperation)
                .where(UploadOperation.document_id == document_id)
                .order_by(UploadOperation.created_at.desc())
                .limit(1)
            ).first()
            if operation is None:
                raise ValueError("上传记录不存在，无法重试解析")
            if operation.status == "failed":
                operation.status = "files_committed"
                operation.error_message = None
            elif operation.status not in {"files_committed", "committed"}:
                raise ValueError("上传记录状态不允许重试解析")
            operation.updated_at = now
            task = DocumentParseTask(
                id=str(uuid.uuid4()),
                document_id=document_id,
                provider="mineru",
                api_mode=api_mode,
                attempt=(latest.attempt if latest is not None else 0) + 1,
                data_id=document_id,
                model_version=model_version,
                request_json=request_json,
                state="created",
                created_at=now,
                updated_at=now,
            )
            session.add(task)
            document.status = "parsing"
            document.error_code = None
            document.error_message = None
            document.updated_at = now
            document.completed_at = None
            session.flush()
            return _parse_task_dict(task)

    def get_parse_task(self, task_id: str) -> dict[str, Any] | None:
        with self.factory.session_scope() as session:
            item = session.get(DocumentParseTask, task_id)
            return _parse_task_dict(item) if item is not None else None

    def get_latest_parse_task(self, document_id: str) -> dict[str, Any] | None:
        with self.factory.session_scope() as session:
            item = session.scalars(
                select(DocumentParseTask)
                .where(DocumentParseTask.document_id == document_id)
                .order_by(DocumentParseTask.attempt.desc())
                .limit(1)
            ).first()
            return _parse_task_dict(item) if item is not None else None

    def list_active_parse_tasks(self) -> list[dict[str, Any]]:
        with self.factory.session_scope() as session:
            items = session.scalars(
                select(DocumentParseTask).where(
                    DocumentParseTask.state.in_(_ACTIVE_PARSE_STATES)
                )
            ).all()
            return [_parse_task_dict(item) for item in items]

    def update_parse_task(self, task_id: str, values: dict[str, Any]) -> None:
        with self.factory.session_scope(write=True) as session:
            item = session.get(DocumentParseTask, task_id)
            if item is None:
                raise KeyError(task_id)
            for field in (
                "batch_id",
                "task_id",
                "state",
                "extracted_pages",
                "total_pages",
                "result_zip_url",
                "error_code",
                "error_message",
                "updated_at",
                "started_at",
                "completed_at",
            ):
                if field in values:
                    setattr(item, field, values[field])

    def list_document_artifacts(self, document_id: str) -> list[dict[str, Any]]:
        with self.factory.session_scope() as session:
            items = session.scalars(
                select(DocumentArtifact)
                .where(DocumentArtifact.document_id == document_id)
                .order_by(DocumentArtifact.kind, DocumentArtifact.relpath)
            ).all()
            return [_artifact_dict(item) for item in items]

    def put_document_artifact(self, values: dict[str, Any]) -> dict[str, Any]:
        with self.factory.session_scope(write=True) as session:
            item = session.scalar(
                select(DocumentArtifact).where(
                    DocumentArtifact.document_id == str(values["document_id"]),
                    DocumentArtifact.kind == str(values["kind"]),
                    DocumentArtifact.relpath == str(values["relpath"]),
                )
            )
            if item is None:
                item = DocumentArtifact(
                    id=str(values.get("id") or uuid.uuid4()),
                    document_id=str(values["document_id"]),
                    kind=str(values["kind"]),
                    relpath=str(values["relpath"]),
                    mime_type=str(values["mime_type"]),
                    size_bytes=int(values["size_bytes"]),
                    sha256=str(values["sha256"]),
                    created_at=str(values["created_at"]),
                )
                session.add(item)
            else:
                item.mime_type = str(values["mime_type"])
                item.size_bytes = int(values["size_bytes"])
                item.sha256 = str(values["sha256"])
            session.flush()
            return _artifact_dict(item)
