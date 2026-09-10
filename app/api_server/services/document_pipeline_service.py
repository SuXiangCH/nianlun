"""Durable normalize/enrich workers for document ingestion."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from app.api_server.config import ApiServerSettings
from app.api_server.repositories import SQLiteMetadataRepository
from app.api_server.services.documents.pipeline_contracts import (
    EnrichmentOptions,
    NormalizationInput,
    PageMap,
    PageMapEntry,
)
from app.api_server.services.documents.normalization import (
    build_content_list_page_entries,
    normalize_chunk_resources,
)
from app.api_server.services.knowledge_base_service import KnowledgeBaseService
from app.api_server.services.model_config_service import ModelConfigService
from app.api_server.services.workspace_snapshot import (
    generation_enriched_dir,
    publish_document_snapshot,
    stage_generation_stage_artifacts,
)
from app.api_server.services.workspace_store import (
    WorkspaceArtifactStore,
    workspace_lock,
)
from nianlun.indexing.tree.pipeline import build_md_index
from nianlun.models.llm import content_to_text


logger = logging.getLogger(__name__)
PROMPT_SCHEMA_VERSION = 1
_MAX_LEASE_MAINTENANCE_INTERVAL_SECONDS = 30.0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _lease_expiry(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _node_count(nodes: list[dict[str, Any]]) -> int:
    return sum(1 + _node_count(list(node.get("nodes") or [])) for node in nodes)


def _load_enriched_generation_artifacts(
    workspace: Path, document_id: str, generation: int
) -> dict[str, bytes] | None:
    """Read a complete immutable enrich result left by a failed publish."""
    root = workspace / generation_enriched_dir(document_id, generation)
    if not root.exists():
        return None
    files: dict[str, bytes] = {}
    for name in ("full.md", "tree.json", "diagnostics.json"):
        path = root / name
        if not path.is_file():
            raise RuntimeError(f"enrich 已落盘产物不完整: {path}")
        files[name] = path.read_bytes()
    return files


def _parse_enrichment_diagnostics(payload: bytes) -> dict[str, int]:
    try:
        raw = json.loads(payload)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("enrich diagnostics 无法解析") from exc
    if not isinstance(raw, dict):
        raise RuntimeError("enrich diagnostics 格式无效")
    diagnostics: dict[str, int] = {}
    for name in (
        "nodes_total",
        "model_requests",
        "model_requests_succeeded",
        "model_requests_failed",
    ):
        value = raw.get(name)
        if not isinstance(value, int) or value < 0:
            raise RuntimeError(f"enrich diagnostics 字段无效: {name}")
        diagnostics[name] = value
    node_failures = raw.get("model_node_requests_failed", 0)
    if not isinstance(node_failures, int) or node_failures < 0:
        raise RuntimeError("enrich diagnostics 字段无效: model_node_requests_failed")
    diagnostics["model_node_requests_failed"] = node_failures
    return diagnostics


class _LimitedCachedLLM:
    """Apply one-loop global/per-document limits and cache successful calls."""

    def __init__(
        self,
        delegate: Any,
        repository: SQLiteMetadataRepository,
        *,
        document_id: str,
        pipeline_generation: int,
        model_fingerprint: str,
        global_semaphore: asyncio.Semaphore,
        per_document_limit: int,
        request_timeout_seconds: float,
    ) -> None:
        self.delegate = delegate
        self.repository = repository
        self.document_id = document_id
        self.pipeline_generation = pipeline_generation
        self.model_fingerprint = model_fingerprint
        self.global_semaphore = global_semaphore
        self.document_semaphore = asyncio.Semaphore(per_document_limit)
        self.request_timeout_seconds = request_timeout_seconds
        self.requested = 0
        self.succeeded = 0
        self.failed = 0
        self.failed_by_purpose: dict[str, int] = {}

    @staticmethod
    def _purpose(prompt: str) -> str:
        if prompt.startswith("You assign hierarchical levels"):
            return "heading_recovery"
        if "Partial Document Text:" in prompt:
            return "node_summary"
        return "doc_description"

    async def ainvoke(self, prompt: Any, **kwargs: Any) -> Any:
        prompt_text = str(prompt)
        digest = _sha256(prompt_text.encode("utf-8"))
        purpose = self._purpose(prompt_text)
        cached = await asyncio.to_thread(
            self.repository.get_enrichment_call_result,
            document_id=self.document_id,
            pipeline_generation=self.pipeline_generation,
            purpose=purpose,
            unit_key=digest,
            input_sha256=digest,
            model_fingerprint=self.model_fingerprint,
            prompt_schema_version=PROMPT_SCHEMA_VERSION,
        )
        if cached is not None and cached["state"] == "succeeded":
            self.succeeded += 1
            return str(cached.get("output_text") or "")
        self.requested += 1
        try:
            async with self.document_semaphore:
                async with self.global_semaphore:
                    try:
                        async with asyncio.timeout(self.request_timeout_seconds):
                            response = await self.delegate.ainvoke(prompt, **kwargs)
                    except TimeoutError as exc:
                        raise TimeoutError(
                            f"LLM 请求超过 {self.request_timeout_seconds:g} 秒，已终止"
                        ) from exc
            output = content_to_text(response)
            await asyncio.to_thread(
                self.repository.put_enrichment_call_result,
                document_id=self.document_id,
                pipeline_generation=self.pipeline_generation,
                purpose=purpose,
                unit_key=digest,
                input_sha256=digest,
                model_fingerprint=self.model_fingerprint,
                prompt_schema_version=PROMPT_SCHEMA_VERSION,
                state="succeeded",
                output_text=output,
                error_code=None,
                now=_now(),
            )
            self.succeeded += 1
            return response
        except Exception as exc:
            self.failed += 1
            self.failed_by_purpose[purpose] = self.failed_by_purpose.get(purpose, 0) + 1
            await asyncio.to_thread(
                self.repository.put_enrichment_call_result,
                document_id=self.document_id,
                pipeline_generation=self.pipeline_generation,
                purpose=purpose,
                unit_key=digest,
                input_sha256=digest,
                model_fingerprint=self.model_fingerprint,
                prompt_schema_version=PROMPT_SCHEMA_VERSION,
                state="failed",
                output_text=None,
                error_code=type(exc).__name__,
                now=_now(),
            )
            raise


class DocumentPipelineService:
    """Run persisted normalize/enrich work on one dedicated asyncio loop."""

    def __init__(
        self,
        repository: SQLiteMetadataRepository,
        knowledge_bases: KnowledgeBaseService,
        models: ModelConfigService,
        settings: ApiServerSettings,
        *,
        fts_schedule: Callable[..., dict[str, Any]] | None = None,
        vector_schedule: Callable[..., dict[str, Any]] | None = None,
    ) -> None:
        self.repository = repository
        self.knowledge_bases = knowledge_bases
        self.models = models
        self.settings = settings
        self.fts_schedule = fts_schedule
        self.vector_schedule = vector_schedule
        self.owner = f"pipeline-{uuid.uuid4()}"
        self._loop: asyncio.AbstractEventLoop | None = None
        self._wake_event: asyncio.Event | None = None
        self._ready = threading.Event()
        self._thread = threading.Thread(
            target=self._thread_main,
            name="nianlun-document-pipeline",
            daemon=True,
        )
        self._thread.start()
        self._ready.wait(timeout=5)

    def _thread_main(self) -> None:
        asyncio.run(self._run())

    async def _run(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._wake_event = asyncio.Event()
        self._stop_event = asyncio.Event()
        self._global_llm_semaphore = asyncio.Semaphore(self.settings.llm_max_concurrent)
        self._ready.set()
        workers = [
            asyncio.create_task(self._worker(index))
            for index in range(self.settings.enrich_workers)
        ]
        self._wake_event.set()
        await self._stop_event.wait()
        for worker in workers:
            worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)

    def wake(self) -> None:
        if self._loop is not None and self._wake_event is not None:
            self._loop.call_soon_threadsafe(self._wake_event.set)

    def recover(self) -> None:
        """Recreate enrich work lost after normalization committed successfully."""
        for document in self.repository.list_unqueued_enrichment_documents():
            document_id = str(document["id"])
            generation = int(document["pipeline_generation"])
            latest_enrichment = self.repository.get_latest_enrichment_task(
                document_id, generation
            )
            if latest_enrichment is not None:
                continue
            latest_normalization = self.repository.get_latest_normalization_task(
                document_id, generation
            )
            if (
                latest_normalization is None
                or latest_normalization.get("state") != "succeeded"
                or not latest_normalization.get("output_sha256")
            ):
                logger.error(
                    "document.pipeline_recovery_invalid_enrich_handoff document_id=%s generation=%s",
                    document_id,
                    generation,
                )
                continue
            try:
                self._queue_enrichment(
                    document_id,
                    generation,
                    str(latest_normalization["output_sha256"]),
                )
            except Exception:
                logger.exception(
                    "document.pipeline_recovery_enqueue_failed document_id=%s generation=%s",
                    document_id,
                    generation,
                )
        self.wake()

    async def _worker(self, index: int) -> None:
        assert self._wake_event is not None
        prefer_normalize = index % 2 == 0
        while True:
            try:
                await asyncio.wait_for(
                    self._wake_event.wait(),
                    timeout=self._lease_maintenance_interval(),
                )
            except TimeoutError:
                pass
            self._wake_event.clear()
            while True:
                stages = (
                    ("normalize", "enrich")
                    if prefer_normalize
                    else (
                        "enrich",
                        "normalize",
                    )
                )
                task = None
                selected_stage = stages[0]
                for stage in stages:
                    claim = (
                        self.repository.claim_normalization_task
                        if stage == "normalize"
                        else self.repository.claim_enrichment_task
                    )
                    task = await asyncio.to_thread(
                        claim,
                        owner=f"{self.owner}:{stage}:{index}",
                        now=_now(),
                        lease_expires_at=_lease_expiry(
                            self.settings.pipeline_lease_seconds
                        ),
                    )
                    if task is not None:
                        selected_stage = stage
                        break
                if task is None:
                    break
                await self._run_claimed_task(selected_stage, task)
                prefer_normalize = selected_stage != "normalize"

    def _lease_maintenance_interval(self) -> float:
        return max(
            0.1,
            min(
                _MAX_LEASE_MAINTENANCE_INTERVAL_SECONDS,
                self.settings.pipeline_lease_seconds / 3,
            ),
        )

    async def _run_claimed_task(self, stage: str, task: dict[str, Any]) -> None:
        stop = asyncio.Event()
        heartbeat = asyncio.create_task(
            self._maintain_task_lease(
                stage,
                str(task["id"]),
                str(task["lease_token"]),
                stop,
            )
        )
        try:
            if stage == "normalize":
                await self._normalize(task)
            else:
                await self._enrich(task)
        finally:
            stop.set()
            await heartbeat

    async def _maintain_task_lease(
        self,
        stage: str,
        task_id: str,
        lease_token: str,
        stop: asyncio.Event,
    ) -> None:
        interval = self._lease_maintenance_interval()
        while True:
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
                return
            except TimeoutError:
                pass
            try:
                renewed = await asyncio.to_thread(
                    self.repository.renew_pipeline_task_lease,
                    stage,
                    task_id,
                    lease_token,
                    lease_expires_at=_lease_expiry(
                        self.settings.pipeline_lease_seconds
                    ),
                    now=_now(),
                )
            except Exception:
                logger.exception(
                    "document.pipeline_lease_heartbeat_failed stage=%s task_id=%s",
                    stage,
                    task_id,
                )
                continue
            if not renewed:
                logger.warning(
                    "document.pipeline_lease_lost stage=%s task_id=%s",
                    stage,
                    task_id,
                )
                return

    async def _normalize(self, task: dict[str, Any]) -> None:
        token = str(task["lease_token"])
        allocation_key = f"normalize:{task['id']}"
        try:
            result = await asyncio.to_thread(self._normalize_sync, task)
            finished = await asyncio.to_thread(
                self.repository.finish_normalization_task,
                str(task["id"]),
                token,
                normalized_markdown_relpath=result["markdown_relpath"],
                page_map_relpath=result["page_map_relpath"],
                output_sha256=result["output_sha256"],
                now=_now(),
            )
            if not finished:
                await asyncio.to_thread(
                    self.repository.release_staging_allocation,
                    str(task["document_id"]),
                    int(task["pipeline_generation"]),
                    allocation_key,
                    _now(),
                )
                return
            await asyncio.to_thread(
                self._queue_enrichment,
                str(task["document_id"]),
                int(task["pipeline_generation"]),
                str(result["output_sha256"]),
            )
        except Exception as exc:
            logger.exception("document.normalize_failed task_id=%s", task["id"])
            await asyncio.to_thread(
                self.repository.release_staging_allocation,
                str(task["document_id"]),
                int(task["pipeline_generation"]),
                allocation_key,
                _now(),
            )
            await asyncio.to_thread(
                self.repository.fail_pipeline_task,
                "normalize",
                str(task["id"]),
                token,
                error_code=type(exc).__name__,
                error_message=str(exc)[:2000],
                now=_now(),
            )

    def _normalize_sync(self, task: dict[str, Any]) -> dict[str, str]:
        spec = NormalizationInput.model_validate_json(task["input_manifest_json"])
        document = self._document(spec.document_id)
        if int(document["pipeline_generation"]) != spec.pipeline_generation:
            raise RuntimeError("文档 generation 已变化")
        kb = self.knowledge_bases.require_record(str(document["knowledge_base_id"]))
        workspace = Path(str(kb["workspace_dir"])).resolve()
        parts: list[str] = []
        normalized_assets: dict[str, bytes] = {}
        entries: list[PageMapEntry] = []
        exact_entries: list[PageMapEntry] = []
        exact_page_map = True
        next_line = 1
        self._update_progress_sync("normalize", task, 0, len(spec.chunks))
        for completed, chunk in enumerate(spec.chunks, start=1):
            path = (workspace / chunk.markdown_relpath).resolve()
            path.relative_to(workspace)
            raw = path.read_bytes()
            if _sha256(raw) != chunk.output_sha256:
                raise RuntimeError(f"normalize 输入 hash 不匹配: {chunk.chunk_index}")
            text = raw.decode("utf-8").rstrip("\n")
            chunk_exact_entries = build_content_list_page_entries(
                text,
                path.with_name("content_list.json"),
                chunk_index=chunk.chunk_index,
                source_page_start=chunk.source_page_start,
                source_page_end=chunk.source_page_end,
                markdown_line_offset=next_line,
            )
            if chunk_exact_entries is None:
                exact_page_map = False
            else:
                exact_entries.extend(chunk_exact_entries)
            if document["parser"] == "mineru":
                normalized = normalize_chunk_resources(
                    text, path.parent, chunk.chunk_index
                )
                text = normalized.markdown.rstrip("\n")
                normalized_assets.update(normalized.assets)
            line_count = text.count("\n") + 1
            parts.append(text)
            entries.append(
                PageMapEntry(
                    markdown_line_start=next_line,
                    markdown_line_end=next_line + line_count - 1,
                    chunk_index=chunk.chunk_index,
                    source_page_start=chunk.source_page_start,
                    source_page_end=chunk.source_page_end,
                    evidence="chunk_boundary",
                )
            )
            next_line += line_count + 1
            self._update_progress_sync("normalize", task, completed, len(spec.chunks))
        content = "\n\n".join(parts).rstrip().encode("utf-8")
        if exact_page_map:
            page_map = PageMap(precision="exact", entries=exact_entries)
        elif all(item.source_page_start is not None for item in spec.chunks):
            page_map = PageMap(precision="chunk", entries=entries)
        else:
            page_map = PageMap(precision="unavailable", entries=entries)
        output_files = {
            "full.md": content,
            "page_map.json": page_map.model_dump_json(indent=2).encode("utf-8"),
            **normalized_assets,
        }
        if not self.repository.reserve_staging_bytes(
            document_id=spec.document_id,
            pipeline_generation=spec.pipeline_generation,
            allocation_key=f"normalize:{task['id']}",
            kind="temp_output",
            bytes_reserved=sum(len(value) for value in output_files.values()),
            maximum_bytes=self.settings.document_staging_max_bytes,
            now=_now(),
        ):
            raise RuntimeError("normalize 产物超过 staging 配额")
        paths = stage_generation_stage_artifacts(
            workspace,
            spec.document_id,
            spec.pipeline_generation,
            "normalized",
            output_files,
        )
        now = _now()
        for kind, name, mime in (
            ("normalized_markdown", "full.md", "text/markdown"),
            ("page_map", "page_map.json", "application/json"),
        ):
            artifact = (workspace / paths[name]).read_bytes()
            self.repository.put_document_artifact(
                {
                    "document_id": spec.document_id,
                    "kind": kind,
                    "relpath": paths[name],
                    "mime_type": mime,
                    "size_bytes": len(artifact),
                    "sha256": _sha256(artifact),
                    "pipeline_generation": spec.pipeline_generation,
                    "created_at": now,
                }
            )
        for name in sorted(normalized_assets):
            artifact = (workspace / paths[name]).read_bytes()
            self.repository.put_document_artifact(
                {
                    "document_id": spec.document_id,
                    "kind": "asset",
                    "relpath": paths[name],
                    "mime_type": "application/octet-stream",
                    "size_bytes": len(artifact),
                    "sha256": _sha256(artifact),
                    "pipeline_generation": spec.pipeline_generation,
                    "created_at": now,
                }
            )
        self.repository.append_pipeline_event(
            document_id=spec.document_id,
            pipeline_generation=spec.pipeline_generation,
            stage="normalize",
            event_type="succeeded",
            now=now,
            progress_completed=len(spec.chunks),
            progress_total=len(spec.chunks),
        )
        return {
            "markdown_relpath": paths["full.md"],
            "page_map_relpath": paths["page_map.json"],
            "output_sha256": _sha256(content),
        }

    def _queue_enrichment(
        self, document_id: str, generation: int, input_sha256: str
    ) -> None:
        document = self._document(document_id)
        kb = self.knowledge_bases.require_record(str(document["knowledge_base_id"]))
        options = WorkspaceArtifactStore.read_tree_build_options(
            Path(str(kb["workspace_dir"]))
        )
        profile = self.repository.get_default_model_profile("llm")
        payload = EnrichmentOptions(
            summary_enabled=bool(kb["summary_enabled"]),
            heading_recovery_enabled=bool(kb.get("heading_recovery_enabled", True)),
            subtree_folding_enabled=options.subtree_folding_enabled,
            min_subtree_tokens=options.min_subtree_tokens or 1,
        )
        self.repository.create_enrichment_task(
            document_id=document_id,
            pipeline_generation=generation,
            input_sha256=input_sha256,
            llm_profile_id=str(profile["id"]) if profile is not None else None,
            llm_profile_updated_at=(
                str(profile["updated_at"]) if profile is not None else None
            ),
            prompt_schema_version=PROMPT_SCHEMA_VERSION,
            options_json=payload.model_dump_json(),
            now=_now(),
        )
        self.wake()

    async def _enrich(self, task: dict[str, Any]) -> None:
        token = str(task["lease_token"])
        allocation_key = f"enrich:{task['id']}"
        try:
            document = await asyncio.to_thread(self._document, str(task["document_id"]))
            generation = int(task["pipeline_generation"])
            kb = await asyncio.to_thread(
                self.knowledge_bases.require_record,
                str(document["knowledge_base_id"]),
            )
            workspace = Path(str(kb["workspace_dir"])).resolve()
            normalized_relpath = (
                f"artifacts/{document['id']}/g{generation}/normalized/full.md"
            )
            normalized_path = (workspace / normalized_relpath).resolve()
            normalized_path.relative_to(workspace)
            raw = await asyncio.to_thread(normalized_path.read_bytes)
            if _sha256(raw) != task["input_sha256"]:
                raise RuntimeError("enrich 输入 hash 不匹配")
            enriched_files = await asyncio.to_thread(
                _load_enriched_generation_artifacts,
                workspace,
                str(document["id"]),
                generation,
            )
            if enriched_files is not None:
                if enriched_files["full.md"] != raw:
                    raise RuntimeError("enrich 已落盘产物与 normalize 输入不一致")
                tree_bytes = enriched_files["tree.json"]
                diagnostics = _parse_enrichment_diagnostics(
                    enriched_files["diagnostics.json"]
                )
                base = generation_enriched_dir(str(document["id"]), generation)
                staged = {name: str(base / name) for name in enriched_files}
            else:
                options = EnrichmentOptions.model_validate_json(task["options_json"])
                reported_completed = -1
                reported_total = -1

                async def report_node_progress(completed: int, total: int) -> None:
                    nonlocal reported_completed, reported_total
                    step = max(1, total // 100)
                    if (
                        total == reported_total
                        and completed not in {0, total}
                        and completed - reported_completed < step
                    ):
                        return
                    updated = await asyncio.to_thread(
                        self.repository.update_pipeline_task_progress,
                        "enrich",
                        str(task["id"]),
                        token,
                        progress_completed=completed,
                        progress_total=total,
                        lease_expires_at=_lease_expiry(
                            self.settings.pipeline_lease_seconds
                        ),
                        now=_now(),
                    )
                    if not updated:
                        raise RuntimeError("enrich 任务租约已失效")
                    reported_completed = completed
                    reported_total = total

                limited_llm: _LimitedCachedLLM | None = None
                llm = None
                if options.summary_enabled:
                    delegate = await asyncio.to_thread(self.models.build_llm)
                    fingerprint = _sha256(
                        (
                            f"{task.get('llm_profile_id') or ''}:"
                            f"{task.get('llm_profile_updated_at') or ''}"
                        ).encode("utf-8")
                    )
                    limited_llm = _LimitedCachedLLM(
                        delegate,
                        self.repository,
                        document_id=str(document["id"]),
                        pipeline_generation=generation,
                        model_fingerprint=fingerprint,
                        global_semaphore=self._global_llm_semaphore,
                        per_document_limit=self.settings.llm_per_document_concurrent,
                        request_timeout_seconds=(
                            self.settings.llm_request_timeout_seconds
                        ),
                    )
                    llm = limited_llm
                heading_mode = (
                    "rules_then_llm"
                    if document["parser"] == "mineru"
                    and document["file_extension"] == ".pdf"
                    and options.heading_recovery_enabled
                    else "off"
                )
                result = await build_md_index(
                    str(normalized_path),
                    llm=llm,
                    add_node_summary=options.summary_enabled,
                    add_doc_description=options.summary_enabled,
                    add_node_text=True,
                    add_node_id=True,
                    thin=options.subtree_folding_enabled and heading_mode == "off",
                    min_node_token=options.min_subtree_tokens,
                    heading_recovery_mode=heading_mode,
                    heading_recovery_llm=llm,
                    node_progress_callback=report_node_progress,
                )
                tree: dict[str, Any] = {
                    "id": str(document["id"]),
                    "type": "md",
                    "path": normalized_relpath,
                    "doc_name": str(document["original_filename"]),
                    "doc_description": result.get("doc_description") or "",
                    "line_count": result["line_count"],
                    "structure": result["structure"],
                }
                if "heading_recovery" in result:
                    tree["heading_recovery"] = result["heading_recovery"]
                tree_bytes = json.dumps(tree, ensure_ascii=False, indent=2).encode(
                    "utf-8"
                )
                diagnostics = {
                    "nodes_total": _node_count(tree["structure"]),
                    "model_requests": limited_llm.requested if limited_llm else 0,
                    "model_requests_succeeded": (
                        limited_llm.succeeded if limited_llm else 0
                    ),
                    "model_requests_failed": limited_llm.failed if limited_llm else 0,
                    "model_node_requests_failed": (
                        limited_llm.failed_by_purpose.get("node_summary", 0)
                        if limited_llm
                        else 0
                    ),
                }
                enriched_files = {
                    "full.md": raw,
                    "tree.json": tree_bytes,
                    "diagnostics.json": json.dumps(
                        diagnostics, ensure_ascii=False, indent=2
                    ).encode("utf-8"),
                }
                reserved = await asyncio.to_thread(
                    self.repository.reserve_staging_bytes,
                    document_id=str(document["id"]),
                    pipeline_generation=generation,
                    allocation_key=allocation_key,
                    kind="temp_output",
                    bytes_reserved=sum(len(value) for value in enriched_files.values()),
                    maximum_bytes=self.settings.document_staging_max_bytes,
                    now=_now(),
                )
                if not reserved:
                    raise RuntimeError("enrich 产物超过 staging 配额")
                staged = await asyncio.to_thread(
                    stage_generation_stage_artifacts,
                    workspace,
                    str(document["id"]),
                    generation,
                    "enriched",
                    enriched_files,
                )
            warnings: list[dict[str, Any]] = []
            failed_calls = diagnostics["model_requests_failed"]
            failed_nodes = diagnostics["model_node_requests_failed"]
            if failed_calls:
                warnings.append(
                    {"warning_code": "LLM_PARTIAL_FAILURE", "count": failed_calls}
                )
            state = "partial" if warnings else "succeeded"
            with workspace_lock(workspace):
                revision = publish_document_snapshot(
                    self.repository,
                    workspace=workspace,
                    knowledge_base_id=str(document["knowledge_base_id"]),
                    document_id=str(document["id"]),
                    generation=generation,
                    content=raw,
                    source_relpath=str(document["source_relpath"]),
                    source_mime_type=str(document["mime_type"]),
                    idempotency_key=self._upload_key(document),
                    fts_enabled=self.settings.fts_enabled,
                    now=_now(),
                    page_map_relpath=(
                        f"artifacts/{document['id']}/g{generation}/normalized/"
                        "page_map.json"
                    ),
                    tree_content=tree_bytes,
                    enrichment_completion={
                        "task_id": str(task["id"]),
                        "lease_token": token,
                        "state": state,
                        "node_count": diagnostics["nodes_total"],
                        "nodes_completed": diagnostics["nodes_total"] - failed_nodes,
                        "nodes_failed": failed_nodes,
                        "output_markdown_relpath": staged["full.md"],
                        "output_tree_relpath": staged["tree.json"],
                        "output_sha256": _sha256(tree_bytes + raw),
                        "warning_json": json.dumps(warnings, ensure_ascii=False),
                    },
                )
            await asyncio.to_thread(
                self.repository.append_pipeline_event,
                document_id=str(document["id"]),
                pipeline_generation=generation,
                stage="publish",
                event_type="succeeded",
                now=_now(),
                message=f"content revision {revision}",
            )
            self._schedule_indexes(str(document["knowledge_base_id"]))
        except Exception as exc:
            logger.exception("document.enrich_failed task_id=%s", task["id"])
            await asyncio.to_thread(
                self.repository.release_staging_allocation,
                str(task["document_id"]),
                int(task["pipeline_generation"]),
                allocation_key,
                _now(),
            )
            await asyncio.to_thread(
                self.repository.fail_pipeline_task,
                "enrich",
                str(task["id"]),
                token,
                error_code=type(exc).__name__,
                error_message=str(exc)[:2000],
                now=_now(),
            )

    def _update_progress_sync(
        self,
        stage: str,
        task: dict[str, Any],
        completed: int,
        total: int,
    ) -> None:
        updated = self.repository.update_pipeline_task_progress(
            stage,
            str(task["id"]),
            str(task["lease_token"]),
            progress_completed=completed,
            progress_total=total,
            lease_expires_at=_lease_expiry(self.settings.pipeline_lease_seconds),
            now=_now(),
        )
        if not updated:
            raise RuntimeError(f"{stage} 任务租约已失效")

    def _schedule_indexes(self, knowledge_base_id: str) -> None:
        if self.fts_schedule is not None and self.settings.fts_enabled:
            try:
                self.fts_schedule(knowledge_base_id)
            except Exception:
                logger.exception("document.fts_schedule_failed")
        if self.vector_schedule is not None:
            try:
                self.vector_schedule(knowledge_base_id)
            except Exception:
                logger.exception("document.vector_schedule_failed")

    def _document(self, document_id: str) -> dict[str, Any]:
        for kb in self.repository.list("knowledge_bases"):
            item = self.repository.get_document(str(kb["id"]), document_id)
            if item is not None:
                return item
        raise KeyError(document_id)

    def _upload_key(self, document: dict[str, Any]) -> str | None:
        operation = self.repository.get_upload_by_document(
            str(document["knowledge_base_id"]), str(document["id"])
        )
        return str(operation["idempotency_key"]) if operation is not None else None

    def shutdown(self) -> None:
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._stop_event.set)
        self._thread.join(timeout=5)


__all__ = ["DocumentPipelineService", "PROMPT_SCHEMA_VERSION"]
