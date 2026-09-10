"""Durable queue operations for normalize/enrich stages and pipeline telemetry."""

from __future__ import annotations

import json
import uuid
from typing import Any, TypeVar

from sqlalchemy import delete, func, or_, select, update

from app.api_server.database.connection import SQLiteConnectionFactory
from app.api_server.database.models import (
    Document,
    DocumentEnrichmentCallResult,
    DocumentEnrichmentTask,
    DocumentNormalizationTask,
    DocumentParseTask,
    DocumentPipelineEvent,
    DocumentStagingAllocation,
    UploadOperation,
)


TaskT = TypeVar("TaskT", DocumentNormalizationTask, DocumentEnrichmentTask)


def _task_dict(item: Any) -> dict[str, Any]:
    return {
        column.name: getattr(item, column.name) for column in item.__table__.columns
    }


def _event_dict(item: DocumentPipelineEvent) -> dict[str, Any]:
    return {
        column.name: getattr(item, column.name) for column in item.__table__.columns
    }


class DocumentPipelineRepositoryMixin:
    factory: SQLiteConnectionFactory

    def restart_parse_preparation(
        self,
        document_id: str,
        pipeline_generation: int,
        now: str,
    ) -> int:
        """Fence an incomplete parse generation and queue fresh preparation."""
        with self.factory.session_scope(write=True) as session:
            document = session.get(Document, document_id)
            if (
                document is None
                or document.pipeline_generation != pipeline_generation
                or document.status != "failed"
                or document.failed_stage != "parse"
                or document.parser != "mineru"
            ):
                raise ValueError("文档当前状态不允许重新准备解析任务")
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

            cancellation = {
                "lease_owner": None,
                "lease_token": None,
                "lease_expires_at": None,
                "error_code": "PIPELINE_RESTARTED",
                "error_message": "解析任务已在新 generation 重新准备",
                "updated_at": now,
                "completed_at": now,
            }
            session.execute(
                update(DocumentParseTask)
                .where(
                    DocumentParseTask.document_id == document_id,
                    DocumentParseTask.pipeline_generation == pipeline_generation,
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
                        task_model.pipeline_generation == pipeline_generation,
                        task_model.state.in_(("queued", "running")),
                    )
                    .values(state="canceled", **cancellation)
                )
            session.execute(
                update(DocumentStagingAllocation)
                .where(
                    DocumentStagingAllocation.document_id == document_id,
                    DocumentStagingAllocation.pipeline_generation
                    == pipeline_generation,
                    DocumentStagingAllocation.state == "active",
                )
                .values(state="released", updated_at=now)
            )

            next_generation = pipeline_generation + 1
            document.pipeline_generation = next_generation
            document.parse_plan_json = None
            document.status = "parsing"
            document.current_stage = "parse"
            document.stage_state = "queued"
            document.failed_stage = None
            document.progress_completed = 0
            document.progress_total = None
            document.progress_unit = None
            document.warning_json = "[]"
            document.error_code = None
            document.error_message = None
            document.updated_at = now
            document.completed_at = None
            session.add(
                DocumentPipelineEvent(
                    id=str(uuid.uuid4()),
                    document_id=document_id,
                    pipeline_generation=next_generation,
                    stage="parse",
                    event_type="retry_queued",
                    progress_completed=0,
                    progress_total=None,
                    message=(
                        f"pipeline generation {pipeline_generation} -> "
                        f"{next_generation}"
                    ),
                    created_at=now,
                )
            )
            session.flush()
            return next_generation

    def create_normalization_task(
        self,
        *,
        document_id: str,
        pipeline_generation: int,
        input_manifest_json: str,
        now: str,
    ) -> dict[str, Any]:
        with self.factory.session_scope(write=True) as session:
            document = session.get(Document, document_id)
            if (
                document is None
                or document.pipeline_generation != pipeline_generation
                or document.status == "deleted"
            ):
                raise ValueError("文档不存在、已删除或 generation 已变化")
            existing = session.scalars(
                select(DocumentNormalizationTask)
                .where(
                    DocumentNormalizationTask.document_id == document_id,
                    DocumentNormalizationTask.pipeline_generation
                    == pipeline_generation,
                    DocumentNormalizationTask.state.in_(
                        ("queued", "running", "succeeded")
                    ),
                )
                .order_by(DocumentNormalizationTask.attempt.desc())
                .limit(1)
            ).first()
            if existing is not None:
                return _task_dict(existing)
            last_attempt = session.scalar(
                select(func.max(DocumentNormalizationTask.attempt)).where(
                    DocumentNormalizationTask.document_id == document_id,
                    DocumentNormalizationTask.pipeline_generation
                    == pipeline_generation,
                )
            )
            item = DocumentNormalizationTask(
                id=str(uuid.uuid4()),
                document_id=document_id,
                pipeline_generation=pipeline_generation,
                attempt=int(last_attempt or 0) + 1,
                state="queued",
                available_at=now,
                input_manifest_json=input_manifest_json,
                created_at=now,
                updated_at=now,
            )
            session.add(item)
            document.status = "parsing"
            document.current_stage = "normalize"
            document.stage_state = "queued"
            manifest = json.loads(input_manifest_json)
            chunks = manifest.get("chunks", []) if isinstance(manifest, dict) else []
            document.progress_completed = 0
            document.progress_total = len(chunks) if isinstance(chunks, list) else 0
            document.progress_unit = "chunks"
            document.updated_at = now
            session.flush()
            return _task_dict(item)

    def create_enrichment_task(
        self,
        *,
        document_id: str,
        pipeline_generation: int,
        input_sha256: str,
        llm_profile_id: str | None,
        llm_profile_updated_at: str | None,
        prompt_schema_version: int,
        options_json: str,
        now: str,
    ) -> dict[str, Any]:
        with self.factory.session_scope(write=True) as session:
            document = session.get(Document, document_id)
            if (
                document is None
                or document.pipeline_generation != pipeline_generation
                or document.status == "deleted"
            ):
                raise ValueError("文档不存在、已删除或 generation 已变化")
            existing = session.scalars(
                select(DocumentEnrichmentTask)
                .where(
                    DocumentEnrichmentTask.document_id == document_id,
                    DocumentEnrichmentTask.pipeline_generation == pipeline_generation,
                    DocumentEnrichmentTask.state.in_(
                        ("queued", "running", "succeeded", "partial")
                    ),
                )
                .order_by(DocumentEnrichmentTask.attempt.desc())
                .limit(1)
            ).first()
            if existing is not None:
                return _task_dict(existing)
            last_attempt = session.scalar(
                select(func.max(DocumentEnrichmentTask.attempt)).where(
                    DocumentEnrichmentTask.document_id == document_id,
                    DocumentEnrichmentTask.pipeline_generation == pipeline_generation,
                )
            )
            item = DocumentEnrichmentTask(
                id=str(uuid.uuid4()),
                document_id=document_id,
                pipeline_generation=pipeline_generation,
                attempt=int(last_attempt or 0) + 1,
                state="queued",
                available_at=now,
                input_sha256=input_sha256,
                llm_profile_id=llm_profile_id,
                llm_profile_updated_at=llm_profile_updated_at,
                prompt_schema_version=prompt_schema_version,
                options_json=options_json,
                created_at=now,
                updated_at=now,
            )
            session.add(item)
            document.status = "parsed"
            document.current_stage = "enrich"
            document.stage_state = "queued"
            document.failed_stage = None
            document.progress_completed = 0
            document.progress_total = None
            document.progress_unit = "nodes"
            document.updated_at = now
            session.flush()
            return _task_dict(item)

    def retry_failed_pipeline_task(
        self,
        document_id: str,
        pipeline_generation: int,
        stage: str,
        now: str,
    ) -> dict[str, Any]:
        if stage not in {"normalize", "enrich"}:
            raise ValueError("不支持重试该流水线阶段")
        with self.factory.session_scope(write=True) as session:
            normalization_chunk_count: int | None = None
            document = session.get(Document, document_id)
            if (
                document is None
                or document.pipeline_generation != pipeline_generation
                or document.status != "failed"
                or document.failed_stage != stage
            ):
                raise ValueError("文档当前状态不允许重试该阶段")
            if stage == "normalize":
                latest = session.scalars(
                    select(DocumentNormalizationTask)
                    .where(
                        DocumentNormalizationTask.document_id == document_id,
                        DocumentNormalizationTask.pipeline_generation
                        == pipeline_generation,
                    )
                    .order_by(DocumentNormalizationTask.attempt.desc())
                    .limit(1)
                ).first()
                if latest is None or latest.state != "failed":
                    raise ValueError("没有失败的阶段任务可重试")
                manifest = json.loads(latest.input_manifest_json)
                chunks = (
                    manifest.get("chunks", []) if isinstance(manifest, dict) else []
                )
                normalization_chunk_count = (
                    len(chunks) if isinstance(chunks, list) else 0
                )
                item = DocumentNormalizationTask(
                    id=str(uuid.uuid4()),
                    document_id=document_id,
                    pipeline_generation=pipeline_generation,
                    attempt=latest.attempt + 1,
                    state="queued",
                    available_at=now,
                    input_manifest_json=latest.input_manifest_json,
                    created_at=now,
                    updated_at=now,
                )
            else:
                latest_enrichment = session.scalars(
                    select(DocumentEnrichmentTask)
                    .where(
                        DocumentEnrichmentTask.document_id == document_id,
                        DocumentEnrichmentTask.pipeline_generation
                        == pipeline_generation,
                    )
                    .order_by(DocumentEnrichmentTask.attempt.desc())
                    .limit(1)
                ).first()
                if latest_enrichment is None or latest_enrichment.state != "failed":
                    raise ValueError("没有失败的阶段任务可重试")
                item = DocumentEnrichmentTask(
                    id=str(uuid.uuid4()),
                    document_id=document_id,
                    pipeline_generation=pipeline_generation,
                    attempt=latest_enrichment.attempt + 1,
                    state="queued",
                    available_at=now,
                    input_sha256=latest_enrichment.input_sha256,
                    llm_profile_id=latest_enrichment.llm_profile_id,
                    llm_profile_updated_at=latest_enrichment.llm_profile_updated_at,
                    prompt_schema_version=latest_enrichment.prompt_schema_version,
                    options_json=latest_enrichment.options_json,
                    created_at=now,
                    updated_at=now,
                )
            session.add(item)
            document.status = "parsing" if stage == "normalize" else "parsed"
            document.current_stage = stage
            document.stage_state = "queued"
            document.failed_stage = None
            document.error_code = None
            document.error_message = None
            document.completed_at = None
            document.progress_completed = 0
            if stage == "normalize":
                document.progress_total = normalization_chunk_count
                document.progress_unit = "chunks"
            else:
                document.progress_total = None
                document.progress_unit = "nodes"
            document.updated_at = now
            session.flush()
            return _task_dict(item)

    def update_pipeline_task_progress(
        self,
        stage: str,
        task_id: str,
        lease_token: str,
        *,
        progress_completed: int,
        progress_total: int,
        lease_expires_at: str,
        now: str,
    ) -> bool:
        if stage not in {"normalize", "enrich"}:
            raise ValueError("不支持的流水线阶段")
        if not 0 <= progress_completed <= progress_total:
            raise ValueError("流水线进度无效")
        model = (
            DocumentNormalizationTask
            if stage == "normalize"
            else DocumentEnrichmentTask
        )
        with self.factory.session_scope(write=True) as session:
            item = session.get(model, task_id)
            if (
                not self._lease_matches(item, lease_token)
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
            if isinstance(item, DocumentEnrichmentTask):
                item.node_count = progress_total
                item.nodes_completed = progress_completed
            document.progress_completed = progress_completed
            document.progress_total = progress_total
            document.progress_unit = "chunks" if stage == "normalize" else "nodes"
            document.updated_at = now
            return True

    def renew_pipeline_task_lease(
        self,
        stage: str,
        task_id: str,
        lease_token: str,
        *,
        lease_expires_at: str,
        now: str,
    ) -> bool:
        """Renew a running stage without coupling liveness to progress changes."""
        if stage == "normalize":
            model = DocumentNormalizationTask
        elif stage == "enrich":
            model = DocumentEnrichmentTask
        else:
            raise ValueError("不支持的流水线阶段")
        with self.factory.session_scope(write=True) as session:
            item = session.get(model, task_id)
            if not self._lease_matches(item, lease_token, now):
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

    def claim_normalization_task(
        self, *, owner: str, now: str, lease_expires_at: str
    ) -> dict[str, Any] | None:
        return self._claim_task(DocumentNormalizationTask, owner, now, lease_expires_at)

    def claim_enrichment_task(
        self, *, owner: str, now: str, lease_expires_at: str
    ) -> dict[str, Any] | None:
        return self._claim_task(DocumentEnrichmentTask, owner, now, lease_expires_at)

    def _claim_task(
        self,
        model: type[TaskT],
        owner: str,
        now: str,
        lease_expires_at: str,
    ) -> dict[str, Any] | None:
        with self.factory.session_scope(write=True) as session:
            item = session.scalars(
                select(model)
                .where(
                    or_(
                        (model.state == "queued") & (model.available_at <= now),
                        (model.state == "running")
                        & (model.lease_expires_at.is_not(None))
                        & (model.lease_expires_at < now),
                    )
                )
                .order_by(model.available_at, model.created_at)
                .limit(1)
            ).first()
            if item is None:
                return None
            token = str(uuid.uuid4())
            item.state = "running"
            item.lease_owner = owner
            item.lease_token = token
            item.lease_expires_at = lease_expires_at
            item.started_at = item.started_at or now
            item.updated_at = now
            document = session.get(Document, item.document_id)
            if (
                document is None
                or document.pipeline_generation != item.pipeline_generation
            ):
                item.state = "canceled"
                item.completed_at = now
                item.lease_owner = None
                item.lease_token = None
                item.lease_expires_at = None
                return None
            document.stage_state = "running"
            document.updated_at = now
            session.flush()
            return _task_dict(item)

    def finish_normalization_task(
        self,
        task_id: str,
        lease_token: str,
        *,
        normalized_markdown_relpath: str,
        page_map_relpath: str,
        output_sha256: str,
        now: str,
    ) -> bool:
        with self.factory.session_scope(write=True) as session:
            item = session.get(DocumentNormalizationTask, task_id)
            if not self._lease_matches(item, lease_token, now):
                return False
            document = session.get(Document, item.document_id)
            if (
                document is None
                or document.pipeline_generation != item.pipeline_generation
            ):
                return False
            item.state = "succeeded"
            item.normalized_markdown_relpath = normalized_markdown_relpath
            item.page_map_relpath = page_map_relpath
            item.output_sha256 = output_sha256
            self._clear_lease(item, now)
            document.status = "parsed"
            document.current_stage = "enrich"
            document.stage_state = "queued"
            document.updated_at = now
            return True

    def finish_enrichment_task(
        self,
        task_id: str,
        lease_token: str,
        *,
        state: str,
        node_count: int,
        nodes_completed: int,
        nodes_failed: int,
        output_markdown_relpath: str,
        output_tree_relpath: str,
        output_sha256: str,
        warning_json: str,
        now: str,
    ) -> bool:
        if state not in {"succeeded", "partial"}:
            raise ValueError("增强完成状态无效")
        with self.factory.session_scope(write=True) as session:
            item = session.get(DocumentEnrichmentTask, task_id)
            if not self._lease_matches(item, lease_token, now):
                return False
            document = session.get(Document, item.document_id)
            if (
                document is None
                or document.pipeline_generation != item.pipeline_generation
            ):
                return False
            item.state = state
            item.node_count = node_count
            item.nodes_completed = nodes_completed
            item.nodes_failed = nodes_failed
            item.output_markdown_relpath = output_markdown_relpath
            item.output_tree_relpath = output_tree_relpath
            item.output_sha256 = output_sha256
            item.warning_json = warning_json
            self._clear_lease(item, now)
            document.current_stage = (
                "complete" if document.status == "ready" else "index"
            )
            document.stage_state = state
            document.warning_json = warning_json
            document.updated_at = now
            return True

    def fail_pipeline_task(
        self,
        stage: str,
        task_id: str,
        lease_token: str,
        *,
        error_code: str,
        error_message: str,
        now: str,
    ) -> bool:
        model: type[DocumentNormalizationTask] | type[DocumentEnrichmentTask]
        if stage == "normalize":
            model = DocumentNormalizationTask
        elif stage == "enrich":
            model = DocumentEnrichmentTask
        else:
            raise ValueError("不支持的流水线阶段")
        with self.factory.session_scope(write=True) as session:
            item = session.get(model, task_id)
            if not self._lease_matches(item, lease_token, now):
                return False
            item.state = "failed"
            item.error_code = error_code
            item.error_message = error_message
            self._clear_lease(item, now)
            document = session.get(Document, item.document_id)
            if (
                document is not None
                and document.pipeline_generation == item.pipeline_generation
            ):
                document.status = "failed"
                document.current_stage = stage
                document.stage_state = "failed"
                document.failed_stage = stage
                document.error_code = error_code
                document.error_message = error_message
                document.updated_at = now
                document.completed_at = now
            return True

    @staticmethod
    def _lease_matches(item: Any, lease_token: str, now: str | None = None) -> bool:
        return bool(
            item is not None
            and item.state == "running"
            and item.lease_token == lease_token
            and (
                now is None
                or (item.lease_expires_at is not None and item.lease_expires_at > now)
            )
        )

    @staticmethod
    def _clear_lease(item: Any, now: str) -> None:
        item.lease_owner = None
        item.lease_token = None
        item.lease_expires_at = None
        item.updated_at = now
        item.completed_at = now

    def get_normalization_task(self, task_id: str) -> dict[str, Any] | None:
        with self.factory.session_scope() as session:
            item = session.get(DocumentNormalizationTask, task_id)
            return _task_dict(item) if item is not None else None

    def get_latest_normalization_task(
        self, document_id: str, pipeline_generation: int
    ) -> dict[str, Any] | None:
        with self.factory.session_scope() as session:
            item = session.scalars(
                select(DocumentNormalizationTask)
                .where(
                    DocumentNormalizationTask.document_id == document_id,
                    DocumentNormalizationTask.pipeline_generation
                    == pipeline_generation,
                )
                .order_by(DocumentNormalizationTask.attempt.desc())
                .limit(1)
            ).first()
            return _task_dict(item) if item is not None else None

    def get_enrichment_task(self, task_id: str) -> dict[str, Any] | None:
        with self.factory.session_scope() as session:
            item = session.get(DocumentEnrichmentTask, task_id)
            return _task_dict(item) if item is not None else None

    def get_latest_enrichment_task(
        self, document_id: str, pipeline_generation: int
    ) -> dict[str, Any] | None:
        with self.factory.session_scope() as session:
            item = session.scalars(
                select(DocumentEnrichmentTask)
                .where(
                    DocumentEnrichmentTask.document_id == document_id,
                    DocumentEnrichmentTask.pipeline_generation == pipeline_generation,
                )
                .order_by(DocumentEnrichmentTask.attempt.desc())
                .limit(1)
            ).first()
            return _task_dict(item) if item is not None else None

    def get_enrichment_call_result(
        self,
        *,
        document_id: str,
        pipeline_generation: int,
        purpose: str,
        unit_key: str,
        input_sha256: str,
        model_fingerprint: str,
        prompt_schema_version: int,
    ) -> dict[str, Any] | None:
        with self.factory.session_scope() as session:
            item = session.scalar(
                select(DocumentEnrichmentCallResult).where(
                    DocumentEnrichmentCallResult.document_id == document_id,
                    DocumentEnrichmentCallResult.pipeline_generation
                    == pipeline_generation,
                    DocumentEnrichmentCallResult.purpose == purpose,
                    DocumentEnrichmentCallResult.unit_key == unit_key,
                    DocumentEnrichmentCallResult.input_sha256 == input_sha256,
                    DocumentEnrichmentCallResult.model_fingerprint == model_fingerprint,
                    DocumentEnrichmentCallResult.prompt_schema_version
                    == prompt_schema_version,
                )
            )
            return _task_dict(item) if item is not None else None

    def put_enrichment_call_result(
        self,
        *,
        document_id: str,
        pipeline_generation: int,
        purpose: str,
        unit_key: str,
        input_sha256: str,
        model_fingerprint: str,
        prompt_schema_version: int,
        state: str,
        output_text: str | None,
        error_code: str | None,
        now: str,
    ) -> None:
        with self.factory.session_scope(write=True) as session:
            item = session.scalar(
                select(DocumentEnrichmentCallResult).where(
                    DocumentEnrichmentCallResult.document_id == document_id,
                    DocumentEnrichmentCallResult.pipeline_generation
                    == pipeline_generation,
                    DocumentEnrichmentCallResult.purpose == purpose,
                    DocumentEnrichmentCallResult.unit_key == unit_key,
                    DocumentEnrichmentCallResult.input_sha256 == input_sha256,
                    DocumentEnrichmentCallResult.model_fingerprint == model_fingerprint,
                    DocumentEnrichmentCallResult.prompt_schema_version
                    == prompt_schema_version,
                )
            )
            if item is None:
                item = DocumentEnrichmentCallResult(
                    id=str(uuid.uuid4()),
                    document_id=document_id,
                    pipeline_generation=pipeline_generation,
                    purpose=purpose,
                    unit_key=unit_key,
                    input_sha256=input_sha256,
                    model_fingerprint=model_fingerprint,
                    prompt_schema_version=prompt_schema_version,
                    state=state,
                    output_text=output_text,
                    error_code=error_code,
                    created_at=now,
                    updated_at=now,
                )
                session.add(item)
            else:
                item.state = state
                item.output_text = output_text
                item.error_code = error_code
                item.updated_at = now

    def list_pipeline_events(
        self, document_id: str, pipeline_generation: int
    ) -> list[dict[str, Any]]:
        with self.factory.session_scope() as session:
            items = session.scalars(
                select(DocumentPipelineEvent)
                .where(
                    DocumentPipelineEvent.document_id == document_id,
                    DocumentPipelineEvent.pipeline_generation == pipeline_generation,
                )
                .order_by(DocumentPipelineEvent.created_at, DocumentPipelineEvent.id)
            ).all()
            return [_event_dict(item) for item in items]

    def append_pipeline_event(
        self,
        *,
        document_id: str,
        pipeline_generation: int,
        stage: str,
        event_type: str,
        now: str,
        progress_completed: int | None = None,
        progress_total: int | None = None,
        message: str | None = None,
    ) -> None:
        with self.factory.session_scope(write=True) as session:
            session.add(
                DocumentPipelineEvent(
                    id=str(uuid.uuid4()),
                    document_id=document_id,
                    pipeline_generation=pipeline_generation,
                    stage=stage,
                    event_type=event_type,
                    progress_completed=progress_completed,
                    progress_total=progress_total,
                    message=message,
                    created_at=now,
                )
            )
            ids = session.scalars(
                select(DocumentPipelineEvent.id)
                .where(
                    DocumentPipelineEvent.document_id == document_id,
                    DocumentPipelineEvent.pipeline_generation == pipeline_generation,
                )
                .order_by(
                    DocumentPipelineEvent.created_at.desc(),
                    DocumentPipelineEvent.id.desc(),
                )
                .offset(100)
            ).all()
            if ids:
                session.execute(
                    delete(DocumentPipelineEvent).where(
                        DocumentPipelineEvent.id.in_(ids)
                    )
                )

    def reserve_staging_bytes(
        self,
        *,
        document_id: str,
        pipeline_generation: int,
        allocation_key: str,
        kind: str,
        bytes_reserved: int,
        maximum_bytes: int,
        now: str,
    ) -> bool:
        if bytes_reserved < 0:
            raise ValueError("staging 预留字节数不能为负数")
        with self.factory.session_scope(write=True) as session:
            existing = session.scalar(
                select(DocumentStagingAllocation).where(
                    DocumentStagingAllocation.document_id == document_id,
                    DocumentStagingAllocation.pipeline_generation
                    == pipeline_generation,
                    DocumentStagingAllocation.allocation_key == allocation_key,
                )
            )
            current = (
                int(existing.bytes_reserved)
                if existing is not None and existing.state == "active"
                else 0
            )
            total = int(
                session.scalar(
                    select(func.sum(DocumentStagingAllocation.bytes_reserved)).where(
                        DocumentStagingAllocation.document_id == document_id,
                        DocumentStagingAllocation.pipeline_generation
                        == pipeline_generation,
                        DocumentStagingAllocation.state == "active",
                    )
                )
                or 0
            )
            if total - current + bytes_reserved > maximum_bytes:
                return False
            if existing is None:
                existing = DocumentStagingAllocation(
                    id=str(uuid.uuid4()),
                    document_id=document_id,
                    pipeline_generation=pipeline_generation,
                    allocation_key=allocation_key,
                    kind=kind,
                    bytes_reserved=bytes_reserved,
                    state="active",
                    updated_at=now,
                )
                session.add(existing)
            else:
                existing.kind = kind
                existing.bytes_reserved = bytes_reserved
                existing.state = "active"
                existing.updated_at = now
            return True

    def reserve_claimed_parse_staging_bytes(
        self,
        *,
        task_id: str,
        lease_token: str,
        allocation_key: str,
        kind: str,
        bytes_reserved: int,
        maximum_bytes: int,
        now: str,
    ) -> bool:
        """Reserve bytes only while the parse lease still owns its generation."""
        if bytes_reserved < 0:
            raise ValueError("staging 预留字节数不能为负数")
        with self.factory.session_scope(write=True) as session:
            task = session.get(DocumentParseTask, task_id)
            if (
                task is None
                or task.lease_token != lease_token
                or task.lease_expires_at is None
                or task.lease_expires_at <= now
                or task.state
                not in {
                    "created",
                    "uploading",
                    "waiting-file",
                    "pending",
                    "running",
                    "converting",
                }
            ):
                return False
            document = session.get(Document, task.document_id)
            if (
                document is None
                or document.status == "deleted"
                or document.pipeline_generation != task.pipeline_generation
            ):
                return False
            existing = session.scalar(
                select(DocumentStagingAllocation).where(
                    DocumentStagingAllocation.document_id == task.document_id,
                    DocumentStagingAllocation.pipeline_generation
                    == task.pipeline_generation,
                    DocumentStagingAllocation.allocation_key == allocation_key,
                )
            )
            current = (
                int(existing.bytes_reserved)
                if existing is not None and existing.state == "active"
                else 0
            )
            total = int(
                session.scalar(
                    select(func.sum(DocumentStagingAllocation.bytes_reserved)).where(
                        DocumentStagingAllocation.document_id == task.document_id,
                        DocumentStagingAllocation.pipeline_generation
                        == task.pipeline_generation,
                        DocumentStagingAllocation.state == "active",
                    )
                )
                or 0
            )
            if total - current + bytes_reserved > maximum_bytes:
                return False
            if existing is None:
                existing = DocumentStagingAllocation(
                    id=str(uuid.uuid4()),
                    document_id=task.document_id,
                    pipeline_generation=task.pipeline_generation,
                    allocation_key=allocation_key,
                    kind=kind,
                    bytes_reserved=bytes_reserved,
                    state="active",
                    updated_at=now,
                )
                session.add(existing)
            else:
                existing.kind = kind
                existing.bytes_reserved = bytes_reserved
                existing.state = "active"
                existing.updated_at = now
            return True

    def release_staging_allocation(
        self,
        document_id: str,
        pipeline_generation: int,
        allocation_key: str,
        now: str,
    ) -> None:
        with self.factory.session_scope(write=True) as session:
            item = session.scalar(
                select(DocumentStagingAllocation).where(
                    DocumentStagingAllocation.document_id == document_id,
                    DocumentStagingAllocation.pipeline_generation
                    == pipeline_generation,
                    DocumentStagingAllocation.allocation_key == allocation_key,
                )
            )
            if item is not None:
                item.state = "released"
                item.updated_at = now

    def release_generation_staging_allocations(
        self, document_id: str, pipeline_generation: int, now: str
    ) -> None:
        with self.factory.session_scope(write=True) as session:
            session.query(DocumentStagingAllocation).filter(
                DocumentStagingAllocation.document_id == document_id,
                DocumentStagingAllocation.pipeline_generation == pipeline_generation,
                DocumentStagingAllocation.state == "active",
            ).update(
                {"state": "released", "updated_at": now},
                synchronize_session=False,
            )

    def reconcile_generation_staging_bytes(
        self,
        document_id: str,
        pipeline_generation: int,
        bytes_reserved: int,
        now: str,
    ) -> None:
        """Replace possibly stale per-file allocations with one exact disk total."""
        if bytes_reserved < 0:
            raise ValueError("staging 实际字节数不能为负数")
        with self.factory.session_scope(write=True) as session:
            allocations = session.scalars(
                select(DocumentStagingAllocation).where(
                    DocumentStagingAllocation.document_id == document_id,
                    DocumentStagingAllocation.pipeline_generation
                    == pipeline_generation,
                )
            ).all()
            for allocation in allocations:
                allocation.state = "released"
                allocation.updated_at = now
            if bytes_reserved == 0:
                return
            item = next(
                (
                    allocation
                    for allocation in allocations
                    if allocation.allocation_key == "reconcile:filesystem"
                ),
                None,
            )
            if item is None:
                item = DocumentStagingAllocation(
                    id=str(uuid.uuid4()),
                    document_id=document_id,
                    pipeline_generation=pipeline_generation,
                    allocation_key="reconcile:filesystem",
                    kind="temp_output",
                    bytes_reserved=bytes_reserved,
                    state="active",
                    updated_at=now,
                )
                session.add(item)
            else:
                item.kind = "temp_output"
                item.bytes_reserved = bytes_reserved
                item.state = "active"
                item.updated_at = now

    def list_active_task_generation_refs(
        self, knowledge_base_id: str
    ) -> set[tuple[str, int]]:
        """Return generations protected by a queued/running stage task."""
        refs: set[tuple[str, int]] = set()
        with self.factory.session_scope() as session:
            for model, active_states in (
                (
                    DocumentParseTask,
                    (
                        "created",
                        "uploading",
                        "waiting-file",
                        "pending",
                        "running",
                        "converting",
                    ),
                ),
                (DocumentNormalizationTask, ("queued", "running")),
                (DocumentEnrichmentTask, ("queued", "running")),
            ):
                rows = session.execute(
                    select(model.document_id, model.pipeline_generation)
                    .join(Document, Document.id == model.document_id)
                    .where(
                        Document.knowledge_base_id == knowledge_base_id,
                        model.state.in_(active_states),
                    )
                ).all()
                refs.update((str(row[0]), int(row[1])) for row in rows)
        return refs

    def list_active_staging_generation_refs(
        self, knowledge_base_id: str
    ) -> set[tuple[str, int]]:
        with self.factory.session_scope() as session:
            rows = session.execute(
                select(
                    DocumentStagingAllocation.document_id,
                    DocumentStagingAllocation.pipeline_generation,
                )
                .join(Document, Document.id == DocumentStagingAllocation.document_id)
                .where(
                    Document.knowledge_base_id == knowledge_base_id,
                    DocumentStagingAllocation.state == "active",
                )
            ).all()
            return {(str(row[0]), int(row[1])) for row in rows}


__all__ = ["DocumentPipelineRepositoryMixin"]
