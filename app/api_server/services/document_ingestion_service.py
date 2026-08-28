"""Document upload, MinerU ingestion, and document detail services."""

from __future__ import annotations

import hashlib
import json
import logging
import mimetypes
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import status

from app.api_server.common.errors import ApiError
from app.api_server.config import ApiServerSettings
from app.api_server.integrations.mineru import (
    MineruClient,
    MineruError,
    SelfHostedMineruClient,
)
from app.api_server.repositories import SQLiteMetadataRepository
from app.api_server.services.knowledge_base_service import KnowledgeBaseService
from app.api_server.services.model_config_service import ModelConfigService
from app.api_server.services.documents.mineru_artifacts import (
    extract_result_archive,
    select_markdown_result,
)
from app.api_server.services.documents.mineru_tasks import build_parser_options
from app.api_server.services.workspace_store import (
    WorkspaceArtifactStore,
    workspace_lock,
)
from nianlun.indexing.fts.store import NodeFtsStore
from nianlun.indexing.tree.workspace import build_workspace_doc
from nianlun.indexing.vector.store import DocVectorStore


SUPPORTED_EXTENSIONS = {".md", ".pdf", ".doc", ".docx"}
MINERU_EXTENSIONS = {".pdf", ".doc", ".docx"}
_ACTIVE_STATES = {
    "created",
    "uploading",
    "waiting-file",
    "pending",
    "running",
    "converting",
}
logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_filename(filename: str, default: str = "document") -> str:
    name = Path(filename).name.strip().replace("\x00", "")
    if not name:
        name = default
    return name[:240]


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _resolve_workspace_path(workspace: Path, stored: str) -> Path | None:
    """Resolve a stored document path to a filesystem location or ``None``.

    Absolute paths come from legacy/CLI imports whose source ``full.md`` lives
    outside the workspace; they are written by the trusted indexing pipeline, so
    we only require the location to resolve. Relative paths are confined to the
    workspace to keep rejecting ``..`` traversal.
    """
    if Path(stored).is_absolute():
        return Path(stored)
    resolved = (workspace / stored).resolve()
    try:
        resolved.relative_to(workspace.resolve())
    except ValueError:
        return None
    return resolved


def _strip_index_text(nodes: Any) -> list[dict[str, Any]]:
    """Drop node bodies from an index tree, keeping outline-only fields."""
    if not isinstance(nodes, list):
        return []
    cleaned: list[dict[str, Any]] = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        entry = {
            key: node[key]
            for key in ("title", "node_id", "line_num", "summary", "prefix_summary")
            if node.get(key) is not None
        }
        children = _strip_index_text(node.get("nodes"))
        if children:
            entry["nodes"] = children
        cleaned.append(entry)
    return cleaned


