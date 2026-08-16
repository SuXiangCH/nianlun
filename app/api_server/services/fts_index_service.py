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
from nianlun.indexing.fts.store import NodeFtsStore


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _collection_name(base: str | None, knowledge_base_id: str) -> str:
    """Return a deterministic, per-knowledge-base Milvus collection name."""
    prefix = re.sub(r"[^a-zA-Z0-9_]", "_", base or "pageindex_node_fts").strip(
        "_"
    ) or "pageindex_node_fts"
    suffix = hashlib.sha256(knowledge_base_id.encode("utf-8")).hexdigest()[:16]
    return f"{prefix[:200]}_{suffix}"


logger = logging.getLogger(__name__)


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
        self._jobs: dict[str, Future[None]] = {}
        self._lock = threading.RLock()

    def schedule(self, knowledge_base_id: str, *, force: bool = False) -> dict[str, Any]:
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
        """Resume pending/building indexes after an API process restart."""
        if not self.settings.fts_enabled:
            return
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

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)

    def delete_collection(self, collection_name: str | None, knowledge_base_id: str) -> None:
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
        collection_exists = force or self._fts_collection_exists(collection_name)
        if force or not collection_exists:
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

    def _fts_collection_exists(self, collection_name: str) -> bool:
        """探测 collection 是否存在（force 路径不调用）。Milvus 不可用时抛出。"""
        store = NodeFtsStore(
            uri=self.settings.milvus_uri,
            token=self.settings.milvus_token,
            collection_name=collection_name,
        )
        return bool(store.client.has_collection(collection_name))


__all__ = ["FTSIndexService"]
