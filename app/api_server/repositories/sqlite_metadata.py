"""SQLAlchemy persistence for API metadata and upload state."""

from __future__ import annotations

from typing import Any, Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api_server.database.connection import SQLiteConnectionFactory
from app.api_server.database.models import (
    Application,
    KnowledgeBase,
)
from app.api_server.repositories.metadata.indexing import IndexStateRepositoryMixin
from app.api_server.repositories.metadata.model_profiles import (
    ModelProfileRepositoryMixin,
)
from app.api_server.repositories.metadata.documents import DocumentRepositoryMixin
from app.api_server.repositories.metadata.parse_tasks import (
    DocumentParseRepositoryMixin,
)
from app.api_server.repositories.metadata.uploads import UploadOperationRepositoryMixin

MetadataKind = Literal["knowledge_bases", "applications"]


def _knowledge_base_dict(item: KnowledgeBase) -> dict[str, Any]:
    return {
        "id": item.id,
        "name": item.name,
        "description": item.description,
        "status": item.status,
        "workspace_relpath": item.workspace_relpath,
        "document_count": item.document_count,
        "summary_enabled": bool(item.summary_enabled),
        "content_version": item.content_version,
        "fts_status": item.fts_status,
        "fts_revision": item.fts_revision,
        "fts_target_revision": item.fts_target_revision,
        "fts_collection": item.fts_collection,
        "fts_error": item.fts_error,
        "vector_status": item.vector_status,
        "vector_revision": item.vector_revision,
        "vector_target_revision": item.vector_target_revision,
        "vector_collection": item.vector_collection,
        "vector_error": item.vector_error,
        "embedding_model_id": item.vector_model_id,
        "vector_model_id": item.vector_model_id,
        "vector_model_updated_at": item.vector_model_updated_at,
        "vector_dimension": item.vector_dimension,
        "vector_progress_stage": item.vector_progress_stage,
        "vector_documents_total": item.vector_documents_total,
        "vector_documents_completed": item.vector_documents_completed,
        "vector_records_processed": item.vector_records_processed,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
    }


def _application_dict(item: Application) -> dict[str, Any]:
    return {
        "id": item.id,
        "name": item.name,
        "description": item.description,
        "knowledge_base_id": item.knowledge_base_id,
        "model": item.model,
        "provider": item.provider,
        "search_mode": item.search_mode,
        "config_version": item.config_version,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
    }


