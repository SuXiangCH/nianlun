"""Document parse-task and parsed-artifact persistence."""

from __future__ import annotations

import uuid
from typing import Any, cast

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
        "pipeline_generation": item.pipeline_generation,
        "chunk_index": item.chunk_index,
        "chunk_count": item.chunk_count,
        "source_page_start": item.source_page_start,
        "source_page_end": item.source_page_end,
        "chunk_source_relpath": item.chunk_source_relpath,
        "result_root_relpath": item.result_root_relpath,
        "input_sha256": item.input_sha256,
        "output_sha256": item.output_sha256,
        "data_id": item.data_id,
        "batch_id": item.batch_id,
        "task_id": item.task_id,
        "model_version": item.model_version,
        "request_json": item.request_json,
        "state": item.state,
        "dispatch_state": item.dispatch_state,
        "available_at": item.available_at,
        "next_poll_at": item.next_poll_at,
        "lease_owner": item.lease_owner,
        "lease_expires_at": item.lease_expires_at,
        "lease_token": item.lease_token,
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
        "pipeline_generation": item.pipeline_generation,
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
                pipeline_generation=int(values.get("pipeline_generation", 1)),
                chunk_index=int(values.get("chunk_index", 0)),
                chunk_count=int(values.get("chunk_count", 1)),
                source_page_start=values.get("source_page_start"),
                source_page_end=values.get("source_page_end"),
                chunk_source_relpath=values.get("chunk_source_relpath"),
                result_root_relpath=values.get("result_root_relpath"),
                input_sha256=values.get("input_sha256"),
                output_sha256=values.get("output_sha256"),
                data_id=str(values["data_id"]),
                batch_id=values.get("batch_id"),
                task_id=values.get("task_id"),
                model_version=str(values["model_version"]),
                request_json=str(values.get("request_json", "{}")),
                state=str(values.get("state", "created")),
                dispatch_state=str(values.get("dispatch_state", "queued")),
                available_at=str(values.get("available_at", values["created_at"])),
                next_poll_at=values.get("next_poll_at"),
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
            attempts = session.scalars(
                select(DocumentParseTask)
                .where(
                    DocumentParseTask.document_id == document_id,
                    DocumentParseTask.pipeline_generation
                    == document.pipeline_generation,
                )
                .order_by(
                    DocumentParseTask.chunk_index,
                    DocumentParseTask.attempt.desc(),
                )
            ).all()
            latest_by_chunk: dict[int, DocumentParseTask] = {}
            for item in attempts:
                latest_by_chunk.setdefault(item.chunk_index, item)
            if any(
                item.state in _ACTIVE_PARSE_STATES for item in latest_by_chunk.values()
            ):
                raise ValueError("文档解析任务正在处理中")
            failed = [
                item for item in latest_by_chunk.values() if item.state == "failed"
            ]
            if not failed:
                raise ValueError("没有失败的解析分段可重试")
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
            created_tasks: list[DocumentParseTask] = []
            for latest in sorted(failed, key=lambda item: item.chunk_index):
                task = DocumentParseTask(
                    id=str(uuid.uuid4()),
                    document_id=document_id,
                    provider="mineru",
                    api_mode=latest.api_mode or api_mode,
                    attempt=latest.attempt + 1,
                    pipeline_generation=document.pipeline_generation,
                    chunk_index=latest.chunk_index,
                    chunk_count=latest.chunk_count,
                    source_page_start=latest.source_page_start,
                    source_page_end=latest.source_page_end,
                    chunk_source_relpath=latest.chunk_source_relpath,
                    input_sha256=latest.input_sha256,
                    data_id=latest.data_id,
                    model_version=latest.model_version or model_version,
                    request_json=latest.request_json or request_json,
                    state="created",
                    dispatch_state="queued",
                    available_at=now,
                    created_at=now,
                    updated_at=now,
                )
                session.add(task)
                created_tasks.append(task)
            document.status = "parsing"
            document.current_stage = "parse"
            document.stage_state = "queued"
            document.failed_stage = None
            document.error_code = None
            document.error_message = None
            document.updated_at = now
            document.completed_at = None
            session.flush()
            return _parse_task_dict(created_tasks[0])

    def claim_parse_task(
        self,
        task_id: str,
        *,
        owner: str,
        mode: str,
        now: str,
        lease_expires_at: str,
    ) -> dict[str, Any] | None:
        """Lease one due submit/poll unit and return its fencing token."""
        if mode not in {"submit", "poll"}:
            raise ValueError("解析任务领取模式无效")
        with self.factory.session_scope(write=True) as session:
            item = session.get(DocumentParseTask, task_id)
            if item is None or item.state not in _ACTIVE_PARSE_STATES:
                return None
            document = session.get(Document, item.document_id)
            if (
                document is None
                or document.pipeline_generation != item.pipeline_generation
                or document.status == "deleted"
            ):
                return None
            has_remote_id = bool(item.batch_id or item.task_id)
            if (mode == "submit" and has_remote_id) or (
                mode == "poll" and not has_remote_id
            ):
                return None
            due_at = item.available_at if mode == "submit" else item.next_poll_at
            if due_at is not None and due_at > now:
                return None
            if item.lease_token is not None and (
                item.lease_expires_at is None or item.lease_expires_at > now
            ):
                return None
            item.lease_owner = owner
            item.lease_token = str(uuid.uuid4())
            item.lease_expires_at = lease_expires_at
            item.dispatch_state = "leased"
            item.updated_at = now
            if item.started_at is None:
                item.started_at = now
            session.flush()
            return _parse_task_dict(item)

    def update_claimed_parse_task(
        self,
        task_id: str,
        lease_token: str,
        values: dict[str, Any],
        *,
        release: bool = False,
    ) -> bool:
        with self.factory.session_scope(write=True) as session:
            item = session.get(DocumentParseTask, task_id)
            now = str(values.get("updated_at") or "")
            if (
                item is None
                or item.lease_token != lease_token
                or item.state not in _ACTIVE_PARSE_STATES
                or not now
                or item.lease_expires_at is None
                or item.lease_expires_at <= now
            ):
                return False
            for field in (
                "batch_id",
                "task_id",
                "state",
                "dispatch_state",
                "available_at",
                "next_poll_at",
                "result_root_relpath",
                "output_sha256",
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
            document = session.get(Document, item.document_id)
            if (
                document is not None
                and document.pipeline_generation == item.pipeline_generation
                and document.current_stage == "parse"
            ):
                tasks = session.scalars(
                    select(DocumentParseTask)
                    .where(
                        DocumentParseTask.document_id == item.document_id,
                        DocumentParseTask.pipeline_generation
                        == item.pipeline_generation,
                    )
                    .order_by(
                        DocumentParseTask.chunk_index,
                        DocumentParseTask.attempt.desc(),
                    )
                ).all()
                latest_by_chunk: dict[int, DocumentParseTask] = {}
                for parse_task in tasks:
                    latest_by_chunk.setdefault(parse_task.chunk_index, parse_task)
                has_page_ranges = bool(latest_by_chunk) and all(
                    parse_task.source_page_start is not None
                    and parse_task.source_page_end is not None
                    for parse_task in latest_by_chunk.values()
                )
                if has_page_ranges:
                    page_counts = {
                        chunk_index: cast(int, parse_task.source_page_end)
                        - cast(int, parse_task.source_page_start)
                        + 1
                        for chunk_index, parse_task in latest_by_chunk.items()
                    }
                    document.progress_completed = sum(
                        page_counts[chunk_index]
                        for chunk_index, parse_task in latest_by_chunk.items()
                        if parse_task.state == "done"
                    )
                    document.progress_total = sum(page_counts.values())
                    document.progress_unit = "pages"
                else:
                    document.progress_completed = sum(
                        parse_task.state == "done"
                        for parse_task in latest_by_chunk.values()
                    )
                    document.progress_total = max(
                        (
                            parse_task.chunk_count
                            for parse_task in latest_by_chunk.values()
                        ),
                        default=0,
                    )
                    document.progress_unit = "chunks"
                document.updated_at = str(values.get("updated_at") or item.updated_at)
            if release:
                item.lease_owner = None
                item.lease_token = None
                item.lease_expires_at = None
            return True

    def renew_claimed_parse_task_lease(
        self,
        task_id: str,
        lease_token: str,
        *,
        lease_expires_at: str,
        now: str,
    ) -> bool:
        """Keep a claimed MinerU task fenced while result I/O is in progress."""
        with self.factory.session_scope(write=True) as session:
            item = session.get(DocumentParseTask, task_id)
            if (
                item is None
                or item.lease_token != lease_token
                or item.state not in _ACTIVE_PARSE_STATES
                or item.lease_expires_at is None
                or item.lease_expires_at <= now
            ):
                return False
            document = session.get(Document, item.document_id)
            if (
                document is None
                or document.pipeline_generation != item.pipeline_generation
                or document.status == "deleted"
            ):
                return False
            item.lease_expires_at = lease_expires_at
            item.updated_at = now
            return True

    def fail_claimed_parse_task(
        self,
        task_id: str,
        lease_token: str,
        *,
        error_code: str | None,
        error_message: str,
        now: str,
    ) -> bool:
        with self.factory.session_scope(write=True) as session:
            item = session.get(DocumentParseTask, task_id)
            if (
                item is None
                or item.lease_token != lease_token
                or item.lease_expires_at is None
                or item.lease_expires_at <= now
            ):
                return False
            item.state = "failed"
            item.dispatch_state = "failed"
            item.error_code = error_code
            item.error_message = error_message[:2_000]
            item.updated_at = now
            item.completed_at = now
            item.lease_owner = None
            item.lease_token = None
            item.lease_expires_at = None
            document = session.get(Document, item.document_id)
            if (
                document is not None
                and document.pipeline_generation == item.pipeline_generation
            ):
                document.status = "failed"
                document.current_stage = "parse"
                document.stage_state = "failed"
                document.failed_stage = "parse"
                document.error_code = error_code
                document.error_message = error_message[:2_000]
                document.updated_at = now
                document.completed_at = now
            return True

    def fail_document_parse_preflight(
        self,
        document_id: str,
        pipeline_generation: int,
        *,
        error_code: str | None,
        error_message: str,
        now: str,
    ) -> bool:
        with self.factory.session_scope(write=True) as session:
            document = session.get(Document, document_id)
            if (
                document is None
                or document.pipeline_generation != pipeline_generation
                or document.status == "deleted"
            ):
                return False
            document.status = "failed"
            document.current_stage = "parse"
            document.stage_state = "failed"
            document.failed_stage = "parse"
            document.error_code = error_code
            document.error_message = error_message[:2_000]
            document.updated_at = now
            document.completed_at = now
            return True

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

    def list_parse_tasks(
        self, document_id: str, pipeline_generation: int | None = None
    ) -> list[dict[str, Any]]:
        with self.factory.session_scope() as session:
            query = select(DocumentParseTask).where(
                DocumentParseTask.document_id == document_id
            )
            if pipeline_generation is not None:
                query = query.where(
                    DocumentParseTask.pipeline_generation == pipeline_generation
                )
            items = session.scalars(
                query.order_by(
                    DocumentParseTask.chunk_index, DocumentParseTask.attempt.desc()
                )
            ).all()
            return [_parse_task_dict(item) for item in items]

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
                "dispatch_state",
                "available_at",
                "next_poll_at",
                "lease_owner",
                "lease_expires_at",
                "lease_token",
                "result_root_relpath",
                "output_sha256",
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
                    pipeline_generation=int(values.get("pipeline_generation", 1)),
                    created_at=str(values["created_at"]),
                )
                session.add(item)
            else:
                item.mime_type = str(values["mime_type"])
                item.size_bytes = int(values["size_bytes"])
                item.sha256 = str(values["sha256"])
                item.pipeline_generation = int(
                    values.get("pipeline_generation", item.pipeline_generation)
                )
            session.flush()
            return _artifact_dict(item)
