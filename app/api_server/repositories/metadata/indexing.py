"""FTS and vector index-state persistence operations."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select, update

from app.api_server.database.connection import SQLiteConnectionFactory
from app.api_server.database.models import Document, KnowledgeBase


class IndexStateRepositoryMixin:
    """Track document dirtiness and knowledge-base index build state."""

    factory: SQLiteConnectionFactory

    def list_fts_dirty_documents(self, knowledge_base_id: str) -> list[str]:
        """返回需要（重）建 FTS 记录的 ``status='ready'`` 文档 id（脏集）。

        脏 = ``fts_indexed_version IS NULL``（见设计文档 §5.2）。非 ready 文档不参与
        索引构建（其 ``<doc_id>.json`` 尚未就绪）。
        """
        with self.factory.session_scope() as session:
            rows = session.scalars(
                select(Document.id)
                .where(
                    Document.knowledge_base_id == knowledge_base_id,
                    Document.status == "ready",
                    Document.fts_indexed_version.is_(None),
                )
                .order_by(Document.created_at)
            ).all()
            return [str(row) for row in rows]

    def mark_documents_fts_indexed(
        self,
        knowledge_base_id: str,
        document_ids: list[str],
        revision: int,
        now: str,
    ) -> None:
        """置干净：一批文档的 FTS 记录已在 ``revision`` 时写入当前 collection。

        **先落库 Milvus、后置干净**（设计文档 §6）：仅在 insert+flush 成功后调用。
        按 ``doc_id`` 精确置，避免误标并发新增的脏文档。
        """
        if not document_ids:
            return
        with self.factory.session_scope(write=True) as session:
            session.execute(
                update(Document)
                .where(
                    Document.knowledge_base_id == knowledge_base_id,
                    Document.id.in_([str(doc_id) for doc_id in document_ids]),
                )
                .values(fts_indexed_version=revision, updated_at=now)
            )

    def mark_all_fts_dirty(self, knowledge_base_id: str, now: str) -> None:
        """将该 KB 全部文档置脏（force 重建 / collection 缺失 / 迁移后）。"""
        with self.factory.session_scope(write=True) as session:
            session.execute(
                update(Document)
                .where(Document.knowledge_base_id == knowledge_base_id)
                .values(fts_indexed_version=None, updated_at=now)
            )

    def list_vector_dirty_documents(self, knowledge_base_id: str) -> list[str]:
        """返回需要（重）建向量记录的 ``status='ready'`` 文档 id（脏集）。

        脏 = ``vector_indexed_version IS NULL``（见设计文档 §5.2）。
        """
        with self.factory.session_scope() as session:
            rows = session.scalars(
                select(Document.id)
                .where(
                    Document.knowledge_base_id == knowledge_base_id,
                    Document.status == "ready",
                    Document.vector_indexed_version.is_(None),
                )
                .order_by(Document.created_at)
            ).all()
            return [str(row) for row in rows]

    def mark_documents_vector_indexed(
        self,
        knowledge_base_id: str,
        document_ids: list[str],
        revision: int,
        now: str,
    ) -> None:
        """置干净：一批文档的向量记录已在 ``revision`` 时写入当前 collection。

        **先落库 Milvus、后置干净**（设计文档 §6）：仅在 insert+flush 成功后调用。
        """
        if not document_ids:
            return
        with self.factory.session_scope(write=True) as session:
            session.execute(
                update(Document)
                .where(
                    Document.knowledge_base_id == knowledge_base_id,
                    Document.id.in_([str(doc_id) for doc_id in document_ids]),
                )
                .values(vector_indexed_version=revision, updated_at=now)
            )

    def mark_all_vector_dirty(self, knowledge_base_id: str, now: str) -> None:
        """将该 KB 全部文档的向量标记置脏（force 重建 / 模型变更 / collection 缺失）。"""
        with self.factory.session_scope(write=True) as session:
            session.execute(
                update(Document)
                .where(Document.knowledge_base_id == knowledge_base_id)
                .values(vector_indexed_version=None, updated_at=now)
            )

    def reconcile_document_count(
        self, knowledge_base_id: str, document_count: int, now: str
    ) -> int | None:
        """Align SQLite metadata with the durable workspace manifest."""
        with self.factory.session_scope(write=True) as session:
            knowledge_base = session.get(KnowledgeBase, knowledge_base_id)
            if knowledge_base is None:
                return None
            if knowledge_base.document_count == document_count:
                return knowledge_base.content_version
            next_version = knowledge_base.content_version + 1
            knowledge_base.document_count = document_count
            knowledge_base.content_version = next_version
            knowledge_base.fts_status = "pending"
            knowledge_base.fts_target_revision = next_version
            if knowledge_base.vector_status != "disabled":
                knowledge_base.vector_status = "pending"
                knowledge_base.vector_target_revision = next_version
                knowledge_base.vector_progress_stage = "queued"
                knowledge_base.vector_documents_total = document_count
                knowledge_base.vector_documents_completed = 0
                knowledge_base.vector_records_processed = 0
            knowledge_base.updated_at = now
            return next_version

    def mark_fts_pending(
        self,
        knowledge_base_id: str,
        target_revision: int,
        collection_name: str,
        now: str,
    ) -> None:
        """Record that a workspace revision needs an FTS rebuild."""
        self._update_fts(
            knowledge_base_id,
            {
                "fts_status": "pending",
                "fts_target_revision": target_revision,
                "fts_collection": collection_name,
                "fts_error": None,
                "updated_at": now,
            },
        )

    def mark_fts_building(
        self,
        knowledge_base_id: str,
        target_revision: int,
        collection_name: str,
        now: str,
    ) -> None:
        """Record that an FTS build is currently running."""
        self._update_fts(
            knowledge_base_id,
            {
                "fts_status": "building",
                "fts_target_revision": target_revision,
                "fts_collection": collection_name,
                "fts_error": None,
                "updated_at": now,
            },
        )

    def finish_fts_build(
        self,
        knowledge_base_id: str,
        revision: int,
        collection_name: str,
        now: str,
    ) -> bool:
        """Mark a build ready only when its revision is still current."""
        with self.factory.session_scope(write=True) as session:
            knowledge_base = session.get(KnowledgeBase, knowledge_base_id)
            if knowledge_base is None:
                return False
            if knowledge_base.content_version != revision:
                knowledge_base.fts_status = "pending"
                knowledge_base.fts_target_revision = knowledge_base.content_version
                knowledge_base.fts_collection = collection_name
                knowledge_base.updated_at = now
                return False
            knowledge_base.fts_status = "ready"
            knowledge_base.fts_revision = revision
            knowledge_base.fts_target_revision = revision
            knowledge_base.fts_collection = collection_name
            knowledge_base.fts_error = None
            knowledge_base.updated_at = now
            return True

    def advance_fts_revision_after_surgical_delete(
        self,
        knowledge_base_id: str,
        revision: int,
        collection_name: str,
        now: str,
    ) -> bool:
        """定向删除成功后推进 ``fts_revision``，带版本闸门。

        复用 ``finish_fts_build`` 的闸门语义：仅当 ``content_version`` 仍等于本次
        删除产生的新版本时才置 ``ready`` 并推进 ``fts_revision``；若期间又有上传把
        版本推高，则保持 ``pending``（``delete_document`` 已置）并返回 False，由调用方
        回退到调度重建（重建从剩余 ``_meta.json`` 重建，排除已删文档）。
        """
        with self.factory.session_scope(write=True) as session:
            knowledge_base = session.get(KnowledgeBase, knowledge_base_id)
            if knowledge_base is None:
                return False
            if knowledge_base.content_version != revision:
                return False
            knowledge_base.fts_status = "ready"
            knowledge_base.fts_revision = revision
            knowledge_base.fts_target_revision = revision
            knowledge_base.fts_collection = collection_name
            knowledge_base.fts_error = None
            knowledge_base.updated_at = now
            return True

    def fail_fts_build(
        self,
        knowledge_base_id: str,
        revision: int,
        error_message: str,
        now: str,
    ) -> None:
        """Persist a build error without hiding a newer workspace revision."""
        with self.factory.session_scope(write=True) as session:
            knowledge_base = session.get(KnowledgeBase, knowledge_base_id)
            if knowledge_base is None:
                return
            if knowledge_base.content_version == revision:
                knowledge_base.fts_status = "failed"
                knowledge_base.fts_error = error_message[:2_000]
                knowledge_base.updated_at = now
            else:
                knowledge_base.fts_status = "pending"
                knowledge_base.fts_target_revision = knowledge_base.content_version
                knowledge_base.fts_error = error_message[:2_000]
                knowledge_base.updated_at = now

    def disable_fts(self, knowledge_base_id: str, now: str) -> None:
        """Keep FTS metadata disabled when the server capability is off."""
        self._update_fts(
            knowledge_base_id,
            {
                "fts_status": "disabled",
                "fts_target_revision": None,
                "fts_error": None,
                "updated_at": now,
            },
        )

    def _update_fts(self, knowledge_base_id: str, values: dict[str, Any]) -> None:
        with self.factory.session_scope(write=True) as session:
            knowledge_base = session.get(KnowledgeBase, knowledge_base_id)
            if knowledge_base is None:
                return
            knowledge_base.fts_status = values["fts_status"]
            knowledge_base.fts_target_revision = values.get("fts_target_revision")
            if values.get("fts_collection") is not None:
                knowledge_base.fts_collection = values["fts_collection"]
            knowledge_base.fts_error = values.get("fts_error")
            knowledge_base.updated_at = values["updated_at"]

    def mark_vector_pending(
        self,
        knowledge_base_id: str,
        target_revision: int,
        collection_name: str,
        model_id: str,
        model_updated_at: str,
        dimension: int,
        now: str,
        *,
        activate: bool = False,
    ) -> bool:
        with self.factory.session_scope(write=True) as session:
            knowledge_base = session.get(KnowledgeBase, knowledge_base_id)
            if knowledge_base is None:
                return False
            if knowledge_base.vector_status == "disabled" and not activate:
                return False
            knowledge_base.vector_status = "pending"
            knowledge_base.vector_target_revision = target_revision
            knowledge_base.vector_collection = collection_name
            knowledge_base.vector_model_id = model_id
            knowledge_base.vector_model_updated_at = model_updated_at
            knowledge_base.vector_dimension = dimension
            knowledge_base.vector_progress_stage = "queued"
            knowledge_base.vector_documents_completed = 0
            knowledge_base.vector_records_processed = 0
            knowledge_base.vector_error = None
            knowledge_base.updated_at = now
            return True

    def mark_vector_building(
        self,
        knowledge_base_id: str,
        target_revision: int,
        collection_name: str,
        model_id: str,
        model_updated_at: str,
        dimension: int,
        now: str,
    ) -> bool:
        with self.factory.session_scope(write=True) as session:
            knowledge_base = session.get(KnowledgeBase, knowledge_base_id)
            if knowledge_base is None:
                return False
            if (
                knowledge_base.vector_status == "disabled"
                or knowledge_base.content_version != target_revision
                or knowledge_base.vector_model_id != model_id
                or knowledge_base.vector_model_updated_at != model_updated_at
                or knowledge_base.vector_dimension != dimension
            ):
                return False
            knowledge_base.vector_status = "building"
            knowledge_base.vector_target_revision = target_revision
            knowledge_base.vector_collection = collection_name
            knowledge_base.vector_error = None
            knowledge_base.vector_progress_stage = "starting"
            knowledge_base.vector_documents_completed = 0
            knowledge_base.vector_records_processed = 0
            knowledge_base.updated_at = now
            return True

    def update_vector_progress(
        self,
        knowledge_base_id: str,
        revision: int,
        model_id: str,
        model_updated_at: str,
        dimension: int,
        stage: str,
        documents_completed: int,
        documents_total: int,
        records_processed: int,
        now: str,
    ) -> bool:
        """Persist progress only while the build fingerprint is still current."""
        with self.factory.session_scope(write=True) as session:
            knowledge_base = session.get(KnowledgeBase, knowledge_base_id)
            if knowledge_base is None:
                return False
            if (
                knowledge_base.vector_status != "building"
                or knowledge_base.content_version != revision
                or knowledge_base.vector_model_id != model_id
                or knowledge_base.vector_model_updated_at != model_updated_at
                or knowledge_base.vector_dimension != dimension
            ):
                return False
            knowledge_base.vector_progress_stage = stage
            knowledge_base.vector_documents_completed = max(documents_completed, 0)
            knowledge_base.vector_documents_total = max(documents_total, 0)
            knowledge_base.vector_records_processed = max(records_processed, 0)
            knowledge_base.updated_at = now
            return True

    def finish_vector_build(
        self,
        knowledge_base_id: str,
        revision: int,
        collection_name: str,
        model_id: str,
        model_updated_at: str,
        dimension: int,
        now: str,
    ) -> bool:
        with self.factory.session_scope(write=True) as session:
            knowledge_base = session.get(KnowledgeBase, knowledge_base_id)
            if knowledge_base is None:
                return False
            current_model = (
                knowledge_base.vector_model_id,
                knowledge_base.vector_model_updated_at,
                knowledge_base.vector_dimension,
            )
            build_model = (model_id, model_updated_at, dimension)
            if (
                knowledge_base.vector_status not in {"pending", "building"}
                or knowledge_base.content_version != revision
                or current_model != build_model
            ):
                if knowledge_base.vector_status == "disabled":
                    return False
                knowledge_base.vector_status = "pending"
                knowledge_base.vector_target_revision = knowledge_base.content_version
                knowledge_base.updated_at = now
                return False
            knowledge_base.vector_status = "ready"
            knowledge_base.vector_revision = revision
            knowledge_base.vector_target_revision = revision
            knowledge_base.vector_collection = collection_name
            knowledge_base.vector_error = None
            knowledge_base.vector_progress_stage = "completed"
            if knowledge_base.vector_documents_total is not None:
                knowledge_base.vector_documents_completed = (
                    knowledge_base.vector_documents_total
                )
            knowledge_base.updated_at = now
            return True

    def advance_vector_revision_after_surgical_delete(
        self,
        knowledge_base_id: str,
        revision: int,
        collection_name: str,
        model_id: str,
        model_updated_at: str,
        dimension: int,
        now: str,
    ) -> bool:
        """定向删除成功后推进 ``vector_revision``，带版本+模型指纹闸门。

        复用 ``finish_vector_build`` 的闸门语义：仅当 ``content_version`` 仍等于本次
        删除产生的新版本、且向量模型指纹未变时才置 ``ready``；否则保持 ``pending``，
        交由在途重建处理（重建用新模型从剩余 ``_meta.json`` 重建，排除已删文档）。
        """
        with self.factory.session_scope(write=True) as session:
            knowledge_base = session.get(KnowledgeBase, knowledge_base_id)
            if knowledge_base is None:
                return False
            current_model = (
                knowledge_base.vector_model_id,
                knowledge_base.vector_model_updated_at,
                knowledge_base.vector_dimension,
            )
            if knowledge_base.content_version != revision or current_model != (
                model_id,
                model_updated_at,
                dimension,
            ):
                return False
            knowledge_base.vector_status = "ready"
            knowledge_base.vector_revision = revision
            knowledge_base.vector_target_revision = revision
            knowledge_base.vector_collection = collection_name
            knowledge_base.vector_error = None
            knowledge_base.updated_at = now
            return True

    def fail_vector_build(
        self,
        knowledge_base_id: str,
        revision: int,
        model_id: str,
        model_updated_at: str,
        dimension: int,
        error_message: str,
        now: str,
    ) -> None:
        with self.factory.session_scope(write=True) as session:
            knowledge_base = session.get(KnowledgeBase, knowledge_base_id)
            if knowledge_base is None:
                return
            if knowledge_base.vector_status == "disabled":
                return
            current_model = (
                knowledge_base.vector_model_id,
                knowledge_base.vector_model_updated_at,
                knowledge_base.vector_dimension,
            )
            if knowledge_base.content_version == revision and current_model == (
                model_id,
                model_updated_at,
                dimension,
            ):
                knowledge_base.vector_status = "failed"
                knowledge_base.vector_error = error_message[:2_000]
                knowledge_base.vector_progress_stage = "failed"
            else:
                knowledge_base.vector_status = "pending"
                knowledge_base.vector_target_revision = knowledge_base.content_version
                knowledge_base.vector_error = error_message[:2_000]
                knowledge_base.vector_progress_stage = "queued"
            knowledge_base.updated_at = now

    def disable_vector(self, knowledge_base_id: str, now: str) -> None:
        self._update_vector(
            knowledge_base_id,
            {
                "vector_status": "disabled",
                "vector_target_revision": None,
                "vector_error": None,
                "vector_progress_stage": None,
                "vector_documents_total": None,
                "vector_documents_completed": None,
                "vector_records_processed": None,
                "updated_at": now,
            },
        )

    def fail_vector_configuration(
        self, knowledge_base_id: str, error_message: str, now: str
    ) -> None:
        self._update_vector(
            knowledge_base_id,
            {
                "vector_status": "failed",
                "vector_error": error_message[:2_000],
                "vector_progress_stage": "failed",
                "updated_at": now,
            },
        )

    def _update_vector(self, knowledge_base_id: str, values: dict[str, Any]) -> None:
        with self.factory.session_scope(write=True) as session:
            knowledge_base = session.get(KnowledgeBase, knowledge_base_id)
            if knowledge_base is None:
                return
            knowledge_base.vector_status = values["vector_status"]
            knowledge_base.vector_target_revision = values.get("vector_target_revision")
            if values.get("vector_collection") is not None:
                knowledge_base.vector_collection = values["vector_collection"]
            if values.get("vector_model_id") is not None:
                knowledge_base.vector_model_id = values["vector_model_id"]
            if values.get("vector_model_updated_at") is not None:
                knowledge_base.vector_model_updated_at = values[
                    "vector_model_updated_at"
                ]
            if values.get("vector_dimension") is not None:
                knowledge_base.vector_dimension = values["vector_dimension"]
            if "vector_progress_stage" in values:
                knowledge_base.vector_progress_stage = values["vector_progress_stage"]
            if "vector_documents_total" in values:
                knowledge_base.vector_documents_total = values["vector_documents_total"]
            if "vector_documents_completed" in values:
                knowledge_base.vector_documents_completed = values[
                    "vector_documents_completed"
                ]
            if "vector_records_processed" in values:
                knowledge_base.vector_records_processed = values[
                    "vector_records_processed"
                ]
            if "vector_error" in values:
                knowledge_base.vector_error = values["vector_error"]
            knowledge_base.updated_at = values["updated_at"]
