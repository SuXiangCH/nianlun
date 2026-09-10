"""Knowledge-base lifecycle and document ingestion orchestration."""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import uuid
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import status

from app.api_server.common.errors import ApiError
from app.api_server.repositories import SQLiteMetadataRepository
from app.api_server.apis.v1.schemas import (
    KnowledgeBaseCreateRequest,
    KnowledgeBaseResponse,
    KnowledgeBaseUpdateRequest,
)
from app.api_server.services.model_config_service import ModelConfigService
from nianlun.indexing.tree.workspace import build_workspace_doc
from app.api_server.services.workspace_store import (
    NEW_KNOWLEDGE_BASE_TREE_BUILD_OPTIONS,
    WorkspaceArtifactStore,
    workspace_lock,
)
from app.api_server.services.workspace_snapshot import (
    build_manifest,
    discard_staging,
    promote_staged_snapshot,
    publish_document_snapshot,
    snapshot_relpath,
    stage_manifest,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


logger = logging.getLogger(__name__)


class KnowledgeBaseService:
    """Own knowledge-base metadata while delegating indexing to indexing modules."""

    def __init__(
        self,
        repository: SQLiteMetadataRepository,
        workspace_root: Path,
        models: ModelConfigService,
        *,
        fts_enabled: bool = True,
    ) -> None:
        self.repository = repository
        self.workspace_root = workspace_root.expanduser().resolve()
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        self.artifacts = WorkspaceArtifactStore()
        self.models = models
        self.fts_enabled = fts_enabled

    def _workspace_path(self, relative_path: str) -> Path:
        candidate = (self.workspace_root / relative_path).resolve()
        try:
            candidate.relative_to(self.workspace_root)
        except ValueError as exc:
            raise ApiError(
                "知识库 workspace 路径越界", status.HTTP_500_INTERNAL_SERVER_ERROR
            ) from exc
        return candidate

    def _with_runtime_fields(self, item: dict[str, Any]) -> dict[str, Any]:
        result = dict(item)
        relative_path = str(result.get("workspace_relpath", result["id"]))
        result["workspace_dir"] = str(self._workspace_path(relative_path))
        result["vector_enabled"] = result.get("vector_status") != "disabled"
        revision = self.repository.get_committed_revision(
            str(result["id"]), int(result.get("content_version", 0))
        )
        if revision is not None:
            result["snapshot_relpath"] = revision["snapshot_relpath"]
            result["snapshot_manifest_sha256"] = revision["manifest_sha256"]
        return result

    @staticmethod
    def _response(item: dict[str, Any]) -> KnowledgeBaseResponse:
        fields = KnowledgeBaseResponse.model_fields
        return KnowledgeBaseResponse.model_validate(
            {key: item[key] for key in fields if key in item}
        )

    def list(self) -> list[KnowledgeBaseResponse]:
        return [
            self._response(self._with_runtime_fields(item))
            for item in self.repository.list("knowledge_bases")
        ]

    def get(self, knowledge_base_id: str) -> KnowledgeBaseResponse:
        item = self.repository.get("knowledge_bases", knowledge_base_id)
        if item is None:
            raise ApiError("知识库不存在", status.HTTP_404_NOT_FOUND)
        return self._response(self._with_runtime_fields(item))

    def require_record(self, knowledge_base_id: str) -> dict[str, Any]:
        item = self.repository.get("knowledge_bases", knowledge_base_id)
        if item is None:
            raise ApiError("知识库不存在", status.HTTP_404_NOT_FOUND)
        return self._with_runtime_fields(item)

    def workspace_path_for(self, item: dict[str, Any]) -> Path:
        """Resolve the workspace directory for a knowledge-base record."""
        return self._workspace_path(str(item["workspace_relpath"]))

    def reconcile(self) -> None:
        """Repair SQLite document counts from durable workspace manifests."""
        for item in self.repository.list("knowledge_bases"):
            workspace = self._workspace_path(str(item["workspace_relpath"]))
            try:
                document_count = self.artifacts.document_count(workspace)
            except ValueError:
                logger.exception(
                    "knowledge_base.reconcile_failed knowledge_base_id=%s",
                    item["id"],
                )
                continue
            if int(item["document_count"]) != document_count:
                self.repository.reconcile_document_count(
                    str(item["id"]), document_count, _now().isoformat()
                )

    def create(self, request: KnowledgeBaseCreateRequest) -> KnowledgeBaseResponse:
        knowledge_base_id = str(uuid.uuid4())
        if request.embedding_model_id is not None:
            self.models.require_embedding_profile(request.embedding_model_id)
        workspace_dir = self._workspace_path(knowledge_base_id)
        workspace_dir.mkdir(parents=True, exist_ok=False)
        try:
            WorkspaceArtifactStore.atomic_write(workspace_dir / "_meta.json", b"{}")
            WorkspaceArtifactStore.write_tree_build_options(
                workspace_dir,
                NEW_KNOWLEDGE_BASE_TREE_BUILD_OPTIONS,
            )
            timestamp = _now()
            item = {
                "id": knowledge_base_id,
                "name": request.name,
                "description": request.description,
                "status": "ready",
                "workspace_relpath": knowledge_base_id,
                "document_count": 0,
                "summary_enabled": request.summary_enabled,
                "heading_recovery_enabled": request.heading_recovery_enabled,
                "embedding_model_id": request.embedding_model_id,
                "content_version": 0,
                "fts_status": "disabled",
                "fts_revision": None,
                "fts_target_revision": None,
                "workspace_dir": str(workspace_dir),
                "created_at": timestamp.isoformat(),
                "updated_at": timestamp.isoformat(),
            }
            initial_manifest = build_manifest(knowledge_base_id, 0, [], [])
            staging, manifest_sha256 = stage_manifest(workspace_dir, initial_manifest)
            try:
                promote_staged_snapshot(workspace_dir, staging, initial_manifest)
            except Exception:
                discard_staging(staging)
                raise
            self.repository.create_knowledge_base_with_revision(
                item,
                snapshot_relpath=snapshot_relpath(0),
                manifest_sha256=manifest_sha256,
            )
        except Exception:
            self.repository.delete_knowledge_base(knowledge_base_id)
            shutil.rmtree(workspace_dir, ignore_errors=True)
            raise
        return self._response(self._with_runtime_fields(item))

    def update(
        self, knowledge_base_id: str, request: KnowledgeBaseUpdateRequest
    ) -> KnowledgeBaseResponse:
        item = self.require_record(knowledge_base_id)
        updates: dict[str, Any] = {}
        if request.name is not None:
            updates["name"] = request.name
        if request.summary_enabled is not None:
            updates["summary_enabled"] = request.summary_enabled
        if request.heading_recovery_enabled is not None:
            updates["heading_recovery_enabled"] = request.heading_recovery_enabled
        if "embedding_model_id" in request.model_fields_set:
            if request.embedding_model_id is None:
                raise ApiError(
                    "模型选择不能用于关闭向量检索，请使用 vector_enabled",
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                )
            self.models.require_embedding_profile(request.embedding_model_id)
            if request.embedding_model_id != item.get("embedding_model_id"):
                updates.update(
                    {
                        "vector_model_id": request.embedding_model_id,
                        "vector_status": "disabled",
                        "vector_revision": None,
                        "vector_target_revision": None,
                        "vector_model_updated_at": None,
                        "vector_dimension": None,
                        "vector_error": None,
                        "vector_progress_stage": None,
                        "vector_documents_total": None,
                        "vector_documents_completed": None,
                        "vector_records_processed": None,
                    }
                )
        if request.vector_enabled is False:
            updates.update(
                {
                    "vector_status": "disabled",
                    "vector_target_revision": None,
                    "vector_error": None,
                    "vector_progress_stage": None,
                    "vector_documents_total": None,
                    "vector_documents_completed": None,
                    "vector_records_processed": None,
                }
            )
        elif request.vector_enabled is True and not item.get("embedding_model_id"):
            raise ApiError("请先选择 Embedding 模型", status.HTTP_409_CONFLICT)
        updates["updated_at"] = _now().isoformat()
        self.repository.update_knowledge_base_settings(knowledge_base_id, updates)
        return self.get(knowledge_base_id)

    def delete(self, knowledge_base_id: str) -> dict[str, Any]:
        """Hard-delete a knowledge base and its API-owned workspace."""
        item = self.require_record(knowledge_base_id)
        application_count = self.repository.count_applications_for_knowledge_base(
            knowledge_base_id
        )
        if application_count:
            raise ApiError(
                "知识库仍被应用绑定，请先删除或解绑这些应用",
                status.HTTP_409_CONFLICT,
            )

        workspace = self._workspace_path(str(item["workspace_relpath"]))
        if workspace.exists():
            with workspace_lock(workspace):
                shutil.rmtree(workspace)
        if not self.repository.delete_knowledge_base(knowledge_base_id):
            raise ApiError("知识库不存在", status.HTTP_404_NOT_FOUND)
        return item

    def add_markdown(
        self,
        knowledge_base_id: str,
        filename: str,
        content: bytes,
        idempotency_key: str | None = None,
        *,
        workspace_locked: bool = False,
        prebuilt_document: dict[str, Any] | None = None,
    ) -> KnowledgeBaseResponse:
        item = self.require_record(knowledge_base_id)
        if not filename.lower().endswith(".md"):
            raise ApiError(
                "当前版本只支持 Markdown 文档", status.HTTP_415_UNSUPPORTED_MEDIA_TYPE
            )
        if not content:
            raise ApiError("上传文档不能为空")

        workspace_dir = self._workspace_path(str(item["workspace_relpath"]))
        request_sha256 = hashlib.sha256(
            filename.encode("utf-8") + b"\0" + content
        ).hexdigest()
        operation_key = idempotency_key or f"auto:{uuid.uuid4().hex}"

        lock = nullcontext() if workspace_locked else workspace_lock(workspace_dir)
        with lock:
            source_sha256 = hashlib.sha256(content).hexdigest()
            existing_document = self.repository.get_document_by_hash(
                knowledge_base_id, source_sha256
            )
            if (
                existing_document is not None
                and existing_document["status"] != "deleted"
            ):
                replay = self._with_runtime_fields(
                    self.require_record(knowledge_base_id)
                )
                replay["document_id"] = existing_document["id"]
                replay["idempotent_replay"] = True
                return self._response(replay)
            operation = self.repository.get_upload(knowledge_base_id, operation_key)
            if operation is not None:
                if operation["request_sha256"] != request_sha256:
                    raise ApiError(
                        "Idempotency-Key 已用于其他上传内容", status.HTTP_409_CONFLICT
                    )
                if operation["status"] == "committed":
                    replay = self._with_runtime_fields(
                        self.require_record(knowledge_base_id)
                    )
                    replay["document_id"] = operation["document_id"]
                    replay["idempotent_replay"] = True
                    return self._response(replay)
                document_id = str(operation["document_id"])
            else:
                document_id = str(uuid.uuid4())
                operation = self.repository.start_upload(
                    knowledge_base_id,
                    operation_key,
                    request_sha256,
                    document_id,
                    _now().isoformat(),
                )
                document_id = str(operation["document_id"])

            source_name = Path(filename).name or "document.md"
            source_relpath = f"sources/{document_id}-{source_name}"
            staging_path = workspace_dir / f".upload-{document_id}.md.tmp"
            try:
                staging_path.write_bytes(content)
                tree_options = self.artifacts.read_tree_build_options(workspace_dir)
                if prebuilt_document is not None:
                    # 离线导入：直接采用调用方提供的已建树（含 LLM 摘要），
                    # 跳过现场重建，不产生任何模型调用。
                    document = json.loads(json.dumps(prebuilt_document))
                elif item["summary_enabled"]:
                    _, document = build_workspace_doc(
                        str(staging_path),
                        llm=self.models.build_llm(),
                        thin=tree_options.subtree_folding_enabled,
                        min_node_token=tree_options.min_subtree_tokens,
                    )
                else:
                    _, document = build_workspace_doc(
                        str(staging_path),
                        no_summary=True,
                        thin=tree_options.subtree_folding_enabled,
                        min_node_token=tree_options.min_subtree_tokens,
                    )
                document["doc_name"] = source_name
                _document_count, artifact_sha256 = self.artifacts.write_document(
                    workspace_dir,
                    document_id,
                    source_relpath,
                    content,
                    document,
                )
                self.repository.mark_upload_files_committed(
                    knowledge_base_id,
                    operation_key,
                    source_relpath,
                    f"{document_id}.json",
                    hashlib.sha256(content).hexdigest(),
                    artifact_sha256,
                    _now().isoformat(),
                )
                now = _now().isoformat()
                content_version = publish_document_snapshot(
                    self.repository,
                    workspace=workspace_dir,
                    knowledge_base_id=knowledge_base_id,
                    document_id=document_id,
                    generation=1,
                    content=content,
                    source_relpath=source_relpath,
                    source_mime_type="text/markdown",
                    idempotency_key=operation_key,
                    fts_enabled=self.fts_enabled,
                    now=now,
                    document_values={
                        "id": document_id,
                        "knowledge_base_id": knowledge_base_id,
                        "original_filename": filename,
                        "file_extension": ".md",
                        "mime_type": "text/markdown",
                        "size_bytes": len(content),
                        "source_relpath": source_relpath,
                        "source_sha256": hashlib.sha256(content).hexdigest(),
                        "parser": "native_markdown",
                        "status": "uploaded",
                        "pipeline_generation": 1,
                        "created_at": now,
                        "updated_at": now,
                    },
                )
            except Exception as exc:
                operation_state = None
                try:
                    operation_state = self.repository.get_upload(
                        knowledge_base_id, operation_key
                    )
                except Exception:
                    logger.exception("upload.state_read_failed")
                if operation_state is None or operation_state["status"] != "committed":
                    try:
                        self.artifacts.discard_document(
                            workspace_dir, document_id, source_relpath
                        )
                    except Exception:
                        logger.exception(
                            "upload.cleanup_failed knowledge_base_id=%s document_id=%s",
                            knowledge_base_id,
                            document_id,
                        )
                    if operation_state is not None:
                        self.repository.fail_upload(
                            knowledge_base_id,
                            operation_key,
                            str(exc),
                            _now().isoformat(),
                        )
                if isinstance(exc, ValueError):
                    raise ApiError(
                        "知识库 workspace 元数据损坏",
                        status.HTTP_500_INTERNAL_SERVER_ERROR,
                    ) from exc
                raise
            finally:
                staging_path.unlink(missing_ok=True)

        response_item = self._with_runtime_fields(
            self.require_record(knowledge_base_id)
        )
        response_item["document_id"] = document_id
        response_item["content_version"] = content_version
        response_item["idempotent_replay"] = False
        return self._response(response_item)


__all__ = ["KnowledgeBaseService"]
