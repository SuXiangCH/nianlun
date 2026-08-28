"""Manage knowledge-base FTS indexes owned by the API Server."""

from __future__ import annotations

import hashlib
import logging
import re
import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import status

from app.api_server.common.errors import ApiError
from app.api_server.config import ApiServerSettings
from app.api_server.repositories import SQLiteMetadataRepository
from app.api_server.services.workspace_store import workspace_lock
from nianlun.indexing.fts.build import build_node_fts
from nianlun.indexing.fts.config import FTS_SCHEMA_CHECK_TIMEOUT_SECONDS
from nianlun.indexing.fts.store import NodeFtsStore


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _collection_name(base: str | None, knowledge_base_id: str) -> str:
    """Return a deterministic, per-knowledge-base Milvus collection name."""
    prefix = (
        re.sub(r"[^a-zA-Z0-9_]", "_", base or "pageindex_node_fts").strip("_")
        or "pageindex_node_fts"
    )
    suffix = hashlib.sha256(knowledge_base_id.encode("utf-8")).hexdigest()[:16]
    return f"{prefix[:200]}_{suffix}"


logger = logging.getLogger(__name__)

FTS_SCHEMA_RETRY_INITIAL_SECONDS = 1.0
FTS_SCHEMA_RETRY_MAX_SECONDS = 30.0


