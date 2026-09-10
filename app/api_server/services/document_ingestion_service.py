"""Document upload, MinerU ingestion, and document detail services."""

from __future__ import annotations

import hashlib
import json
import logging
import mimetypes
import shutil
import threading
import uuid
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
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
from app.api_server.services.document_pipeline_service import DocumentPipelineService
from app.api_server.services.documents.pipeline_contracts import (
    NormalizationChunk,
    NormalizationInput,
)
from app.api_server.services.documents.pdf_chunks import (
    PdfChunk,
    PdfChunkError,
    split_pdf,
)
from app.api_server.services.documents.mineru_artifacts import (
    extract_result_archive,
    select_markdown_result,
)
from app.api_server.services.documents.mineru_tasks import build_parser_options
from app.api_server.services.workspace_store import (
    WorkspaceArtifactStore,
    workspace_lock,
)
from app.api_server.services.workspace_snapshot import (
    publish_document_deletion_snapshot,
)
from nianlun.indexing.fts.store import NodeFtsStore
from nianlun.indexing.tree.workspace import build_workspace_doc
from nianlun.indexing.vector.store import DocVectorStore
from nianlun.knowledgebase.workspace_view import WorkspaceView, WorkspaceViewError


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


class PdfChunkPlanIncompatibleError(PdfChunkError):
    """Existing partial tasks cannot safely be reconciled with current settings."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _after(seconds: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def _safe_filename(filename: str, default: str = "document") -> str:
    name = Path(filename).name.strip().replace("\x00", "")
    if not name:
        name = default
    return name[:240]


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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
            max_workers=settings.mineru_submit_workers,
            thread_name_prefix="nianlun-mineru-submit",
        )
        self._poll_executor = ThreadPoolExecutor(
            max_workers=settings.mineru_poll_workers,
            thread_name_prefix="nianlun-mineru-poll",
        )
        self._jobs: dict[str, Future[Any]] = {}
        self._jobs_lock = threading.RLock()
        self._mineru_client_factory = (
            mineru_client_factory or self._create_mineru_client
        )
        self.pipeline = DocumentPipelineService(
            repository,
            knowledge_bases,
            models,
            settings,
            fts_schedule=fts_schedule,
            vector_schedule=vector_schedule,
        )
        self._parse_owner = f"parse-{uuid.uuid4()}"
        self._dispatcher_wake = threading.Event()
        self._dispatcher_stop = threading.Event()
        self._dispatcher_thread = threading.Thread(
            target=self._dispatcher_loop,
            name="nianlun-parse-dispatcher",
            daemon=True,
        )
        self._dispatcher_thread.start()

    def _dispatcher_loop(self) -> None:
        interval = max(0.05, min(self.settings.mineru_poll_interval_seconds, 1.0))
        while not self._dispatcher_stop.is_set():
            self._dispatcher_wake.wait(timeout=interval)
            self._dispatcher_wake.clear()
            if self._dispatcher_stop.is_set():
                return
            try:
                for task in self.repository.list_active_parse_tasks():
                    if task.get("batch_id") or task.get("task_id"):
                        self._schedule_poll(str(task["id"]))
                    else:
                        self._schedule_task_submission(str(task["id"]))
            except Exception:
                logger.exception("document.parse_dispatch_failed")

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
        if prebuilt_document is not None:
            return self._upload_prebuilt_markdown(
                knowledge_base_id,
                filename,
                content,
                mime_type,
                idempotency_key,
                prebuilt_document,
            )
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
                self._queue_markdown_normalization(existing, created)
                self.pipeline.wake()
                return self.knowledge_bases.get(knowledge_base_id).model_copy(
                    update={"document_id": existing["id"], "idempotent_replay": True}
                )
            operation = self.repository.get_upload(knowledge_base_id, operation_key)
            if operation is not None:
                if operation["request_sha256"] != request_sha256:
                    raise ApiError(
                        "Idempotency-Key 已用于其他上传内容",
                        status.HTTP_409_CONFLICT,
                    )
                document_id = str(operation["document_id"])
                stored = self.repository.get_document(knowledge_base_id, document_id)
                if stored is not None:
                    self.pipeline.wake()
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
            source_path = workspace / source_relpath
            source_path.parent.mkdir(parents=True, exist_ok=True)
            WorkspaceArtifactStore.atomic_write(source_path, content)
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
                    "status": "parsing",
                    "current_stage": "normalize",
                    "stage_state": "queued",
                    "created_at": created,
                    "updated_at": created,
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
                    "pipeline_generation": 1,
                    "created_at": created,
                }
            )
            self.repository.mark_upload_files_committed(
                knowledge_base_id,
                operation_key,
                source_relpath,
                "",
                digest,
                "",
                created,
            )
            document = self.repository.get_document(knowledge_base_id, document_id)
            if document is None:
                raise RuntimeError("Markdown 文档创建后不可见")
            self._queue_markdown_normalization(document, created)
        self.pipeline.wake()
        return self.knowledge_bases.get(knowledge_base_id).model_copy(
            update={"document_id": document_id, "idempotent_replay": False}
        )

    def _upload_prebuilt_markdown(
        self,
        knowledge_base_id: str,
        filename: str,
        content: bytes,
        mime_type: str | None,
        idempotency_key: str | None,
        prebuilt_document: dict[str, Any],
    ) -> Any:
        item = self.knowledge_bases.require_record(knowledge_base_id)
        workspace = Path(str(item["workspace_dir"]))
        with workspace_lock(workspace):
            return self.knowledge_bases.add_markdown(
                knowledge_base_id,
                filename,
                content,
                idempotency_key,
                workspace_locked=True,
                prebuilt_document=prebuilt_document,
            )

    def _queue_markdown_normalization(
        self, document: dict[str, Any], now: str
    ) -> None:
        """Idempotently enqueue the native-Markdown normalize handoff."""
        document_id = str(document["id"])
        generation = int(document["pipeline_generation"])
        if (
            document.get("parser") != "native_markdown"
            or document.get("status") != "parsing"
            or document.get("current_stage") != "normalize"
            or document.get("stage_state") != "queued"
            or document.get("parsed_content_version") is not None
        ):
            return
        if self.repository.get_latest_normalization_task(document_id, generation):
            return
        normalization = NormalizationInput(
            document_id=document_id,
            pipeline_generation=generation,
            chunks=[
                NormalizationChunk(
                    task_id=f"markdown:{document_id}",
                    chunk_index=0,
                    markdown_relpath=str(document["source_relpath"]),
                    output_sha256=str(document["source_sha256"]),
                )
            ],
        )
        self.repository.create_normalization_task(
            document_id=document_id,
            pipeline_generation=generation,
            input_manifest_json=normalization.model_dump_json(),
            now=now,
        )
        self.repository.append_pipeline_event(
            document_id=document_id,
            pipeline_generation=generation,
            stage="normalize",
            event_type="queued",
            now=now,
        )

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
                        "current_stage": "parse",
                        "stage_state": "queued",
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
            self.repository.mark_upload_files_committed(
                knowledge_base_id,
                operation_key,
                source_relpath,
                f"{document_id}.json",
                digest,
                "",
                _now(),
            )

        self._schedule_preparation(document_id, parser_config)
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
        lease_token: str,
        schedule_poll: bool = True,
    ) -> None:
        client = self._mineru_client_factory(parser_config)
        try:
            if not self.repository.update_claimed_parse_task(
                task["id"],
                lease_token,
                {
                    "state": "uploading",
                    "dispatch_state": "leased",
                    "updated_at": _now(),
                    "started_at": _now(),
                },
            ):
                return
            if task.get("api_mode") == "self_hosted":
                upstream_task_id = client.submit_file(
                    filename=self._document_filename(str(task["document_id"])),
                    content=content,
                    model_version=str(task["model_version"]),
                    options=self._task_options(task),
                )
                updated = self.repository.update_claimed_parse_task(
                    task["id"],
                    lease_token,
                    {
                        "task_id": upstream_task_id,
                        "state": "pending",
                        "dispatch_state": "waiting",
                        "next_poll_at": _now(),
                        "updated_at": _now(),
                    },
                    release=True,
                )
            else:
                ticket = client.request_upload_url(
                    filename=self._document_filename(str(task["document_id"])),
                    data_id=str(task["data_id"]),
                    model_version=str(task["model_version"]),
                    options=self._task_options(task),
                )
                client.upload_file(ticket.upload_url, content)
                updated = self.repository.update_claimed_parse_task(
                    task["id"],
                    lease_token,
                    {
                        "batch_id": ticket.batch_id,
                        "state": "waiting-file",
                        "dispatch_state": "waiting",
                        "next_poll_at": _now(),
                        "updated_at": _now(),
                    },
                    release=True,
                )
        finally:
            client.close()
        if schedule_poll and updated:
            self._schedule_poll(str(task["id"]))

    def _schedule_preparation(
        self, document_id: str, parser_config: dict[str, Any] | None = None
    ) -> None:
        key = f"prepare:{document_id}"
        with self._jobs_lock:
            active = self._jobs.get(key)
            if active is not None and not active.done():
                return
            self._jobs[key] = self._submission_executor.submit(
                self._prepare_parse_tasks,
                document_id,
                parser_config or self.models.parser_runtime_config(),
            )

    @staticmethod
    def _serialize_pdf_chunk_plan(
        document: dict[str, Any], generation: int, chunks: list[PdfChunk]
    ) -> str:
        return json.dumps(
            {
                "pipeline_generation": generation,
                "source_sha256": str(document["source_sha256"]),
                "chunks": [
                    {
                        "chunk_index": chunk.chunk_index,
                        "source_page_start": chunk.source_page_start,
                        "source_page_end": chunk.source_page_end,
                        "relpath": chunk.relpath,
                        "sha256": chunk.sha256,
                        "size_bytes": chunk.size_bytes,
                    }
                    for chunk in chunks
                ],
            },
            ensure_ascii=False,
            sort_keys=True,
        )

    def _load_pdf_chunk_plan(
        self,
        document: dict[str, Any],
        workspace: Path,
        generation: int,
    ) -> list[PdfChunk] | None:
        raw_plan = document.get("parse_plan_json")
        if not isinstance(raw_plan, str) or not raw_plan:
            return None
        try:
            plan = json.loads(raw_plan)
            entries = plan["chunks"]
            if (
                not isinstance(plan, dict)
                or int(plan["pipeline_generation"]) != generation
                or plan["source_sha256"] != document["source_sha256"]
                or not isinstance(entries, list)
                or not entries
            ):
                raise ValueError
            chunks = [
                PdfChunk(
                    chunk_index=int(entry["chunk_index"]),
                    source_page_start=(
                        int(entry["source_page_start"])
                        if entry["source_page_start"] is not None
                        else None
                    ),
                    source_page_end=(
                        int(entry["source_page_end"])
                        if entry["source_page_end"] is not None
                        else None
                    ),
                    relpath=(
                        str(entry["relpath"])
                        if entry["relpath"] is not None
                        else None
                    ),
                    sha256=str(entry["sha256"]),
                    size_bytes=int(entry["size_bytes"]),
                )
                for entry in entries
            ]
            if [chunk.chunk_index for chunk in chunks] != list(range(len(chunks))):
                raise ValueError
        except (KeyError, TypeError, ValueError) as exc:
            raise PdfChunkError("PDF 分段计划无效，请重试以创建新的解析 generation") from exc
        for chunk in chunks:
            if chunk.relpath is None:
                continue
            path = (workspace / chunk.relpath).resolve()
            try:
                path.relative_to(workspace)
            except ValueError as exc:
                raise PdfChunkError("PDF 分段计划路径越界") from exc
            if not path.is_file() or _sha256_file(path) != chunk.sha256:
                raise PdfChunkError("PDF 分段计划产物缺失或不一致，请重试")
        return chunks

    @staticmethod
    def _plan_matches_existing_tasks(
        chunks: list[PdfChunk], tasks: list[dict[str, Any]]
    ) -> bool:
        latest_by_chunk: dict[int, dict[str, Any]] = {}
        for task in tasks:
            latest_by_chunk.setdefault(int(task["chunk_index"]), task)
        if not set(latest_by_chunk).issubset(range(len(chunks))):
            return False
        for chunk_index, task in latest_by_chunk.items():
            chunk = chunks[chunk_index]
            if int(task.get("chunk_count", 1)) != len(chunks):
                return False
            if len(chunks) == 1 and task.get("source_page_start") is None:
                continue
            if (
                task.get("source_page_start") != chunk.source_page_start
                or task.get("source_page_end") != chunk.source_page_end
                or (
                    task.get("chunk_source_relpath") is not None
                    and task["chunk_source_relpath"] != chunk.relpath
                )
                or (
                    task.get("input_sha256") is not None
                    and task["input_sha256"] != chunk.sha256
                )
            ):
                return False
        return True

    def _prepare_parse_tasks(
        self, document_id: str, parser_config: dict[str, Any]
    ) -> None:
        key = f"prepare:{document_id}"
        document = self._find_document(document_id)
        if document is None:
            self._clear_job(key)
            return
        try:
            kb = self.knowledge_bases.require_record(str(document["knowledge_base_id"]))
            workspace = Path(str(kb["workspace_dir"])).resolve()
            source = (workspace / str(document["source_relpath"])).resolve()
            source.relative_to(workspace)
            content = source.read_bytes()
            generation = int(document.get("pipeline_generation", 1))
            existing_tasks = self.repository.list_parse_tasks(document_id, generation)
            if document["file_extension"] == ".pdf":
                options = build_parser_options(parser_config, ".pdf")
                page_ranges = str(options.pop("page_ranges", "") or "")
                chunks = self._load_pdf_chunk_plan(document, workspace, generation)
                if chunks is None:
                    chunks = split_pdf(
                        content,
                        workspace=workspace,
                        document_id=document_id,
                        generation=generation,
                        page_ranges=page_ranges,
                        max_pages=self.settings.pdf_chunk_max_pages,
                        max_chunks=self.settings.pdf_max_chunks,
                    )
                    if not self._plan_matches_existing_tasks(chunks, existing_tasks):
                        raise PdfChunkPlanIncompatibleError(
                            "PDF 分段计划缺失且当前配置不兼容，正在创建新的解析 generation"
                        )
                    plan_json = self._serialize_pdf_chunk_plan(
                        document, generation, chunks
                    )
                    now = _now()
                    self.repository.update_document(
                        document_id,
                        {"parse_plan_json": plan_json, "updated_at": now},
                    )
                    document["parse_plan_json"] = plan_json
            else:
                options = build_parser_options(
                    parser_config, str(document["file_extension"])
                )
                chunks = [
                    PdfChunk(
                        chunk_index=0,
                        source_page_start=None,
                        source_page_end=None,
                        relpath=None,
                        sha256=_sha256(content),
                        size_bytes=len(content),
                    )
                ]
            existing = {
                int(item["chunk_index"]) for item in existing_tasks
            }
            now = _now()
            total_pages = sum(
                (
                    int(chunk.source_page_end) - int(chunk.source_page_start) + 1
                    if chunk.source_page_start is not None
                    and chunk.source_page_end is not None
                    else 0
                )
                for chunk in chunks
            )
            for chunk in chunks:
                if chunk.relpath is not None:
                    if not self.repository.reserve_staging_bytes(
                        document_id=document_id,
                        pipeline_generation=generation,
                        allocation_key=f"parse:{chunk.chunk_index:04d}:pdf",
                        kind="chunk_pdf",
                        bytes_reserved=int(chunk.size_bytes),
                        maximum_bytes=self.settings.document_staging_max_bytes,
                        now=now,
                    ):
                        raise PdfChunkError("PDF 分段累计大小超过 staging 限制")
                    self.repository.put_document_artifact(
                        {
                            "document_id": document_id,
                            "kind": "parse_chunk_source",
                            "relpath": chunk.relpath,
                            "mime_type": "application/pdf",
                            "size_bytes": chunk.size_bytes,
                            "sha256": chunk.sha256,
                            "pipeline_generation": generation,
                            "created_at": now,
                        }
                    )
                if chunk.chunk_index in existing:
                    continue
                task = self.repository.create_parse_task(
                    {
                        "id": str(uuid.uuid4()),
                        "document_id": document_id,
                        "attempt": 1,
                        "pipeline_generation": generation,
                        "chunk_index": chunk.chunk_index,
                        "chunk_count": len(chunks),
                        "source_page_start": chunk.source_page_start,
                        "source_page_end": chunk.source_page_end,
                        "chunk_source_relpath": chunk.relpath,
                        "input_sha256": chunk.sha256,
                        "data_id": (
                            f"{document_id}:g{generation}:c{chunk.chunk_index:04d}"
                        ),
                        "api_mode": parser_config["api_mode"],
                        "model_version": parser_config["model_version"],
                        "request_json": json.dumps(
                            options, ensure_ascii=False, sort_keys=True
                        ),
                        "created_at": now,
                        "updated_at": now,
                        "available_at": now,
                    }
                )
                self._schedule_task_submission(str(task["id"]))
            self.repository.update_document(
                document_id,
                {
                    "current_stage": "parse",
                    "stage_state": "running",
                    "progress_completed": 0,
                    "progress_total": total_pages or len(chunks),
                    "progress_unit": "pages" if total_pages else "chunks",
                    "updated_at": now,
                },
            )
            self.repository.append_pipeline_event(
                document_id=document_id,
                pipeline_generation=generation,
                stage="parse",
                event_type="chunks_created",
                now=now,
                progress_completed=0,
                progress_total=total_pages or len(chunks),
            )
        except Exception as exc:
            code = getattr(exc, "code", None)
            message = str(exc).strip() or exc.__class__.__name__
            generation = int(document.get("pipeline_generation", 1))
            failed = self.repository.fail_document_parse_preflight(
                document_id,
                generation,
                error_code=str(code) if code else type(exc).__name__,
                error_message=message,
                now=_now(),
            )
            if failed and isinstance(exc, PdfChunkPlanIncompatibleError):
                try:
                    self.repository.restart_parse_preparation(
                        document_id, generation, _now()
                    )
                except ValueError:
                    logger.exception(
                        "document.parse_plan_generation_restart_failed document_id=%s",
                        document_id,
                    )
                else:
                    self._prepare_parse_tasks(document_id, parser_config)
                    return
            self._fail_upload_for_document(document_id, message)
        finally:
            self._clear_job(key)

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
        tasks = self.repository.list_parse_tasks(document_id)
        for task in tasks:
            if task["state"] in _ACTIVE_STATES and not (
                task.get("batch_id") or task.get("task_id")
            ):
                self._schedule_task_submission(str(task["id"]))

    def _schedule_task_submission(self, task_id: str) -> None:
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
        task = self.repository.claim_parse_task(
            task_id,
            owner=f"{self._parse_owner}:submit",
            mode="submit",
            now=_now(),
            lease_expires_at=_after(self.settings.pipeline_lease_seconds),
        )
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
            source = (
                workspace
                / str(task.get("chunk_source_relpath") or document["source_relpath"])
            ).resolve()
            source.relative_to(workspace)
            self._submit_task(
                task,
                source.read_bytes(),
                self.models.parser_runtime_config(),
                lease_token=str(task["lease_token"]),
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
        document = self._find_document(document_id)
        if document is None:
            return
        generation = int(document["pipeline_generation"])
        tasks = [
            task
            for task in self.repository.list_parse_tasks(document_id)
            if int(task.get("pipeline_generation", 1)) == generation
        ]
        expected_chunk_indexes = set(
            range(max((int(task.get("chunk_count", 1)) for task in tasks), default=0))
        )
        if not tasks or {
            int(task.get("chunk_index", 0)) for task in tasks
        } != expected_chunk_indexes:
            self._schedule_preparation(document_id)
        for task in tasks:
            if (
                task["state"] == "done"
                and (
                    not task.get("result_root_relpath") or not task.get("output_sha256")
                )
                and (task.get("batch_id") or task.get("task_id"))
            ):
                self.repository.update_parse_task(
                    str(task["id"]),
                    {
                        "state": "pending",
                        "dispatch_state": "waiting",
                        "next_poll_at": _now(),
                        "completed_at": None,
                        "updated_at": _now(),
                    },
                )
                self._schedule_poll(str(task["id"]))
                continue
            if task["state"] not in _ACTIVE_STATES:
                continue
            if task.get("batch_id") or task.get("task_id"):
                self._schedule_poll(str(task["id"]))
            else:
                self._schedule_task_submission(str(task["id"]))
        # Replay the idempotent parse -> normalize handoff after a restart.
        self._queue_normalization_if_complete(document_id, generation)

    def _parse_lease_maintenance_interval(self) -> float:
        return max(
            0.1,
            min(30.0, self.settings.pipeline_lease_seconds / 3),
        )

    def _renew_parse_task_lease(self, task: dict[str, Any]) -> bool:
        return self.repository.renew_claimed_parse_task_lease(
            str(task["id"]),
            str(task["lease_token"]),
            lease_expires_at=_after(self.settings.pipeline_lease_seconds),
            now=_now(),
        )

    def _maintain_parse_task_lease(
        self,
        task: dict[str, Any],
        stop: threading.Event,
        lost: threading.Event,
    ) -> None:
        interval = self._parse_lease_maintenance_interval()
        while not stop.wait(interval):
            try:
                renewed = self._renew_parse_task_lease(task)
            except Exception:
                logger.exception(
                    "document.parse_lease_heartbeat_failed task_id=%s", task["id"]
                )
                lost.set()
                return
            if not renewed:
                lost.set()
                logger.warning("document.parse_lease_lost task_id=%s", task["id"])
                return

    def _poll_task(self, task_id: str) -> None:
        task = self.repository.claim_parse_task(
            task_id,
            owner=f"{self._parse_owner}:poll",
            mode="poll",
            now=_now(),
            lease_expires_at=_after(self.settings.pipeline_lease_seconds),
        )
        if task is None or not (task.get("batch_id") or task.get("task_id")):
            self._clear_job(task_id)
            return
        document = self._find_document(task["document_id"])
        if document is None:
            return
        parser_config = self.models.parser_runtime_config()
        client = self._client_for_task(task, parser_config)
        try:
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
                self.repository.update_claimed_parse_task(
                    task_id,
                    str(task["lease_token"]),
                    {
                        "task_id": None,
                        "state": "created",
                        "dispatch_state": "queued",
                        "available_at": _now(),
                        "next_poll_at": None,
                        "updated_at": _now(),
                    },
                    release=True,
                )
                return
            if result.state in {"done", "failed"}:
                if result.state == "failed":
                    raise MineruError(
                        result.error_message or "MinerU 解析失败",
                        code=result.error_code,
                    )
                if not result.full_zip_url:
                    raise MineruError("MinerU 已完成，但未返回解析结果 ZIP 地址")
                persisted = self._persist_result(task, result.full_zip_url, client)
                now = _now()
                finished = self.repository.update_claimed_parse_task(
                    task_id,
                    str(task["lease_token"]),
                    {
                        "state": "done",
                        "dispatch_state": "succeeded",
                        "result_root_relpath": persisted["result_root_relpath"],
                        "output_sha256": persisted["output_sha256"],
                        "extracted_pages": result.extracted_pages,
                        "total_pages": result.total_pages,
                        "result_zip_url": None,
                        "error_code": None,
                        "error_message": None,
                        "updated_at": now,
                        "completed_at": now,
                    },
                    release=True,
                )
                if finished:
                    self._queue_normalization_if_complete(
                        str(task["document_id"]),
                        int(task.get("pipeline_generation", 1)),
                    )
                return
            started_at = task.get("started_at")
            if isinstance(started_at, str):
                elapsed = datetime.now(timezone.utc) - datetime.fromisoformat(
                    started_at
                )
                if elapsed.total_seconds() >= self.settings.mineru_poll_timeout_seconds:
                    raise MineruError("MinerU 解析任务轮询超时")
            state = result.state if result.state in _ACTIVE_STATES else "pending"
            self.repository.update_claimed_parse_task(
                task_id,
                str(task["lease_token"]),
                {
                    "state": state,
                    "dispatch_state": "waiting",
                    "next_poll_at": _after(self.settings.mineru_poll_interval_seconds),
                    "extracted_pages": result.extracted_pages,
                    "total_pages": result.total_pages,
                    "updated_at": _now(),
                },
                release=True,
            )
        except Exception as exc:
            self._mark_failed(task, exc)
        finally:
            client.close()
            self._clear_job(task_id)
            self._dispatcher_wake.set()

    def _persist_result(
        self, task: dict[str, Any], result_url: str, client: Any
    ) -> dict[str, str]:
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
        generation = int(task.get("pipeline_generation", 1))
        chunk_index = int(task.get("chunk_index", 0))
        try:
            claim_token = uuid.UUID(str(task["lease_token"])).hex
        except (KeyError, ValueError) as exc:
            raise MineruError("MinerU 结果缺少有效任务租约") from exc
        allocation_prefix = f"parse:{chunk_index:04d}:claim:{claim_token}"
        result_root_relpath = (
            f"staging/{document['id']}/g{generation}/parse/results/"
            f"{chunk_index:04d}/{claim_token}"
        )
        parsed_root = workspace / result_root_relpath
        parsed_root.mkdir(parents=True, exist_ok=True)
        result_zip_relpath = f"{result_root_relpath}/result.zip"
        zip_path = workspace / result_zip_relpath
        zip_allocation = f"{allocation_prefix}:zip"
        extracted_allocation = f"{allocation_prefix}:extracted"
        stop_lease_heartbeat = threading.Event()
        lease_lost = threading.Event()

        if not self._renew_parse_task_lease(task):
            raise MineruError("MinerU 解析任务租约已失效")
        lease_heartbeat = threading.Thread(
            target=self._maintain_parse_task_lease,
            args=(task, stop_lease_heartbeat, lease_lost),
            name=f"mineru-lease-{task['id']}",
            daemon=True,
        )
        lease_heartbeat.start()

        def ensure_lease() -> None:
            if lease_lost.is_set():
                raise MineruError("MinerU 解析任务租约已失效")

        def reserve(allocation_key: str, kind: str, size: int) -> bool:
            ensure_lease()
            return self.repository.reserve_claimed_parse_staging_bytes(
                task_id=str(task["id"]),
                lease_token=str(task["lease_token"]),
                allocation_key=allocation_key,
                kind=kind,
                bytes_reserved=size,
                maximum_bytes=self.settings.document_staging_max_bytes,
                now=_now(),
            )

        def cleanup_claim() -> None:
            shutil.rmtree(parsed_root, ignore_errors=True)
            for allocation_key in (zip_allocation, extracted_allocation):
                self.repository.release_staging_allocation(
                    str(document["id"]), generation, allocation_key, _now()
                )

        try:
            try:
                download_to = getattr(client, "download_result_to", None)
                if callable(download_to):
                    partial_zip = parsed_root / ".result.zip.part"
                    download_to(
                        result_url,
                        partial_zip,
                        max_bytes=self.settings.max_upload_bytes,
                        reserve_bytes=lambda size: reserve(
                            zip_allocation, "result_zip", size
                        ),
                    )
                    ensure_lease()
                    partial_zip.replace(zip_path)
                else:
                    zip_content = client.download_result(result_url)
                    if (
                        not zip_content
                        or len(zip_content) > self.settings.max_upload_bytes
                        or not reserve(zip_allocation, "result_zip", len(zip_content))
                    ):
                        raise MineruError("MinerU 解析结果超过大小限制或 staging 配额不足")
                    WorkspaceArtifactStore.atomic_write(zip_path, zip_content)
                files = extract_result_archive(
                    zip_path,
                    parsed_root,
                    max_member_bytes=self.settings.max_upload_bytes,
                    max_entries=self.settings.mineru_zip_max_entries,
                    max_compression_ratio=self.settings.mineru_zip_max_compression_ratio,
                    reserve_extracted_bytes=lambda size: reserve(
                        extracted_allocation, "extracted", size
                    ),
                )
                ensure_lease()
                extracted_size = sum(
                    path.stat().st_size for path in files if path.is_file()
                )
            except Exception:
                cleanup_claim()
                raise
            try:
                full_member = select_markdown_result(files, document, task)
                if full_member is None:
                    raise MineruError("MinerU 解析结果缺少 Markdown 主文件")
                content_list = next(
                    (path for path in files if path.name.lower() == "content_list.json"),
                    None,
                )
                duplicate_size = full_member.stat().st_size
                if content_list is not None:
                    duplicate_size += content_list.stat().st_size
                if not reserve(
                    extracted_allocation, "extracted", extracted_size + duplicate_size
                ):
                    raise MineruError("文档解压结果累计大小超过 staging 限制")
                full_relpath = f"{result_root_relpath}/full.md"
                full_content = full_member.read_bytes()
                ensure_lease()
                WorkspaceArtifactStore.atomic_write(workspace / full_relpath, full_content)
                now = _now()
                for kind, relpath, mime, size_bytes, sha256 in (
                    (
                        "parse_chunk_result",
                        result_zip_relpath,
                        "application/zip",
                        zip_path.stat().st_size,
                        _sha256_file(zip_path),
                    ),
                    (
                        "full_markdown",
                        full_relpath,
                        "text/markdown",
                        len(full_content),
                        _sha256(full_content),
                    ),
                ):
                    self.repository.put_document_artifact(
                        {
                            "document_id": document["id"],
                            "kind": kind,
                            "relpath": relpath,
                            "mime_type": mime,
                            "size_bytes": size_bytes,
                            "sha256": sha256,
                            "pipeline_generation": generation,
                            "created_at": now,
                        }
                    )
                if content_list is not None:
                    content = content_list.read_bytes()
                    relpath = f"{result_root_relpath}/content_list.json"
                    WorkspaceArtifactStore.atomic_write(workspace / relpath, content)
                    self.repository.put_document_artifact(
                        {
                            "document_id": document["id"],
                            "kind": "content_list",
                            "relpath": relpath,
                            "mime_type": "application/json",
                            "size_bytes": len(content),
                            "sha256": _sha256(content),
                            "pipeline_generation": generation,
                            "created_at": now,
                        }
                    )
                return {
                    "result_root_relpath": result_root_relpath,
                    "output_sha256": _sha256(full_content),
                }
            except Exception:
                cleanup_claim()
                raise
        finally:
            stop_lease_heartbeat.set()
            lease_heartbeat.join()

    def _queue_normalization_if_complete(
        self, document_id: str, generation: int
    ) -> None:
        tasks = self.repository.list_parse_tasks(document_id, generation)
        latest_by_chunk: dict[int, dict[str, Any]] = {}
        for item in tasks:
            latest_by_chunk.setdefault(int(item["chunk_index"]), item)
        if not latest_by_chunk:
            return
        expected_count = max(
            int(item["chunk_count"]) for item in latest_by_chunk.values()
        )
        if len(latest_by_chunk) != expected_count or any(
            item.get("state") != "done" and not item.get("output_sha256")
            for item in latest_by_chunk.values()
        ):
            return
        chunks = [
            NormalizationChunk(
                task_id=str(item["id"]),
                chunk_index=index,
                markdown_relpath=(
                    f"{item['result_root_relpath']}/full.md"
                    if item.get("result_root_relpath")
                    else ""
                ),
                output_sha256=str(item["output_sha256"]),
                source_page_start=item.get("source_page_start"),
                source_page_end=item.get("source_page_end"),
            )
            for index, item in sorted(latest_by_chunk.items())
        ]
        spec = NormalizationInput(
            document_id=document_id,
            pipeline_generation=generation,
            chunks=chunks,
        )
        self.repository.create_normalization_task(
            document_id=document_id,
            pipeline_generation=generation,
            input_manifest_json=spec.model_dump_json(),
            now=_now(),
        )
        self.pipeline.wake()

    def _build_workspace_document(
        self, path: Path, document: dict[str, Any]
    ) -> dict[str, Any]:
        knowledge_base = self.knowledge_bases.require_record(
            str(document["knowledge_base_id"])
        )
        tree_options = self.artifacts.read_tree_build_options(
            Path(str(knowledge_base["workspace_dir"]))
        )
        heading_recovery_mode = (
            "rules_then_llm"
            if document.get("parser") == "mineru"
            and str(document.get("file_extension", "")).lower() == ".pdf"
            and bool(knowledge_base.get("heading_recovery_enabled", True))
            else "off"
        )
        # A repaired PDF outline is a user-facing navigation result. Folding its
        # short sections back into parents would erase the recovered hierarchy.
        thin = tree_options.subtree_folding_enabled and heading_recovery_mode == "off"
        llm = None
        if knowledge_base["summary_enabled"]:
            llm = self.models.build_llm()
        if knowledge_base["summary_enabled"]:
            _, parsed = build_workspace_doc(
                str(path),
                llm=llm,
                thin=thin,
                min_node_token=tree_options.min_subtree_tokens,
                heading_recovery_mode=heading_recovery_mode,
                heading_recovery_llm=llm,
            )
        else:
            _, parsed = build_workspace_doc(
                str(path),
                no_summary=True,
                llm=llm,
                thin=thin,
                min_node_token=tree_options.min_subtree_tokens,
                heading_recovery_mode=heading_recovery_mode,
                heading_recovery_llm=llm,
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
                "file_extension": entity.file_extension,
                "mime_type": entity.mime_type,
                "parser": entity.parser,
                "pipeline_generation": entity.pipeline_generation,
                "parse_plan_json": entity.parse_plan_json,
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
        token = task.get("lease_token")
        if not isinstance(token, str) or not self.repository.fail_claimed_parse_task(
            str(task["id"]),
            token,
            error_code=str(code) if code else type(error).__name__,
            error_message=message,
            now=now,
        ):
            logger.warning(
                "document.parse_failure_fence_rejected task_id=%s", task["id"]
            )
            return
        self._fail_upload_for_document(str(task["document_id"]), message)

    def _fail_upload_for_document(self, document_id: str, message: str) -> None:
        document = self._find_document(document_id)
        if document is None:
            return
        try:
            upload_key = self._upload_key(
                str(document["knowledge_base_id"]), str(document["id"])
            )
        except KeyError:
            logger.warning(
                "document.upload_operation_missing document_id=%s", document_id
            )
            return
        self.repository.fail_upload(
            str(document["knowledge_base_id"]), upload_key, message, _now()
        )

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

    def get_document_pipeline(
        self, knowledge_base_id: str, document_id: str
    ) -> dict[str, Any]:
        document = self.repository.get_document(knowledge_base_id, document_id)
        if document is None:
            raise ApiError("文档不存在", status.HTTP_404_NOT_FOUND)
        generation = int(document.get("pipeline_generation", 1))
        parse_tasks = self.repository.list_parse_tasks(document_id, generation)
        normalization = self.repository.get_latest_normalization_task(
            document_id, generation
        )
        enrichment = self.repository.get_latest_enrichment_task(document_id, generation)
        hidden = {"lease_owner", "lease_token", "lease_expires_at", "request_json"}
        return {
            "document": self._payload(document),
            "parse_tasks": [
                {key: value for key, value in item.items() if key not in hidden}
                for item in parse_tasks
            ],
            "normalization_task": (
                {
                    key: value
                    for key, value in normalization.items()
                    if key not in hidden
                }
                if normalization is not None
                else None
            ),
            "enrichment_task": (
                {key: value for key, value in enrichment.items() if key not in hidden}
                if enrichment is not None
                else None
            ),
            "events": self.repository.list_pipeline_events(document_id, generation),
        }

    def retry_document(
        self, knowledge_base_id: str, document_id: str
    ) -> dict[str, Any]:
        """Retry a failed pipeline stage using the already stored source file."""
        self._materialize_legacy_markdown(knowledge_base_id)
        document = self.repository.get_document(knowledge_base_id, document_id)
        if document is None:
            raise ApiError("文档不存在", status.HTTP_404_NOT_FOUND)
        if document.get("status") != "failed":
            raise ApiError("只有处理失败的文档可以重试", status.HTTP_409_CONFLICT)

        failed_stage = str(document.get("failed_stage") or "parse")
        if failed_stage in {"normalize", "enrich"}:
            try:
                self.repository.retry_failed_pipeline_task(
                    document_id,
                    int(document.get("pipeline_generation", 1)),
                    failed_stage,
                    _now(),
                )
            except ValueError as exc:
                raise ApiError(str(exc), status.HTTP_409_CONFLICT) from exc
            self.pipeline.wake()
            refreshed = self.repository.get_document(knowledge_base_id, document_id)
            return self._payload(refreshed or document)
        if failed_stage != "parse" or document.get("parser") != "mineru":
            raise ApiError("该失败阶段暂不支持重试", status.HTTP_409_CONFLICT)

        record = self.knowledge_bases.require_record(knowledge_base_id)
        workspace = Path(str(record["workspace_dir"])).resolve()
        source = _resolve_workspace_path(workspace, str(document["source_relpath"]))
        if source is None or not source.is_file():
            raise ApiError("原始文件不存在，无法重试解析", status.HTTP_409_CONFLICT)

        parser_config = self.models.parser_runtime_config()
        now = _now()
        generation = int(document.get("pipeline_generation", 1))
        parse_tasks = self.repository.list_parse_tasks(document_id, generation)
        latest_by_chunk: dict[int, dict[str, Any]] = {}
        for task in parse_tasks:
            latest_by_chunk.setdefault(int(task["chunk_index"]), task)
        expected_chunks = max(
            (int(task["chunk_count"]) for task in latest_by_chunk.values()),
            default=0,
        )
        chunks_are_complete = expected_chunks > 0 and set(latest_by_chunk) == set(
            range(expected_chunks)
        )
        legacy_unpartitioned_pdf = (
            str(document.get("file_extension", "")).lower() == ".pdf"
            and expected_chunks == 1
            and len(latest_by_chunk) == 1
            and all(
                latest_by_chunk[0].get(field) is None
                for field in (
                    "source_page_start",
                    "source_page_end",
                    "chunk_source_relpath",
                )
            )
        )
        if not chunks_are_complete or legacy_unpartitioned_pdf:
            try:
                self.repository.restart_parse_preparation(document_id, generation, now)
            except ValueError as exc:
                raise ApiError(str(exc), status.HTTP_409_CONFLICT) from exc
            self._schedule_preparation(document_id, parser_config)
            refreshed = self.repository.get_document(knowledge_base_id, document_id)
            return self._payload(refreshed or document)

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
        if not item.get("parsed_markdown_relpath"):
            raise ApiError("文档尚未完成解析", status.HTTP_409_CONFLICT)
        try:
            return self._committed_workspace_view(knowledge_base_id).read_content(
                document_id
            )
        except KeyError as exc:
            raise ApiError("文档解析内容不存在", status.HTTP_404_NOT_FOUND) from exc
        except WorkspaceViewError as exc:
            raise ApiError(
                "文档解析内容不可读", status.HTTP_500_INTERNAL_SERVER_ERROR
            ) from exc

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
        try:
            document = self._committed_workspace_view(knowledge_base_id).load_document(
                document_id
            )
        except KeyError as exc:
            raise ApiError("文档索引树不存在", status.HTTP_404_NOT_FOUND) from exc
        except WorkspaceViewError as exc:
            raise ApiError(
                "文档索引树不可读", status.HTTP_500_INTERNAL_SERVER_ERROR
            ) from exc
        return {
            "doc_name": document.get("doc_name"),
            "doc_description": document.get("doc_description"),
            "line_count": document.get("line_count"),
            "structure": _strip_index_text(document.get("structure")),
        }

    def _committed_workspace_view(self, knowledge_base_id: str) -> WorkspaceView:
        record = self.knowledge_bases.require_record(knowledge_base_id)
        snapshot_relpath = record.get("snapshot_relpath")
        manifest_sha256 = record.get("snapshot_manifest_sha256")
        if not isinstance(snapshot_relpath, str) or not isinstance(
            manifest_sha256, str
        ):
            raise WorkspaceViewError("知识库当前没有 committed V2 snapshot")
        return WorkspaceView.revision(
            Path(str(record["workspace_dir"])),
            snapshot_relpath,
            manifest_sha256,
            knowledge_base_id=knowledge_base_id,
            content_version=int(record.get("content_version", 0)),
        )

    def delete_document(self, knowledge_base_id: str, document_id: str) -> None:
        """Publish a document tombstone and remove its active metadata.

        Index cleanup is surgical when the index was ready at delete time: the
        deleted document's records are removed from Milvus by ``doc_id`` and the
        index revision advanced, avoiding a full rebuild. If the surgical delete
        is skipped (index not ready) or fails (Milvus unavailable / collection
        missing), the index is left pending and a rebuild is scheduled as fallback
        (the rebuild reads the newly committed snapshot, which omits the document).
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
        # rebuild, which reads the snapshot excluding the deleted document.
        fts_was_ready = record.get("fts_status") == "ready"
        fts_collection = record.get("fts_collection")
        vector_was_ready = record.get("vector_status") == "ready"
        vector_collection = record.get("vector_collection")
        vector_model_id = record.get("vector_model_id")
        vector_model_updated_at = record.get("vector_model_updated_at")
        vector_dimension = record.get("vector_dimension")

        with workspace_lock(workspace):
            expected_revision = publish_document_deletion_snapshot(
                self.repository,
                workspace=workspace,
                knowledge_base_id=knowledge_base_id,
                document_id=document_id,
                fts_enabled=self.settings.fts_enabled,
                now=_now(),
            )

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
            knowledge_base_id,
            document_id,
            expected_revision,
            collection_name,
            _now(),
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
            document_id,
            expected_revision,
            collection_name,
            str(model_id or ""),
            str(model_updated_at or ""),
            int(dimension),
            _now(),
        )

    def _payload(self, item: dict[str, Any]) -> dict[str, Any]:
        parse_tasks = self.repository.list_parse_tasks(
            str(item["id"]), int(item.get("pipeline_generation", 1))
        )
        latest_by_chunk: dict[int, dict[str, Any]] = {}
        for parse_task in parse_tasks:
            latest_by_chunk.setdefault(int(parse_task["chunk_index"]), parse_task)
        task = next(iter(latest_by_chunk.values()), None)
        chunks_total = max(
            (int(parse_task["chunk_count"]) for parse_task in latest_by_chunk.values()),
            default=0,
        )
        chunks_completed = sum(
            parse_task.get("state") == "done" for parse_task in latest_by_chunk.values()
        )
        pages_total = sum(
            (
                int(parse_task["source_page_end"])
                - int(parse_task["source_page_start"])
                + 1
                if parse_task.get("source_page_start") is not None
                and parse_task.get("source_page_end") is not None
                else int(parse_task.get("total_pages") or 0)
            )
            for parse_task in latest_by_chunk.values()
        )
        pages_completed = sum(
            (
                int(parse_task["source_page_end"])
                - int(parse_task["source_page_start"])
                + 1
                if parse_task.get("source_page_start") is not None
                and parse_task.get("source_page_end") is not None
                else int(parse_task.get("extracted_pages") or 0)
            )
            if parse_task.get("state") == "done"
            else 0
            for parse_task in latest_by_chunk.values()
        )
        artifacts = self.repository.list_document_artifacts(str(item["id"]))
        payload = dict(item)
        payload.pop("parse_plan_json", None)
        try:
            warnings = json.loads(str(payload.pop("warning_json", "[]")))
        except (TypeError, ValueError):
            warnings = []
        payload["warnings"] = warnings if isinstance(warnings, list) else []
        payload["published_content_version"] = payload.get("parsed_content_version")
        payload["progress"] = {
            "completed": payload.get("progress_completed"),
            "total": payload.get("progress_total"),
            "unit": payload.get("progress_unit"),
        }
        payload["parse"] = {
            "chunks_completed": chunks_completed,
            "chunks_total": chunks_total,
            "pages_completed": pages_completed,
            "pages_total": pages_total,
        }
        return {
            **payload,
            "latest_task": (
                {
                    key: value
                    for key, value in task.items()
                    if key
                    not in {
                        "request_json",
                        "result_zip_url",
                        "lease_owner",
                        "lease_expires_at",
                        "lease_token",
                        "chunk_source_relpath",
                        "result_root_relpath",
                        "input_sha256",
                        "output_sha256",
                    }
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
        self.pipeline.recover()
        for document in self.repository.list_unqueued_markdown_normalization_documents():
            try:
                self._queue_markdown_normalization(document, _now())
            except Exception:
                logger.exception(
                    "document.markdown_normalization_recovery_failed document_id=%s",
                    document["id"],
                )
        for document in self.repository.list_parsing_documents():
            document_id = str(document["id"])
            self._schedule_existing_task(document_id)
        self._dispatcher_wake.set()

    def shutdown(self) -> None:
        self._dispatcher_stop.set()
        self._dispatcher_wake.set()
        self._dispatcher_thread.join(timeout=5)
        self.pipeline.shutdown()
        self._submission_executor.shutdown(wait=False, cancel_futures=True)
        self._poll_executor.shutdown(wait=False, cancel_futures=True)


__all__ = ["DocumentIngestionService", "MINERU_EXTENSIONS", "SUPPORTED_EXTENSIONS"]
