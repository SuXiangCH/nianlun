"""Manage knowledge-base vector indexes owned by the API Server."""

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

from app.api_server.common.errors import ApiError
from app.api_server.config import ApiServerSettings
from app.api_server.repositories import SQLiteMetadataRepository
from app.api_server.services.workspace_store import workspace_lock
from nianlun.indexing.vector.build import build_doc_vectors
from nianlun.indexing.vector.store import DocVectorStore


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _collection_name(base: str | None, knowledge_base_id: str) -> str:
    """Return a deterministic vector collection name per knowledge base."""
    prefix = (
        re.sub(r"[^a-zA-Z0-9_]", "_", base or "pageindex_doc_vectors").strip("_")
        or "pageindex_doc_vectors"
    )
    suffix = hashlib.sha256(knowledge_base_id.encode("utf-8")).hexdigest()[:16]
    return f"{prefix[:200]}_{suffix}"


logger = logging.getLogger(__name__)


class VectorIndexService:
    """Build and publish one isolated dense-vector index per knowledge base."""

    def __init__(
        self,
        repository: SQLiteMetadataRepository,
        knowledge_base_lookup: Callable[[str], dict[str, Any]],
        embedding_config_lookup: Callable[[str], dict[str, Any]],
        settings: ApiServerSettings,
    ) -> None:
        self.repository = repository
        self.knowledge_base_lookup = knowledge_base_lookup
        self.embedding_config_lookup = embedding_config_lookup
        self.settings = settings
        self._executor = ThreadPoolExecutor(
            max_workers=settings.vector_build_workers,
            thread_name_prefix="nianlun-vector",
        )
        self._jobs: dict[str, Future[None]] = {}
        self._lock = threading.RLock()

    def schedule(
        self,
        knowledge_base_id: str,
        *,
        force: bool = False,
        activate: bool = False,
    ) -> dict[str, Any]:
        """Queue a vector build and return the latest persisted metadata."""
        item = self.knowledge_base_lookup(knowledge_base_id)
        if item.get("vector_status") == "disabled" and not activate:
            return item
        revision = int(item.get("content_version", 0))
        model_selection = str(item.get("embedding_model_id") or "").strip()
        if not model_selection:
            self.repository.disable_vector(knowledge_base_id, _now())
            return self.knowledge_base_lookup(knowledge_base_id)
        collection_name = str(
            item.get("vector_collection")
            or _collection_name(self.settings.vector_collection, knowledge_base_id)
        )
        try:
            config = self.embedding_config_lookup(model_selection)
            model_id = model_selection
            model_updated_at = str(config["profile_updated_at"])
            dimension = int(config["dimension"])
            if not config.get("model"):
                raise ValueError("Embedding 模型名称未配置")
            if not config.get("base_url"):
                raise ValueError("Embedding API URL 未配置")
            if not config.get("api_key"):
                raise ValueError("Embedding API Key 未配置")
        except Exception as exc:
            self.repository.fail_vector_configuration(
                knowledge_base_id, f"{type(exc).__name__}: {exc}", _now()
            )
            return self.knowledge_base_lookup(knowledge_base_id)
        # 是否需要全量蓝绿：force、首次构建（无现存 collection 可增量）、或 embedding
        # 模型指纹变更（维度/模型变了，旧 collection 的向量不可复用，必须 staging+publish
        # 替换）。设计文档 §5.4/§5.5/§5.6：首次/force/模型变更走全量蓝绿；同模型增量上传
        # 走线上 collection 增量。
        never_built = item.get("vector_revision") is None
        old_model = (
            item.get("vector_model_id"),
            item.get("vector_model_updated_at"),
            int(item.get("vector_dimension") or 0),
        )
        new_model = (model_id, model_updated_at, dimension)
        model_changed = (not never_built) and (old_model != new_model)
        full_rebuild = force or never_built or model_changed
        with self._lock:
            active = self._jobs.get(knowledge_base_id)
            if active is not None and not active.done():
                # Update the target fingerprint while the old task finishes. Its
                # completion check will reject the stale result and resubmit it.
                queued = self.repository.mark_vector_pending(
                    knowledge_base_id,
                    revision,
                    collection_name,
                    model_id,
                    model_updated_at,
                    dimension,
                    _now(),
                    activate=activate,
                )
                if not queued:
                    return self.knowledge_base_lookup(knowledge_base_id)
                return self.knowledge_base_lookup(knowledge_base_id)

            if (
                not force
                and item.get("vector_status") == "ready"
                and item.get("vector_revision") is not None
                and int(item["vector_revision"]) == revision
                and item.get("vector_collection")
                and item.get("vector_model_id") == model_id
                and item.get("vector_model_updated_at") == model_updated_at
                and int(item.get("vector_dimension") or 0) == dimension
            ):
                return item

            queued = self.repository.mark_vector_pending(
                knowledge_base_id,
                revision,
                collection_name,
                model_id,
                model_updated_at,
                dimension,
                _now(),
                activate=activate,
            )
            if not queued:
                return self.knowledge_base_lookup(knowledge_base_id)
            self._jobs[knowledge_base_id] = self._executor.submit(
                self._build,
                knowledge_base_id,
                revision,
                collection_name,
                Path(str(item["workspace_dir"])),
                config,
                full_rebuild=full_rebuild,
            )
        return self.knowledge_base_lookup(knowledge_base_id)

    def recover_pending(self) -> None:
        """Resume pending vector builds after an API process restart."""
        for item in self.repository.list("knowledge_bases"):
            if item.get("vector_status") in {"pending", "building"}:
                try:
                    self.schedule(str(item["id"]))
                except Exception:
                    self.repository.fail_vector_configuration(
                        str(item["id"]), "无法提交向量索引构建任务", _now()
                    )

    def mark_embedding_config_changed(self) -> None:
        """Re-evaluate every knowledge base after the default embedding changes."""
        for item in self.repository.list("knowledge_bases"):
            try:
                self.schedule(str(item["id"]))
            except Exception:
                logger.exception(
                    "vector.schedule_after_model_change_failed knowledge_base_id=%s",
                    item["id"],
                )

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)

    def delete_collection(
        self, collection_name: str | None, knowledge_base_id: str
    ) -> None:
        """Stop a pending build and best-effort drop the KB's vector collection."""
        with self._lock:
            active = self._jobs.pop(knowledge_base_id, None)
            if active is not None:
                active.cancel()
        if not collection_name:
            return
        try:
            store = DocVectorStore(
                uri=self.settings.milvus_uri,
                token=self.settings.milvus_token,
                collection_name=collection_name,
            )
            if store.client.has_collection(collection_name):
                store.client.drop_collection(collection_name)
        except Exception:
            logger.warning(
                "knowledge_base.vector_cleanup_failed collection=%s",
                collection_name,
                exc_info=True,
            )

    def _build(
        self,
        knowledge_base_id: str,
        revision: int,
        collection_name: str,
        workspace_dir: Path,
        config: dict[str, Any],
        *,
        full_rebuild: bool = False,
    ) -> None:
        model_id = str(config["profile_id"])
        model_updated_at = str(config["profile_updated_at"])
        dimension = int(config["dimension"])
        ready = False

        def report_progress(
            stage: str,
            documents_completed: int,
            documents_total: int,
            records_processed: int,
        ) -> None:
            try:
                self.repository.update_vector_progress(
                    knowledge_base_id,
                    revision,
                    model_id,
                    model_updated_at,
                    dimension,
                    stage,
                    documents_completed,
                    documents_total,
                    records_processed,
                    _now(),
                )
            except Exception:
                # Progress is observability; it must not turn a valid index build
                # into a failed build when SQLite is temporarily unavailable.
                logger.warning(
                    "vector.progress_persist_failed knowledge_base_id=%s",
                    knowledge_base_id,
                    exc_info=True,
                )

        try:
            building = self.repository.mark_vector_building(
                knowledge_base_id,
                revision,
                collection_name,
                model_id,
                model_updated_at,
                dimension,
                _now(),
            )
            if building:
                with workspace_lock(workspace_dir):
                    self._build_vector_index(
                        knowledge_base_id,
                        revision,
                        collection_name,
                        workspace_dir,
                        config,
                        dimension,
                        full_rebuild,
                        report_progress,
                    )
                ready = self.repository.finish_vector_build(
                    knowledge_base_id,
                    revision,
                    collection_name,
                    model_id,
                    model_updated_at,
                    dimension,
                    _now(),
                )
        except Exception as exc:
            self.repository.fail_vector_build(
                knowledge_base_id,
                revision,
                model_id,
                model_updated_at,
                dimension,
                f"{type(exc).__name__}: {exc}",
                _now(),
            )
            ready = False
        finally:
            with self._lock:
                self._jobs.pop(knowledge_base_id, None)

        if not ready:
            try:
                latest = self.knowledge_base_lookup(knowledge_base_id)
            except ApiError as exc:
                if exc.status_code == 404:
                    return
                raise
            if (
                int(latest.get("content_version", 0)) != revision
                or latest.get("vector_model_id") != model_id
                or latest.get("vector_model_updated_at") != model_updated_at
                or int(latest.get("vector_dimension") or 0) != dimension
            ):
                self.schedule(knowledge_base_id)

    def _build_vector_index(
        self,
        knowledge_base_id: str,
        revision: int,
        collection_name: str,
        workspace_dir: Path,
        config: dict[str, Any],
        dimension: int,
        full_rebuild: bool,
        report_progress,
    ) -> None:
        """驱动向量增量/全量构建（设计文档 §5.3/§5.4）。

        - ``full_rebuild``（force / 模型变更）-> 全量蓝绿：``mark_all_vector_dirty`` ->
          staging + publish（``force=True``），处理全部脏文档后置干净。
        - 否则 -> 增量：collection 缺失则 ``mark_all_vector_dirty``（建空表后全量写入，
          无需蓝绿）；仅处理脏集，每文档先 ``delete_by_doc`` 后 embed+insert；
          脏集为空则跳过（仅 finish 推进 revision）。
        - 不变式（§6）：先落库 Milvus（insert+flush）、后置干净。
        """
        if full_rebuild:
            self.repository.mark_all_vector_dirty(knowledge_base_id, _now())
            dirty_doc_ids = self.repository.list_vector_dirty_documents(
                knowledge_base_id
            )
            build_doc_vectors(
                workspace_dir,
                uri=self.settings.milvus_uri,
                token=self.settings.milvus_token,
                collection_name=collection_name,
                embedding_model=str(config["model"]),
                embedding_dim=dimension,
                knowledge_base_id=knowledge_base_id,
                api_key=str(config["api_key"]),
                base_url=str(config["base_url"]),
                allow_env_fallback=False,
                progress_callback=report_progress,
                doc_ids=dirty_doc_ids,
                force=True,
            )
            self.repository.mark_documents_vector_indexed(
                knowledge_base_id, dirty_doc_ids, revision, _now()
            )
            return

        if not self._vector_collection_exists(collection_name, dimension):
            # collection 缺失（被外部 drop / 首次构建）：建空表后全量写入，无需蓝绿。
            self.repository.mark_all_vector_dirty(knowledge_base_id, _now())
        dirty_doc_ids = self.repository.list_vector_dirty_documents(knowledge_base_id)
        if not dirty_doc_ids:
            return
        build_doc_vectors(
            workspace_dir,
            uri=self.settings.milvus_uri,
            token=self.settings.milvus_token,
            collection_name=collection_name,
            embedding_model=str(config["model"]),
            embedding_dim=dimension,
            knowledge_base_id=knowledge_base_id,
            api_key=str(config["api_key"]),
            base_url=str(config["base_url"]),
            allow_env_fallback=False,
            progress_callback=report_progress,
            doc_ids=dirty_doc_ids,
            force=False,
        )
        self.repository.mark_documents_vector_indexed(
            knowledge_base_id, dirty_doc_ids, revision, _now()
        )

    def _vector_collection_exists(self, collection_name: str, dimension: int) -> bool:
        """探测 collection 是否存在（增量路径使用）。Milvus 不可用时抛出。"""
        store = DocVectorStore(
            uri=self.settings.milvus_uri,
            token=self.settings.milvus_token,
            collection_name=collection_name,
            dimension=dimension,
        )
        return bool(store.client.has_collection(collection_name))


__all__ = ["VectorIndexService"]
