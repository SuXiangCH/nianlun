"""Document-record persistence operations."""

from __future__ import annotations

from typing import Any

from sqlalchemy import delete, func, select

from app.api_server.database.connection import SQLiteConnectionFactory
from app.api_server.database.models import Document, KnowledgeBase, UploadOperation


def _document_dict(item: Document) -> dict[str, Any]:
    return {
        field: getattr(item, field)
        for field in (
            "id",
            "knowledge_base_id",
            "original_filename",
            "file_extension",
            "mime_type",
            "size_bytes",
            "source_relpath",
            "source_sha256",
            "parser",
            "status",
            "parsed_markdown_relpath",
            "parsed_content_version",
            "fts_indexed_version",
            "vector_indexed_version",
            "error_code",
            "error_message",
            "created_at",
            "updated_at",
            "completed_at",
        )
    }


class DocumentRepositoryMixin:
    factory: SQLiteConnectionFactory

    def list_parsing_documents(self) -> list[dict[str, Any]]:
        with self.factory.session_scope() as session:
            items = session.scalars(
                select(Document).where(
                    Document.parser == "mineru", Document.status == "parsing"
                )
            ).all()
            return [_document_dict(item) for item in items]

    def list_documents(self, knowledge_base_id: str) -> list[dict[str, Any]]:
        with self.factory.session_scope() as session:
            items = session.scalars(
                select(Document)
                .where(Document.knowledge_base_id == knowledge_base_id)
                .order_by(Document.created_at.desc())
            ).all()
            return [_document_dict(item) for item in items]

    def get_document(
        self, knowledge_base_id: str, document_id: str
    ) -> dict[str, Any] | None:
        with self.factory.session_scope() as session:
            item = session.scalar(
                select(Document).where(
                    Document.id == document_id,
                    Document.knowledge_base_id == knowledge_base_id,
                )
            )
            return _document_dict(item) if item is not None else None

    def get_document_by_hash(
        self, knowledge_base_id: str, source_sha256: str
    ) -> dict[str, Any] | None:
        with self.factory.session_scope() as session:
            item = session.scalar(
                select(Document).where(
                    Document.knowledge_base_id == knowledge_base_id,
                    Document.source_sha256 == source_sha256,
                )
            )
            return _document_dict(item) if item is not None else None

    def create_document(self, values: dict[str, Any]) -> dict[str, Any]:
        with self.factory.session_scope(write=True) as session:
            item = Document(
                id=str(values["id"]),
                knowledge_base_id=str(values["knowledge_base_id"]),
                original_filename=str(values["original_filename"]),
                file_extension=str(values["file_extension"]),
                mime_type=str(values.get("mime_type", "application/octet-stream")),
                size_bytes=int(values["size_bytes"]),
                source_relpath=str(values["source_relpath"]),
                source_sha256=str(values["source_sha256"]),
                parser=str(values["parser"]),
                status=str(values.get("status", "uploaded")),
                parsed_markdown_relpath=values.get("parsed_markdown_relpath"),
                parsed_content_version=values.get("parsed_content_version"),
                fts_indexed_version=values.get("fts_indexed_version"),
                vector_indexed_version=values.get("vector_indexed_version"),
                error_code=values.get("error_code"),
                error_message=values.get("error_message"),
                created_at=str(values["created_at"]),
                updated_at=str(values["updated_at"]),
                completed_at=values.get("completed_at"),
            )
            session.add(item)
            session.flush()
            return _document_dict(item)

    def update_document(self, document_id: str, values: dict[str, Any]) -> None:
        with self.factory.session_scope(write=True) as session:
            item = session.get(Document, document_id)
            if item is None:
                raise KeyError(document_id)
            for field in (
                "status",
                "parsed_markdown_relpath",
                "parsed_content_version",
                "error_code",
                "error_message",
                "updated_at",
                "completed_at",
            ):
                if field in values:
                    setattr(item, field, values[field])

    def delete_document(
        self, knowledge_base_id: str, document_id: str, now: str
    ) -> bool:
        with self.factory.session_scope(write=True) as session:
            item = session.scalar(
                select(Document).where(
                    Document.id == document_id,
                    Document.knowledge_base_id == knowledge_base_id,
                )
            )
            if item is None:
                return False
            session.execute(
                delete(UploadOperation).where(
                    UploadOperation.knowledge_base_id == knowledge_base_id,
                    UploadOperation.document_id == document_id,
                )
            )
            session.delete(item)
            session.flush()
            knowledge_base = session.get(KnowledgeBase, knowledge_base_id)
            if knowledge_base is not None:
                remaining = int(
                    session.scalar(
                        select(func.count(Document.id)).where(
                            Document.knowledge_base_id == knowledge_base_id
                        )
                    )
                    or 0
                )
                knowledge_base.document_count = remaining
                knowledge_base.content_version += 1
                knowledge_base.fts_status = "pending"
                knowledge_base.fts_target_revision = knowledge_base.content_version
                if knowledge_base.vector_status != "disabled":
                    knowledge_base.vector_status = "pending"
                    knowledge_base.vector_target_revision = (
                        knowledge_base.content_version
                    )
                    knowledge_base.vector_progress_stage = "queued"
                    knowledge_base.vector_documents_total = remaining
                    knowledge_base.vector_documents_completed = 0
                    knowledge_base.vector_records_processed = 0
                knowledge_base.updated_at = now
            return True