class SQLiteMetadataRepository(
    IndexStateRepositoryMixin,
    ModelProfileRepositoryMixin,
    UploadOperationRepositoryMixin,
    DocumentParseRepositoryMixin,
    DocumentRepositoryMixin,
):
    """Persist API metadata with typed ORM entities and short transactions."""

    def __init__(self, factory: SQLiteConnectionFactory) -> None:
        self.factory = factory

    @staticmethod
    def _table(kind: str) -> MetadataKind:
        if kind not in ("knowledge_bases", "applications"):
            raise ValueError(f"unsupported metadata kind: {kind}")
        return kind

    def list(self, kind: str) -> list[dict[str, Any]]:
        table = self._table(kind)
        with self.factory.session_scope() as session:
            if table == "knowledge_bases":
                items = session.scalars(
                    select(KnowledgeBase).order_by(KnowledgeBase.created_at)
                ).all()
                return [_knowledge_base_dict(item) for item in items]
            items = session.scalars(
                select(Application).order_by(Application.created_at)
            ).all()
            return [_application_dict(item) for item in items]

    def get(self, kind: str, item_id: str) -> dict[str, Any] | None:
        table = self._table(kind)
        with self.factory.session_scope() as session:
            if table == "knowledge_bases":
                item = session.get(KnowledgeBase, item_id)
                return _knowledge_base_dict(item) if item is not None else None
            item = session.get(Application, item_id)
            return _application_dict(item) if item is not None else None

    def put(self, kind: str, item_id: str, item: dict[str, Any]) -> None:
        table = self._table(kind)
        with self.factory.session_scope(write=True) as session:
            if table == "knowledge_bases":
                self._put_knowledge_base(session, item_id, item)
            else:
                self._put_application(session, item_id, item)

    def update_knowledge_base_settings(
        self, knowledge_base_id: str, values: dict[str, Any]
    ) -> dict[str, Any]:
        """Update mutable settings without overwriting concurrent index state."""
        allowed_fields = {
            "name",
            "summary_enabled",
            "vector_model_id",
            "vector_status",
            "vector_revision",
            "vector_target_revision",
            "vector_model_updated_at",
            "vector_dimension",
            "vector_error",
            "vector_progress_stage",
            "vector_documents_total",
            "vector_documents_completed",
            "vector_records_processed",
            "updated_at",
        }
        unexpected = values.keys() - allowed_fields
        if unexpected:
            raise ValueError(
                f"unsupported knowledge base setting fields: {sorted(unexpected)}"
            )
        with self.factory.session_scope(write=True) as session:
            entity = session.get(KnowledgeBase, knowledge_base_id)
            if entity is None:
                raise KeyError(knowledge_base_id)
            for field, value in values.items():
                setattr(entity, field, value)
            session.flush()
            return _knowledge_base_dict(entity)

    def count_applications_for_knowledge_base(self, knowledge_base_id: str) -> int:
        with self.factory.session_scope() as session:
            count = session.scalar(
                select(func.count(Application.id)).where(
                    Application.knowledge_base_id == knowledge_base_id
                )
            )
            return int(count or 0)

    def delete_knowledge_base(self, knowledge_base_id: str) -> bool:
        """Hard-delete a knowledge base after its workspace is cleaned up."""
        with self.factory.session_scope(write=True) as session:
            item = session.get(KnowledgeBase, knowledge_base_id)
            if item is None:
                return False
            session.delete(item)
            session.flush()
            return True

    def delete_application(self, application_id: str) -> bool:
        """Hard-delete an application; dependent chat data cascades in SQLite."""
        with self.factory.session_scope(write=True) as session:
            item = session.get(Application, application_id)
            if item is None:
                return False
            session.delete(item)
            session.flush()
            return True

    @staticmethod
    def _put_knowledge_base(
        session: Session, item_id: str, item: dict[str, Any]
    ) -> None:
        entity = session.get(KnowledgeBase, item_id)
        if entity is None:
            entity = KnowledgeBase(
                id=item_id,
                name=item["name"],
                description=item.get("description", ""),
                status=item.get("status", "ready"),
                workspace_relpath=item.get("workspace_relpath", item_id),
                document_count=int(item.get("document_count", 0)),
                summary_enabled=bool(item.get("summary_enabled", True)),
                content_version=int(item.get("content_version", 0)),
                fts_status=item.get("fts_status", "disabled"),
                fts_revision=item.get("fts_revision"),
                fts_target_revision=item.get("fts_target_revision"),
                fts_collection=item.get("fts_collection"),
                fts_error=item.get("fts_error"),
                vector_status=item.get("vector_status", "disabled"),
                vector_revision=item.get("vector_revision"),
                vector_target_revision=item.get("vector_target_revision"),
                vector_collection=item.get("vector_collection"),
                vector_error=item.get("vector_error"),
                vector_model_id=item.get(
                    "embedding_model_id", item.get("vector_model_id")
                ),
                vector_model_updated_at=item.get("vector_model_updated_at"),
                vector_dimension=item.get("vector_dimension"),
                vector_progress_stage=item.get("vector_progress_stage"),
                vector_documents_total=item.get("vector_documents_total"),
                vector_documents_completed=item.get("vector_documents_completed"),
                vector_records_processed=item.get("vector_records_processed"),
                created_at=item["created_at"],
                updated_at=item["updated_at"],
            )
            session.add(entity)
        else:
            entity.name = item["name"]
            entity.description = item.get("description", "")
            entity.status = item.get("status", "ready")
            entity.workspace_relpath = item.get("workspace_relpath", item_id)
            entity.document_count = int(item.get("document_count", 0))
            entity.summary_enabled = bool(item.get("summary_enabled", True))
            entity.content_version = int(item.get("content_version", 0))
            entity.fts_status = item.get("fts_status", "disabled")
            entity.fts_revision = item.get("fts_revision")
            entity.fts_target_revision = item.get("fts_target_revision")
            entity.fts_collection = item.get("fts_collection")
            entity.fts_error = item.get("fts_error")
            entity.vector_status = item.get("vector_status", "disabled")
            entity.vector_revision = item.get("vector_revision")
            entity.vector_target_revision = item.get("vector_target_revision")
            entity.vector_collection = item.get("vector_collection")
            entity.vector_error = item.get("vector_error")
            entity.vector_model_id = item.get(
                "embedding_model_id", item.get("vector_model_id")
            )
            entity.vector_model_updated_at = item.get("vector_model_updated_at")
            entity.vector_dimension = item.get("vector_dimension")
            entity.vector_progress_stage = item.get("vector_progress_stage")
            entity.vector_documents_total = item.get("vector_documents_total")
            entity.vector_documents_completed = item.get("vector_documents_completed")
            entity.vector_records_processed = item.get("vector_records_processed")
            entity.updated_at = item["updated_at"]

    @staticmethod
    def _put_application(session: Session, item_id: str, item: dict[str, Any]) -> None:
        entity = session.get(Application, item_id)
        if entity is None:
            session.add(
                Application(
                    id=item_id,
                    name=item["name"],
                    description=item.get("description", ""),
                    knowledge_base_id=item["knowledge_base_id"],
                    model=item.get("model"),
                    provider=item.get("provider", "default"),
                    search_mode="fts",
                    config_version=int(item.get("config_version", 1)),
                    created_at=item["created_at"],
                    updated_at=item["updated_at"],
                )
            )
            return
        entity.name = item["name"]
        entity.description = item.get("description", "")
        entity.knowledge_base_id = item["knowledge_base_id"]
        entity.model = item.get("model")
        entity.provider = item.get("provider", "default")
        entity.search_mode = "fts"
        entity.config_version = int(item.get("config_version", 1))
        entity.updated_at = item["updated_at"]


__all__ = ["MetadataKind", "SQLiteMetadataRepository"]