class DocumentIngestionService:
    """Own document lifecycle while keeping Agent and indexing contracts stable."""

    def __init__(
        self,
        repository: SQLiteMetadataRepository,
        knowledge_bases: KnowledgeBaseService,
        models: ModelConfigService,
        settings: ApiServerSettings,
        fts_schedule: Callable[..., dict[str, Any]] | None = None,
        vector_schedule: Callable[..., dict[str, Any]] | None = None,
        mineru_client_factory: Callable[[dict[str, Any]], Any] | None = None,
    ) -> None:
        self.repository = repository
        self.knowledge_bases = knowledge_bases
        self.models = models
        self.settings = settings
        self.fts_schedule = fts_schedule
        self.vector_schedule = vector_schedule
        self.artifacts = WorkspaceArtifactStore()
        self._submission_executor = ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="nianlun-mineru-submit"
        )
        self._poll_executor = ThreadPoolExecutor(
            max_workers=4, thread_name_prefix="nianlun-mineru-poll"
        )
        self._jobs: dict[str, Future[Any]] = {}
        self._jobs_lock = threading.RLock()
        self._mineru_client_factory = (
            mineru_client_factory or self._create_mineru_client
        )

    @staticmethod
    def _create_mineru_client(config: dict[str, Any]) -> Any:
        client_class = (
            SelfHostedMineruClient
            if config.get("api_mode") == "self_hosted"
            else MineruClient
        )
        return client_class(str(config["base_url"]), str(config.get("api_key") or ""))

    def _client_for_task(self, task: dict[str, Any], config: dict[str, Any]) -> Any:
        """Use the persisted protocol mode even if the default parser changed."""
        task_config = {**config, "api_mode": task.get("api_mode", "saas_precision")}
        return self._mineru_client_factory(task_config)

    def upload(
        self,
        knowledge_base_id: str,
        filename: str,
        content: bytes,
        mime_type: str | None = None,
        idempotency_key: str | None = None,
        *,
        prebuilt_document: dict[str, Any] | None = None,
    ) -> Any:
        self.knowledge_bases.require_record(knowledge_base_id)
        source_name = _safe_filename(filename, "document.md")
        extension = Path(source_name).suffix.lower()
        if extension not in SUPPORTED_EXTENSIONS:
            raise ApiError(
                "只支持 Markdown、PDF、Word（.md、.pdf、.doc、.docx）",
                status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            )
        if not content:
            raise ApiError("上传文档不能为空")
        if extension == ".md":
            return self._upload_markdown(
                knowledge_base_id,
                source_name,
                content,
                mime_type,
                idempotency_key,
                prebuilt_document=prebuilt_document,
            )
        return self._upload_mineru_document(
            knowledge_base_id,
            source_name,
            extension,
            content,
            mime_type,
            idempotency_key,
        )

    def _upload_markdown(
        self,
        knowledge_base_id: str,
        filename: str,
        content: bytes,
        mime_type: str | None,
        idempotency_key: str | None,
        prebuilt_document: dict[str, Any] | None = None,
    ) -> Any:
        digest = _sha256(content)
        item = self.knowledge_bases.require_record(knowledge_base_id)
        workspace = Path(str(item["workspace_dir"]))
        with workspace_lock(workspace):
            existing = self.repository.get_document_by_hash(knowledge_base_id, digest)
            if existing is not None and existing["status"] != "deleted":
                return self.knowledge_bases.get(knowledge_base_id).model_copy(
                    update={"document_id": existing["id"], "idempotent_replay": True}
                )
            response = self.knowledge_bases.add_markdown(
                knowledge_base_id,
                filename,
                content,
                idempotency_key,
                workspace_locked=True,
                prebuilt_document=prebuilt_document,
            )
            document_id = str(response.document_id)
            manifest = json.loads(
                (workspace / "_meta.json").read_text(encoding="utf-8")
            )
            entry = manifest.get(document_id)
            if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
                raise ApiError("Markdown 文档已写入，但 workspace manifest 无法定位")
            source_relpath = str(entry["path"])
            created = _now()
            if self.repository.get_document(knowledge_base_id, document_id) is None:
                self.repository.create_document(
                    {
                        "id": document_id,
                        "knowledge_base_id": knowledge_base_id,
                        "original_filename": filename,
                        "file_extension": ".md",
                        "mime_type": mime_type or "text/markdown",
                        "size_bytes": len(content),
                        "source_relpath": source_relpath,
                        "source_sha256": digest,
                        "parser": "native_markdown",
                        "status": "ready",
                        "parsed_markdown_relpath": source_relpath,
                        "parsed_content_version": response.content_version,
                        "created_at": created,
                        "updated_at": created,
                        "completed_at": created,
                    }
                )
                self.repository.put_document_artifact(
                    {
                        "document_id": document_id,
                        "kind": "original",
                        "relpath": source_relpath,
                        "mime_type": mime_type or "text/markdown",
                        "size_bytes": len(content),
                        "sha256": digest,
                        "created_at": created,
                    }
                )
            return response

    def _upload_mineru_document(
        self,
        knowledge_base_id: str,
        filename: str,
        extension: str,
        content: bytes,
        mime_type: str | None,
        idempotency_key: str | None,
    ) -> Any:
        parser_config = self.models.parser_runtime_config()
        digest = _sha256(content)
        item = self.knowledge_bases.require_record(knowledge_base_id)
        workspace = Path(str(item["workspace_dir"]))
        operation_key = idempotency_key or f"auto:{uuid.uuid4().hex}"
        request_sha256 = hashlib.sha256(
            filename.encode("utf-8") + b"\0" + content
        ).hexdigest()
        created = _now()
        with workspace_lock(workspace):
            existing = self.repository.get_document_by_hash(knowledge_base_id, digest)
            if existing is not None and existing["status"] != "deleted":
                response = self.knowledge_bases.get(knowledge_base_id).model_copy(
                    update={"document_id": existing["id"], "idempotent_replay": True}
                )
                self._schedule_existing_task(existing["id"])
                return response
            operation = self.repository.get_upload(knowledge_base_id, operation_key)
            if operation is not None:
                if operation["request_sha256"] != request_sha256:
                    raise ApiError(
                        "Idempotency-Key 已用于其他上传内容", status.HTTP_409_CONFLICT
                    )
                document_id = str(operation["document_id"])
                existing_document = self.repository.get_document(
                    knowledge_base_id, document_id
                )
                if existing_document is not None:
                    self._schedule_existing_task(document_id)
                    return self.knowledge_bases.get(knowledge_base_id).model_copy(
                        update={"document_id": document_id, "idempotent_replay": True}
                    )
            else:
                document_id = str(uuid.uuid4())
                self.repository.start_upload(
                    knowledge_base_id,
                    operation_key,
                    request_sha256,
                    document_id,
                    created,
                )

            source_relpath = f"sources/{document_id}-{filename}"
            source_path = (workspace / source_relpath).resolve()
            source_path.parent.mkdir(parents=True, exist_ok=True)
            WorkspaceArtifactStore.atomic_write(source_path, content)
            if self.repository.get_document(knowledge_base_id, document_id) is None:
                self.repository.create_document(
                    {
                        "id": document_id,
                        "knowledge_base_id": knowledge_base_id,
                        "original_filename": filename,
                        "file_extension": extension,
                        "mime_type": mime_type
                        or mimetypes.guess_type(filename)[0]
                        or "application/octet-stream",
                        "size_bytes": len(content),
                        "source_relpath": source_relpath,
                        "source_sha256": digest,
                        "parser": "mineru",
                        "status": "parsing",
                        "created_at": created,
                        "updated_at": created,
                    }
                )
                self.repository.put_document_artifact(
                    {
                        "document_id": document_id,
                        "kind": "original",
                        "relpath": source_relpath,
                        "mime_type": mime_type
                        or mimetypes.guess_type(filename)[0]
                        or "application/octet-stream",
                        "size_bytes": len(content),
                        "sha256": digest,
                        "created_at": created,
                    }
                )
                self.repository.create_parse_task(
                    {
                        "id": str(uuid.uuid4()),
                        "document_id": document_id,
                        "attempt": 1,
                        "data_id": document_id,
                        "api_mode": parser_config["api_mode"],
                        "model_version": parser_config["model_version"],
                        "request_json": json.dumps(
                            build_parser_options(parser_config, extension),
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        "created_at": created,
                        "updated_at": created,
                    }
                )
            self.repository.mark_upload_files_committed(
                knowledge_base_id,
                operation_key,
                source_relpath,
                f"{document_id}.json",
                digest,
                "",
                _now(),
            )

        task = self.repository.get_latest_parse_task(document_id)
        if task is None:
            raise ApiError(
                "文档解析任务创建失败", status.HTTP_500_INTERNAL_SERVER_ERROR
            )
        self._schedule_submission(document_id)
        response = self.knowledge_bases.get(knowledge_base_id).model_copy(
            update={"document_id": document_id, "idempotent_replay": False}
        )
        return response

    def _submit_task(
        self,
        task: dict[str, Any],
        content: bytes,
        parser_config: dict[str, Any],
        *,
        schedule_poll: bool = True,
    ) -> None:
        client = self._mineru_client_factory(parser_config)
        try:
            self.repository.update_parse_task(
                task["id"],
                {"state": "uploading", "updated_at": _now(), "started_at": _now()},
            )
            if task.get("api_mode") == "self_hosted":
                upstream_task_id = client.submit_file(
                    filename=self._document_filename(str(task["document_id"])),
                    content=content,
                    model_version=str(task["model_version"]),
                    options=self._task_options(task),
                )
                self.repository.update_parse_task(
                    task["id"],
                    {
                        "task_id": upstream_task_id,
                        "state": "pending",
                        "updated_at": _now(),
                    },
                )
            else:
                ticket = client.request_upload_url(
                    filename=self._document_filename(str(task["document_id"])),
                    data_id=str(task["data_id"]),
                    model_version=str(task["model_version"]),
                    options=self._task_options(task),
                )
                self.repository.update_parse_task(
                    task["id"],
                    {
                        "batch_id": ticket.batch_id,
                        "state": "waiting-file",
                        "updated_at": _now(),
                    },
                )
                client.upload_file(ticket.upload_url, content)
        finally:
            client.close()
        if schedule_poll:
            self._schedule_poll(str(task["id"]))

    def _task_options(self, task: dict[str, Any]) -> dict[str, Any]:
        try:
            payload = json.loads(str(task["request_json"]))
        except (TypeError, ValueError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _document_filename(self, document_id: str) -> str:
        item = self._find_document(document_id)
        if item is None:
            raise KeyError(document_id)
        return str(item["original_filename"])

    def _schedule_poll(self, task_id: str) -> None:
        with self._jobs_lock:
            active = self._jobs.get(task_id)
            if active is not None and not active.done():
                return
            self._jobs[task_id] = self._poll_executor.submit(self._poll_task, task_id)

    def _schedule_submission(self, document_id: str) -> None:
        """Submit a persisted MinerU task without holding the upload request open."""
        task = self.repository.get_latest_parse_task(document_id)
        if task is None:
            return
        task_id = str(task["id"])
        with self._jobs_lock:
            active = self._jobs.get(task_id)
            if active is not None and not active.done():
                return
            self._jobs[task_id] = self._submission_executor.submit(
                self._submit_task_in_background, task_id
            )

    def _clear_job(self, task_id: str) -> None:
        with self._jobs_lock:
            self._jobs.pop(task_id, None)

    def _submit_task_in_background(self, task_id: str) -> None:
        task = self.repository.get_parse_task(task_id)
        if task is None:
            self._clear_job(task_id)
            return
        document = self._find_document(str(task["document_id"]))
        if document is None:
            self._clear_job(task_id)
            return
        try:
            item = self.knowledge_bases.require_record(
                str(document["knowledge_base_id"])
            )
            workspace = Path(str(item["workspace_dir"])).resolve()
            source = (workspace / str(document["source_relpath"])).resolve()
            source.relative_to(workspace)
            self._submit_task(
                task,
                source.read_bytes(),
                self.models.parser_runtime_config(),
                schedule_poll=False,
            )
            with self._jobs_lock:
                self._jobs[task_id] = self._poll_executor.submit(
                    self._poll_task, task_id
                )
        except Exception as exc:
            self._mark_failed(task, exc)
            self._clear_job(task_id)

    def _schedule_existing_task(self, document_id: str) -> None:
        task = self.repository.get_latest_parse_task(document_id)
        if task is None or task["state"] not in _ACTIVE_STATES:
            return
        if task.get("batch_id") or task.get("task_id"):
            self._schedule_poll(str(task["id"]))
        else:
            self._schedule_submission(document_id)

    def _poll_task(self, task_id: str) -> None:
        started = time.monotonic()
        task = self.repository.get_parse_task(task_id)
        if task is None or not (task.get("batch_id") or task.get("task_id")):
            return
        document = self._find_document(task["document_id"])
        if document is None:
            return
        parser_config = self.models.parser_runtime_config()
        client = self._client_for_task(task, parser_config)
        try:
            while True:
                try:
                    result = (
                        client.get_task_result(str(task["task_id"]))
                        if task.get("api_mode") == "self_hosted"
                        else client.get_batch_result(str(task["batch_id"]))
                    )
                except MineruError as exc:
                    if not (
                        task.get("api_mode") == "self_hosted" and exc.code == "HTTP_404"
                    ):
                        raise
                    self._resubmit_missing_self_hosted_task(task, parser_config)
                    refreshed = self.repository.get_parse_task(task_id)
                    if refreshed is None or not refreshed.get("task_id"):
                        raise MineruError("私有 MinerU 重提交后未返回 task_id")
                    task = refreshed
                    continue
                if result.state in {"done", "failed"}:
                    if result.state == "failed":
                        raise MineruError(
                            result.error_message or "MinerU 解析失败",
                            code=result.error_code,
                        )
                    if not result.full_zip_url:
                        raise MineruError("MinerU 已完成，但未返回解析结果 ZIP 地址")
                    self._persist_result(task, result.full_zip_url, client)
                    now = _now()
                    self.repository.update_parse_task(
                        task_id,
                        {
                            "state": "done",
                            "extracted_pages": result.extracted_pages,
                            "total_pages": result.total_pages,
                            "result_zip_url": None,
                            "error_code": None,
                            "error_message": None,
                            "updated_at": now,
                            "completed_at": now,
                        },
                    )
                    return
                state = result.state if result.state in _ACTIVE_STATES else "pending"
                self.repository.update_parse_task(
                    task_id,
                    {
                        "state": state,
                        "extracted_pages": result.extracted_pages,
                        "total_pages": result.total_pages,
                        "updated_at": _now(),
                    },
                )
                if (
                    time.monotonic() - started
                    >= self.settings.mineru_poll_timeout_seconds
                ):
                    raise MineruError("MinerU 解析任务轮询超时")
                if self.settings.mineru_poll_interval_seconds:
                    time.sleep(self.settings.mineru_poll_interval_seconds)
        except Exception as exc:
            self._mark_failed(task, exc)
        finally:
            client.close()
            self._clear_job(task_id)

    def _persist_result(
        self, task: dict[str, Any], result_url: str, client: Any
    ) -> None:
        document = self._find_document(task["document_id"])
        if document is None:
            raise KeyError(task["document_id"])
        workspace = Path(
            str(
                self.knowledge_bases.require_record(document["knowledge_base_id"])[
                    "workspace_dir"
                ]
            )
        )
        zip_content = client.download_result(result_url)
        if not zip_content or len(zip_content) > self.settings.max_upload_bytes:
            raise MineruError("MinerU 解析结果超过大小限制或为空")
        with workspace_lock(workspace):
            document = self._find_document(str(task["document_id"]))
            if document is None:
                raise KeyError(task["document_id"])
            parsed_root = workspace / "parsed" / str(document["id"])
            parsed_root.mkdir(parents=True, exist_ok=True)
            result_zip_relpath = f"parsed/{document['id']}/result.zip"
            WorkspaceArtifactStore.atomic_write(
                workspace / result_zip_relpath, zip_content
            )
            files = extract_result_archive(
                zip_content,
                parsed_root,
                max_member_bytes=self.settings.max_upload_bytes,
            )
            full_member = select_markdown_result(files, document, task)
            if full_member is None:
                raise MineruError("MinerU 解析结果缺少 Markdown 主文件")
            full_relpath = f"parsed/{document['id']}/full.md"
            full_content = full_member.read_bytes()
            WorkspaceArtifactStore.atomic_write(workspace / full_relpath, full_content)
            _, artifact_sha = self.artifacts.write_document(
                workspace,
                str(document["id"]),
                full_relpath,
                full_content,
                self._build_workspace_document(full_member, document),
            )

            self.repository.mark_upload_files_committed(
                document["knowledge_base_id"],
                self._upload_key(document["knowledge_base_id"], document["id"]),
                str(document["source_relpath"]),
                f"{document['id']}.json",
                str(document["source_sha256"]),
                artifact_sha,
                _now(),
            )
            now = _now()
            self.repository.put_document_artifact(
                {
                    "document_id": document["id"],
                    "kind": "result_zip",
                    "relpath": result_zip_relpath,
                    "mime_type": "application/zip",
                    "size_bytes": len(zip_content),
                    "sha256": _sha256(zip_content),
                    "created_at": now,
                }
            )
            self.repository.put_document_artifact(
                {
                    "document_id": document["id"],
                    "kind": "full_markdown",
                    "relpath": full_relpath,
                    "mime_type": "text/markdown",
                    "size_bytes": len(full_content),
                    "sha256": _sha256(full_content),
                    "created_at": now,
                }
            )
            content_list = next(
                (path for path in files if path.name.lower() == "content_list.json"),
                None,
            )
            if content_list is not None:
                content = content_list.read_bytes()
                relpath = f"parsed/{document['id']}/content_list.json"
                WorkspaceArtifactStore.atomic_write(workspace / relpath, content)
                self.repository.put_document_artifact(
                    {
                        "document_id": document["id"],
                        "kind": "content_list",
                        "relpath": relpath,
                        "mime_type": "application/json",
                        "size_bytes": len(content),
                        "sha256": _sha256(content),
                        "created_at": now,
                    }
                )
            content_version = self.repository.commit_upload(
                document["knowledge_base_id"],
                self._upload_key(document["knowledge_base_id"], document["id"]),
                self.artifacts.document_count(workspace),
                _now(),
            )
            self.repository.update_document(
                str(document["id"]),
                {
                    "status": "ready",
                    "parsed_markdown_relpath": full_relpath,
                    "parsed_content_version": content_version,
                    "error_code": None,
                    "error_message": None,
                    "updated_at": now,
                    "completed_at": now,
                },
            )
        if self.fts_schedule is not None and self.settings.fts_enabled:
            try:
                self.fts_schedule(str(document["knowledge_base_id"]))
            except Exception:
                # Parsing is complete even when optional FTS submission is unavailable.
                logger.exception(
                    "document.fts_schedule_failed knowledge_base_id=%s",
                    document["knowledge_base_id"],
                )
        if self.vector_schedule is not None:
            try:
                self.vector_schedule(str(document["knowledge_base_id"]))
            except Exception:
                logger.exception(
                    "document.vector_schedule_failed knowledge_base_id=%s",
                    document["knowledge_base_id"],
                )

    def _resubmit_missing_self_hosted_task(
        self, task: dict[str, Any], parser_config: dict[str, Any]
    ) -> None:
        """The in-memory mineru-api lost a task during its own restart."""
        document = self._find_document(str(task["document_id"]))
        if document is None:
            raise KeyError(task["document_id"])
        workspace = Path(
            str(
                self.knowledge_bases.require_record(document["knowledge_base_id"])[
                    "workspace_dir"
                ]
            )
        )
        source = _resolve_workspace_path(workspace, str(document["source_relpath"]))
        if source is None or not source.is_file():
            raise MineruError("原始文件不存在，无法重提交私有 MinerU 任务")
        self.repository.update_parse_task(
            str(task["id"]),
            {"task_id": None, "state": "created", "updated_at": _now()},
        )
        self._submit_task(task, source.read_bytes(), parser_config, schedule_poll=False)

    def _build_workspace_document(
        self, path: Path, document: dict[str, Any]
    ) -> dict[str, Any]:
        knowledge_base = self.knowledge_bases.require_record(
            str(document["knowledge_base_id"])
        )
        tree_options = self.artifacts.read_tree_build_options(
            Path(str(knowledge_base["workspace_dir"]))
        )
        if knowledge_base["summary_enabled"]:
            _, parsed = build_workspace_doc(
                str(path),
                llm=self.models.build_llm(),
                thin=tree_options.subtree_folding_enabled,
                min_node_token=tree_options.min_subtree_tokens,
            )
        else:
            _, parsed = build_workspace_doc(
                str(path),
                no_summary=True,
                thin=tree_options.subtree_folding_enabled,
                min_node_token=tree_options.min_subtree_tokens,
            )
        parsed["doc_name"] = document["original_filename"]
        return parsed

    def _find_document(self, document_id: str) -> dict[str, Any] | None:
        with self.repository.factory.session_scope() as session:
            from app.api_server.database.models import Document

            entity = session.get(Document, document_id)
            if entity is None:
                return None
            return {
                "id": entity.id,
                "knowledge_base_id": entity.knowledge_base_id,
                "original_filename": entity.original_filename,
                "source_relpath": entity.source_relpath,
                "source_sha256": entity.source_sha256,
            }

    def _upload_key(self, knowledge_base_id: str, document_id: str) -> str:
        with self.repository.factory.session_scope() as session:
            from app.api_server.database.models import UploadOperation

            operation = (
                session.query(UploadOperation)
                .filter_by(knowledge_base_id=knowledge_base_id, document_id=document_id)
                .first()
            )
            if operation is None:
                raise KeyError((knowledge_base_id, document_id))
            return operation.idempotency_key

    def _mark_failed(self, task: dict[str, Any], error: Exception) -> None:
        code = getattr(error, "code", None)
        message = str(error).strip() or error.__class__.__name__
        now = _now()
        try:
            self.repository.update_parse_task(
                str(task["id"]),
                {
                    "state": "failed",
                    "error_code": str(code) if code else None,
                    "error_message": message[:2_000],
                    "updated_at": now,
                    "completed_at": now,
                },
            )
            self.repository.update_document(
                str(task["document_id"]),
                {
                    "status": "failed",
                    "error_code": str(code) if code else None,
                    "error_message": message[:2_000],
                    "updated_at": now,
                },
            )
            document = self._find_document(str(task["document_id"]))
            if document is not None:
                self.repository.fail_upload(
                    str(document["knowledge_base_id"]),
                    self._upload_key(
                        str(document["knowledge_base_id"]), str(document["id"])
                    ),
                    message,
                    now,
                )
        except Exception:
            # Preserve the original failure; restart reconciliation can repair state.
            pass

    def list_documents(self, knowledge_base_id: str) -> list[dict[str, Any]]:
        self._materialize_legacy_markdown(knowledge_base_id)
        return [
            self._payload(item)
            for item in self.repository.list_documents(knowledge_base_id)
        ]

    def get_document(self, knowledge_base_id: str, document_id: str) -> dict[str, Any]:
        self._materialize_legacy_markdown(knowledge_base_id)
        item = self.repository.get_document(knowledge_base_id, document_id)
        if item is None:
            raise ApiError("文档不存在", status.HTTP_404_NOT_FOUND)
        return self._payload(item)

    def retry_document(
        self, knowledge_base_id: str, document_id: str
    ) -> dict[str, Any]:
        """Retry a failed MinerU parse using the already stored source file."""
        self._materialize_legacy_markdown(knowledge_base_id)
        document = self.repository.get_document(knowledge_base_id, document_id)
        if document is None:
            raise ApiError("文档不存在", status.HTTP_404_NOT_FOUND)
        if document.get("parser") != "mineru":
            raise ApiError("仅支持重试 PDF 或 Word 文档解析", status.HTTP_409_CONFLICT)
        if document.get("status") != "failed":
            raise ApiError("只有解析失败的文档可以重试", status.HTTP_409_CONFLICT)

        record = self.knowledge_bases.require_record(knowledge_base_id)
        workspace = Path(str(record["workspace_dir"])).resolve()
        source = _resolve_workspace_path(workspace, str(document["source_relpath"]))
        if source is None or not source.is_file():
            raise ApiError("原始文件不存在，无法重试解析", status.HTTP_409_CONFLICT)

        parser_config = self.models.parser_runtime_config()
        now = _now()
        self.repository.retry_parse_task(
            document_id,
            str(parser_config["api_mode"]),
            str(parser_config["model_version"]),
            json.dumps(
                build_parser_options(parser_config, str(document["file_extension"])),
                ensure_ascii=False,
                sort_keys=True,
            ),
            now,
        )
        self._schedule_submission(document_id)
        return self._payload(
            self.repository.get_document(knowledge_base_id, document_id) or document
        )

    def read_content(self, knowledge_base_id: str, document_id: str) -> str:
        self._materialize_legacy_markdown(knowledge_base_id)
        item = self.repository.get_document(knowledge_base_id, document_id)
        if item is None:
            raise ApiError("文档不存在", status.HTTP_404_NOT_FOUND)
        relpath = item.get("parsed_markdown_relpath")
        if not relpath:
            raise ApiError("文档尚未完成解析", status.HTTP_409_CONFLICT)
        workspace = Path(
            str(self.knowledge_bases.require_record(knowledge_base_id)["workspace_dir"])
        )
        path = _resolve_workspace_path(workspace, str(relpath))
        if path is None:
            raise ApiError("文档内容路径越界", status.HTTP_500_INTERNAL_SERVER_ERROR)
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise ApiError("文档解析内容不存在", status.HTTP_404_NOT_FOUND) from exc

    def read_tree(self, knowledge_base_id: str, document_id: str) -> dict[str, Any]:
        """Return the document's heading-index tree without per-node bodies.

        The workspace artifact also stores each node's ``text``; it duplicates the
        parsed Markdown already served by ``read_content``, so it is stripped to
        keep the reader payload proportional to the outline rather than the doc.
        """
        self._materialize_legacy_markdown(knowledge_base_id)
        item = self.repository.get_document(knowledge_base_id, document_id)
        if item is None:
            raise ApiError("文档不存在", status.HTTP_404_NOT_FOUND)
        if not item.get("parsed_markdown_relpath"):
            raise ApiError("文档尚未完成解析", status.HTTP_409_CONFLICT)
        workspace = Path(
            str(self.knowledge_bases.require_record(knowledge_base_id)["workspace_dir"])
        )
        artifact = workspace / f"{document_id}.json"
        try:
            document = json.loads(artifact.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ApiError("文档索引树不存在", status.HTTP_404_NOT_FOUND) from exc
        except (OSError, ValueError) as exc:
            raise ApiError(
                "文档索引树不可读", status.HTTP_500_INTERNAL_SERVER_ERROR
            ) from exc
        return {
            "doc_name": document.get("doc_name"),
            "doc_description": document.get("doc_description"),
            "line_count": document.get("line_count"),
            "structure": _strip_index_text(document.get("structure")),
        }

    def delete_document(self, knowledge_base_id: str, document_id: str) -> None:
        """Hard-delete a document and all API-owned ingestion artifacts.

        Index cleanup is surgical when the index was ready at delete time: the
        deleted document's records are removed from Milvus by ``doc_id`` and the
        index revision advanced, avoiding a full rebuild. If the surgical delete
        is skipped (index not ready) or fails (Milvus unavailable / collection
        missing), the index is left pending and a rebuild is scheduled as fallback
        (the rebuild rebuilds from ``_meta.json``, which no longer lists the doc).
        """
        self._materialize_legacy_markdown(knowledge_base_id)
        document = self.repository.get_document(knowledge_base_id, document_id)
        if document is None:
            raise ApiError("文档不存在", status.HTTP_404_NOT_FOUND)

        record = self.knowledge_bases.require_record(knowledge_base_id)
        workspace = Path(str(record["workspace_dir"]))
        # Snapshot index state BEFORE repository.delete_document sets it pending.
        # The surgical-delete decision hinges on whether the index was ready (i.e.
        # the collection actually held this document's records); an index that was
        # already pending/building/failed is left to its in-flight or scheduled
        # rebuild, which rebuilds from _meta.json excluding the deleted document.
        expected_revision = int(record.get("content_version", 0)) + 1
        fts_was_ready = record.get("fts_status") == "ready"
        fts_collection = record.get("fts_collection")
        vector_was_ready = record.get("vector_status") == "ready"
        vector_collection = record.get("vector_collection")
        vector_model_id = record.get("vector_model_id")
        vector_model_updated_at = record.get("vector_model_updated_at")
        vector_dimension = record.get("vector_dimension")

        artifacts = self.repository.list_document_artifacts(document_id)
        with workspace_lock(workspace):
            self.artifacts.delete_document(
                workspace,
                document_id,
                source_relpath=str(document["source_relpath"]),
                artifact_relpaths=[str(item["relpath"]) for item in artifacts],
            )
            if not self.repository.delete_document(
                knowledge_base_id, document_id, _now()
            ):
                raise ApiError("文档不存在", status.HTTP_404_NOT_FOUND)

        # Surgical Milvus delete BEFORE advancing the revision, so a crash between
        # the two leaves the revision behind (conservative rebuild) rather than
        # "revision advanced but records linger".
        fts_advanced = self._surgical_delete_fts(
            knowledge_base_id,
            document_id,
            fts_was_ready,
            fts_collection,
            expected_revision,
        )
        vector_advanced = self._surgical_delete_vector(
            knowledge_base_id,
            document_id,
            vector_was_ready,
            vector_collection,
            vector_model_id,
            vector_model_updated_at,
            vector_dimension,
            expected_revision,
        )

        if (
            not fts_advanced
            and self.fts_schedule is not None
            and self.settings.fts_enabled
        ):
            try:
                self.fts_schedule(knowledge_base_id, force=True)
            except Exception:
                logger.exception(
                    "document.fts_schedule_failed knowledge_base_id=%s",
                    knowledge_base_id,
                )
        if not vector_advanced and self.vector_schedule is not None:
            try:
                self.vector_schedule(knowledge_base_id, force=True)
            except Exception:
                logger.exception(
                    "document.vector_schedule_failed knowledge_base_id=%s",
                    knowledge_base_id,
                )

    def _surgical_delete_fts(
        self,
        knowledge_base_id: str,
        document_id: str,
        was_ready: bool,
        collection_name: str | None,
        expected_revision: int,
    ) -> bool:
        """Remove a deleted document's FTS records and advance the index revision.

        Returns True when the index is consistent at ``expected_revision`` (the
        surgical delete succeeded and the version gate held); False when the caller
        should fall back to scheduling a rebuild. Only applies when the FTS index
        was ready at delete time -- otherwise a rebuild is already pending or in
        flight.
        """
        if not was_ready or not collection_name:
            return False
        try:
            store = NodeFtsStore(
                uri=self.settings.milvus_uri,
                token=self.settings.milvus_token,
                collection_name=collection_name,
                knowledge_base_id=knowledge_base_id,
            )
            if not store.client.has_collection(collection_name):
                return False
            store.delete_by_doc(document_id)
        except Exception:
            logger.warning(
                "document.fts_surgical_delete_failed knowledge_base_id=%s document_id=%s",
                knowledge_base_id,
                document_id,
                exc_info=True,
            )
            return False
        return self.repository.advance_fts_revision_after_surgical_delete(
            knowledge_base_id, expected_revision, collection_name, _now()
        )

    def _surgical_delete_vector(
        self,
        knowledge_base_id: str,
        document_id: str,
        was_ready: bool,
        collection_name: str | None,
        model_id: str | None,
        model_updated_at: str | None,
        dimension: int | None,
        expected_revision: int,
    ) -> bool:
        """Remove a deleted document's vectors and advance the index revision.

        See ``_surgical_delete_fts``; the vector side additionally gates on the
        model fingerprint so a concurrent embedding-model change leaves the index
        pending for a full rebuild at the new model. The surgical delete still
        removes the stale vectors, which is harmless since that rebuild recreates
        the collection.
        """
        if not was_ready or not collection_name or not dimension:
            return False
        try:
            store = DocVectorStore(
                uri=self.settings.milvus_uri,
                token=self.settings.milvus_token,
                collection_name=collection_name,
                dimension=int(dimension),
                knowledge_base_id=knowledge_base_id,
            )
            if not store.client.has_collection(collection_name):
                return False
            store.delete_by_doc(document_id)
        except Exception:
            logger.warning(
                "document.vector_surgical_delete_failed knowledge_base_id=%s document_id=%s",
                knowledge_base_id,
                document_id,
                exc_info=True,
            )
            return False
        return self.repository.advance_vector_revision_after_surgical_delete(
            knowledge_base_id,
            expected_revision,
            collection_name,
            str(model_id or ""),
            str(model_updated_at or ""),
            int(dimension),
            _now(),
        )

    def _payload(self, item: dict[str, Any]) -> dict[str, Any]:
        task = self.repository.get_latest_parse_task(str(item["id"]))
        artifacts = self.repository.list_document_artifacts(str(item["id"]))
        return {
            **item,
            "latest_task": (
                {
                    key: value
                    for key, value in task.items()
                    if key not in {"request_json", "result_zip_url"}
                }
                if task is not None
                else None
            ),
            "artifacts": [
                {key: value for key, value in artifact.items() if key != "sha256"}
                for artifact in artifacts
            ],
        }

    def _materialize_legacy_markdown(self, knowledge_base_id: str) -> int:
        item = self.knowledge_bases.require_record(knowledge_base_id)
        workspace = Path(str(item["workspace_dir"]))
        try:
            manifest = json.loads(
                (workspace / "_meta.json").read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            return 0
        created_count = 0
        for document_id, entry in manifest.items():
            if (
                self.repository.get_document(knowledge_base_id, str(document_id))
                is not None
            ):
                continue
            if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
                continue
            source = _resolve_workspace_path(workspace, str(entry["path"]))
            if source is None or not source.is_file() or source.stat().st_size <= 0:
                continue
            content = source.read_bytes()
            now = _now()
            try:
                self.repository.create_document(
                    {
                        "id": str(document_id),
                        "knowledge_base_id": knowledge_base_id,
                        "original_filename": str(entry.get("doc_name") or source.name),
                        "file_extension": ".md",
                        "mime_type": "text/markdown",
                        "size_bytes": len(content),
                        "source_relpath": str(entry["path"]),
                        "source_sha256": _sha256(content),
                        "parser": "native_markdown",
                        "status": "ready",
                        "parsed_markdown_relpath": str(entry["path"]),
                        "parsed_content_version": item["content_version"] or 1,
                        "created_at": now,
                        "updated_at": now,
                        "completed_at": now,
                    }
                )
                created_count += 1
            except Exception:
                continue
        return created_count

    def recover_workspace_documents(self) -> None:
        """Materialize durable workspace entries before index recovery runs."""
        for item in self.repository.list("knowledge_bases"):
            try:
                created_count = self._materialize_legacy_markdown(str(item["id"]))
                if created_count:
                    logger.info(
                        "document.workspace_recovered knowledge_base_id=%s count=%s",
                        item["id"],
                        created_count,
                    )
            except Exception:
                logger.exception(
                    "document.workspace_recovery_failed knowledge_base_id=%s",
                    item["id"],
                )

    def recover(self) -> None:
        for task in self.repository.list_active_parse_tasks():
            if task.get("batch_id") or task.get("task_id"):
                self._schedule_poll(str(task["id"]))
                continue
            self._schedule_submission(str(task["document_id"]))
        for document in self.repository.list_parsing_documents():
            document_id = str(document["id"])
            task = self.repository.get_latest_parse_task(document_id)
            if task is None:
                try:
                    parser_config = self.models.parser_runtime_config()
                    now = _now()
                    task = self.repository.create_parse_task(
                        {
                            "id": str(uuid.uuid4()),
                            "document_id": document_id,
                            "attempt": 1,
                            "data_id": document_id,
                            "api_mode": parser_config["api_mode"],
                            "model_version": parser_config["model_version"],
                            "request_json": json.dumps(
                                build_parser_options(
                                    parser_config, str(document["file_extension"])
                                ),
                                ensure_ascii=False,
                                sort_keys=True,
                            ),
                            "created_at": now,
                            "updated_at": now,
                        }
                    )
                except Exception:
                    logger.exception(
                        "document.recover_task_creation_failed document_id=%s",
                        document_id,
                    )
                    continue
            if task.get("batch_id") or task.get("task_id"):
                self._schedule_poll(str(task["id"]))
            elif task.get("state") in _ACTIVE_STATES:
                self._schedule_submission(document_id)

    def shutdown(self) -> None:
        self._submission_executor.shutdown(wait=False, cancel_futures=True)
        self._poll_executor.shutdown(wait=False, cancel_futures=True)


__all__ = ["DocumentIngestionService", "MINERU_EXTENSIONS", "SUPPORTED_EXTENSIONS"]