class FTSIndexService:
    """Build and publish one full-text index per knowledge base.

    The core FTS builder recreates its collection. A collection per knowledge
    base keeps that implementation isolated while SQLite revisions prevent a
    stale build from being advertised as ready after a concurrent upload.
    """

    def __init__(
        self,
        repository: SQLiteMetadataRepository,
        knowledge_base_lookup: Callable[[str], dict[str, Any]],
        settings: ApiServerSettings,
    ) -> None:
        self.repository = repository
        self.knowledge_base_lookup = knowledge_base_lookup
        self.settings = settings
        self._executor = ThreadPoolExecutor(
            max_workers=settings.fts_build_workers,
            thread_name_prefix="nianlun-fts",
        )
        self._schema_executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="nianlun-fts-schema",
        )
        self._jobs: dict[str, Future[None]] = {}
        self._schema_recovery: Future[None] | None = None
        self._shutdown_event = threading.Event()
        self._lock = threading.RLock()

    def schedule(
        self, knowledge_base_id: str, *, force: bool = False
    ) -> dict[str, Any]:
        """Queue an index build and return the latest persisted metadata."""
        self._ensure_enabled()
        item = self.knowledge_base_lookup(knowledge_base_id)
        revision = int(item.get("content_version", 0))
        collection_name = str(
            item.get("fts_collection")
            or _collection_name(self.settings.fts_collection, knowledge_base_id)
        )
        with self._lock:
            active = self._jobs.get(knowledge_base_id)
            if active is not None and not active.done():
                return self.knowledge_base_lookup(knowledge_base_id)
            fts_revision = item.get("fts_revision")
            if (
                not force
                and item.get("fts_status") == "ready"
                and fts_revision is not None
                and int(fts_revision) == revision
                and item.get("fts_collection")
            ):
                return item
            self.repository.mark_fts_pending(
                knowledge_base_id, revision, collection_name, _now()
            )
            self._jobs[knowledge_base_id] = self._executor.submit(
                self._build,
                knowledge_base_id,
                revision,
                collection_name,
                Path(str(item["workspace_dir"])),
                force=force,
            )
        return self.knowledge_base_lookup(knowledge_base_id)

    def recover_pending(self) -> None:
        """Resume builds and check ready collection schemas in the background."""
        if not self.settings.fts_enabled:
            return
        ready_knowledge_base_ids: list[str] = []
        for item in self.repository.list("knowledge_bases"):
            if item.get("fts_status") in {"pending", "building"}:
                try:
                    self.schedule(str(item["id"]))
                except Exception:
                    # A failed submission must not prevent the HTTP process from
                    # starting; the persisted state remains visible to operators.
                    self.repository.fail_fts_build(
                        str(item["id"]),
                        int(item.get("content_version", 0)),
                        "无法提交 FTS 构建任务",
                        _now(),
                    )
                continue
            if item.get("fts_status") == "ready" and item.get("fts_collection"):
                ready_knowledge_base_ids.append(str(item["id"]))

        if not ready_knowledge_base_ids:
            return
        with self._lock:
            if self._shutdown_event.is_set():
                return
            active = self._schema_recovery
            if active is not None and not active.done():
                return
            self._schema_recovery = self._schema_executor.submit(
                self._recover_ready_collection_schemas,
                tuple(ready_knowledge_base_ids),
            )

    def _recover_ready_collection_schemas(
        self, knowledge_base_ids: tuple[str, ...]
    ) -> None:
        """Rebuild obsolete schemas and retry transient probes until shutdown."""
        pending = list(knowledge_base_ids)
        retry_round = 0
        while pending and not self._shutdown_event.is_set():
            failed: list[str] = []
            for knowledge_base_id in pending:
                if self._shutdown_event.is_set():
                    return
                try:
                    item = self.knowledge_base_lookup(knowledge_base_id)
                    if item.get("fts_status") != "ready":
                        continue
                    collection_name = str(item.get("fts_collection") or "")
                    if not collection_name:
                        continue
                    is_current = self._fts_collection_is_current(collection_name)
                    if self._shutdown_event.is_set():
                        return
                    if not is_current:
                        self.schedule(knowledge_base_id, force=True)
                except ApiError as exc:
                    if exc.status_code == status.HTTP_404_NOT_FOUND:
                        continue
                    failed.append(knowledge_base_id)
                    logger.warning(
                        "knowledge_base.fts_schema_check_failed "
                        "knowledge_base_id=%s retry_round=%d",
                        knowledge_base_id,
                        retry_round,
                        exc_info=True,
                    )
                except Exception:
                    failed.append(knowledge_base_id)
                    logger.warning(
                        "knowledge_base.fts_schema_check_failed "
                        "knowledge_base_id=%s retry_round=%d",
                        knowledge_base_id,
                        retry_round,
                        exc_info=True,
                    )

            if not failed:
                return
            delay = min(
                FTS_SCHEMA_RETRY_MAX_SECONDS,
                FTS_SCHEMA_RETRY_INITIAL_SECONDS * (2 ** min(retry_round, 5)),
            )
            retry_round += 1
            logger.info(
                "knowledge_base.fts_schema_check_retry count=%d delay_seconds=%s",
                len(failed),
                delay,
            )
            if self._shutdown_event.wait(delay):
                return
            pending = failed

    def shutdown(self) -> None:
        self._shutdown_event.set()
        self._schema_executor.shutdown(wait=False, cancel_futures=True)
        self._executor.shutdown(wait=False, cancel_futures=True)

    def delete_collection(
        self, collection_name: str | None, knowledge_base_id: str
    ) -> None:
        """Stop a pending build and best-effort drop the KB's Milvus collection."""
        with self._lock:
            active = self._jobs.pop(knowledge_base_id, None)
            if active is not None:
                active.cancel()
        if not collection_name:
            return
        try:
            store = NodeFtsStore(
                uri=self.settings.milvus_uri,
                token=self.settings.milvus_token,
                collection_name=collection_name,
            )
            if store.client.has_collection(collection_name):
                store.client.drop_collection(collection_name)
        except Exception:
            # A local KB deletion must not be blocked by an unavailable optional
            # search backend. The SQLite reference is removed with the KB.
            logger.warning(
                "knowledge_base.fts_cleanup_failed collection=%s",
                collection_name,
                exc_info=True,
            )

    def _ensure_enabled(self) -> None:
        if not self.settings.fts_enabled:
            raise ApiError("API Server 未启用 FTS", status.HTTP_503_SERVICE_UNAVAILABLE)

    def _build(
        self,
        knowledge_base_id: str,
        revision: int,
        collection_name: str,
        workspace_dir: Path,
        *,
        force: bool = False,
    ) -> None:
        try:
            self.repository.mark_fts_building(
                knowledge_base_id, revision, collection_name, _now()
            )
            with workspace_lock(workspace_dir):
                self._build_fts_index(
                    knowledge_base_id, revision, collection_name, workspace_dir, force
                )
            ready = self.repository.finish_fts_build(
                knowledge_base_id, revision, collection_name, _now()
            )
        except Exception as exc:
            self.repository.fail_fts_build(
                knowledge_base_id, revision, f"{type(exc).__name__}: {exc}", _now()
            )
            ready = False
        finally:
            with self._lock:
                self._jobs.pop(knowledge_base_id, None)

        if not ready and self.settings.fts_enabled:
            try:
                latest = self.knowledge_base_lookup(knowledge_base_id)
            except ApiError as exc:
                if exc.status_code == 404:
                    return
                raise
            if int(latest.get("content_version", 0)) != revision:
                self.schedule(knowledge_base_id)

    def _build_fts_index(
        self,
        knowledge_base_id: str,
        revision: int,
        collection_name: str,
        workspace_dir: Path,
        force: bool,
    ) -> None:
        """驱动 FTS 增量/全量构建（设计文档 §5.3/§5.4）。

        - ``force`` 或 collection 缺失 -> 全量：``mark_all_fts_dirty`` -> ``create_collection``
          （drop+recreate 或新建）-> 处理全部脏文档 -> 置干净。
        - 否则 -> 增量：仅处理脏集（``fts_indexed_version IS NULL``），每文档先
          ``delete_by_doc`` 后 insert；脏集为空则跳过构建（仅 finish 推进 revision）。
        - 不变式（§6）：先落库 Milvus（insert+flush）、后置干净。
        """
        collection_is_current = force or self._fts_collection_is_current(
            collection_name
        )
        if force or not collection_is_current:
            self.repository.mark_all_fts_dirty(knowledge_base_id, _now())
            dirty_doc_ids = self.repository.list_fts_dirty_documents(knowledge_base_id)
            build_node_fts(
                workspace_dir,
                uri=self.settings.milvus_uri,
                token=self.settings.milvus_token,
                collection_name=collection_name,
                knowledge_base_id=knowledge_base_id,
                doc_ids=dirty_doc_ids,
                force=True,
            )
            self.repository.mark_documents_fts_indexed(
                knowledge_base_id, dirty_doc_ids, revision, _now()
            )
            return
        dirty_doc_ids = self.repository.list_fts_dirty_documents(knowledge_base_id)
        if not dirty_doc_ids:
            return
        build_node_fts(
            workspace_dir,
            uri=self.settings.milvus_uri,
            token=self.settings.milvus_token,
            collection_name=collection_name,
            knowledge_base_id=knowledge_base_id,
            doc_ids=dirty_doc_ids,
            force=False,
        )
        self.repository.mark_documents_fts_indexed(
            knowledge_base_id, dirty_doc_ids, revision, _now()
        )

    def _fts_collection_is_current(self, collection_name: str) -> bool:
        """Check collection existence and its current query metadata schema."""
        store = NodeFtsStore(
            uri=self.settings.milvus_uri,
            token=self.settings.milvus_token,
            collection_name=collection_name,
        )
        return store.has_current_schema(timeout=FTS_SCHEMA_CHECK_TIMEOUT_SECONDS)


__all__ = ["FTSIndexService"]
