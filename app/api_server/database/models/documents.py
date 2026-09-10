"""SQLAlchemy ORM models for the API server SQLite database."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship


from .base import Base

if TYPE_CHECKING:
    from .knowledge_bases import KnowledgeBase


class Document(Base):
    """One uploaded source document and its current ingestion state."""

    __tablename__ = "documents"
    __table_args__ = (
        Index(
            "idx_documents_knowledge_base_status_updated",
            "knowledge_base_id",
            "status",
            "updated_at",
        ),
        Index(
            "uq_documents_kb_source_hash",
            "knowledge_base_id",
            "source_sha256",
            unique=True,
            sqlite_where=text("status != 'deleted'"),
        ),
        CheckConstraint("size_bytes > 0", name="ck_documents_size_bytes"),
        CheckConstraint(
            "parser IN ('native_markdown', 'mineru')", name="ck_documents_parser"
        ),
        CheckConstraint(
            "status IN ('uploaded', 'parsing', 'parsed', 'indexing', 'ready', "
            "'failed', 'deleted')",
            name="ck_documents_status",
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    knowledge_base_id: Mapped[str] = mapped_column(
        ForeignKey("knowledge_bases.id", ondelete="CASCADE"), nullable=False
    )
    original_filename: Mapped[str] = mapped_column(String, nullable=False)
    file_extension: Mapped[str] = mapped_column(String(16), nullable=False)
    mime_type: Mapped[str] = mapped_column(
        String(256), nullable=False, default="application/octet-stream"
    )
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    source_relpath: Mapped[str] = mapped_column(String, nullable=False)
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    parser: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="uploaded")
    pipeline_generation: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )
    parse_plan_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    current_stage: Mapped[str] = mapped_column(
        String(16), nullable=False, default="complete", server_default="complete"
    )
    stage_state: Mapped[str] = mapped_column(
        String(16), nullable=False, default="succeeded", server_default="succeeded"
    )
    failed_stage: Mapped[str | None] = mapped_column(String(16), nullable=True)
    progress_completed: Mapped[int | None] = mapped_column(Integer, nullable=True)
    progress_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    progress_unit: Mapped[str | None] = mapped_column(String(16), nullable=True)
    warning_json: Mapped[str] = mapped_column(
        Text, nullable=False, default="[]", server_default="[]"
    )
    deleted_content_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    parsed_markdown_relpath: Mapped[str | None] = mapped_column(String, nullable=True)
    parsed_content_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fts_indexed_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    vector_indexed_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String(64), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(64), nullable=False)
    completed_at: Mapped[str | None] = mapped_column(String(64), nullable=True)

    knowledge_base: Mapped[KnowledgeBase] = relationship(back_populates="documents")
    parse_tasks: Mapped[list[DocumentParseTask]] = relationship(
        back_populates="document",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="DocumentParseTask.attempt",
    )
    artifacts: Mapped[list[DocumentArtifact]] = relationship(
        back_populates="document",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class DocumentParseTask(Base):
    """MinerU batch state; kept separate so retries retain history."""

    __tablename__ = "document_parse_tasks"
    __table_args__ = (
        Index("idx_document_parse_tasks_polling", "dispatch_state", "available_at"),
        Index("idx_document_parse_tasks_batch", "batch_id"),
        UniqueConstraint(
            "document_id",
            "pipeline_generation",
            "chunk_index",
            "attempt",
            name="uq_document_parse_attempt",
        ),
        UniqueConstraint(
            "provider",
            "data_id",
            "pipeline_generation",
            "chunk_index",
            "attempt",
            name="uq_document_parse_data_attempt",
        ),
        CheckConstraint("attempt > 0", name="ck_document_parse_attempt"),
        CheckConstraint(
            "state IN ('created', 'uploading', 'waiting-file', 'pending', "
            "'running', 'converting', 'done', 'failed', 'canceled')",
            name="ck_document_parse_state",
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    provider: Mapped[str] = mapped_column(String, nullable=False, default="mineru")
    api_mode: Mapped[str] = mapped_column(String, nullable=False, default="precision")
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    pipeline_generation: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )
    chunk_index: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    chunk_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )
    source_page_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_page_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    chunk_source_relpath: Mapped[str | None] = mapped_column(String, nullable=True)
    result_root_relpath: Mapped[str | None] = mapped_column(String, nullable=True)
    input_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    output_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    data_id: Mapped[str] = mapped_column(String, nullable=False)
    batch_id: Mapped[str | None] = mapped_column(String, nullable=True)
    task_id: Mapped[str | None] = mapped_column(String, nullable=True)
    model_version: Mapped[str] = mapped_column(String, nullable=False)
    request_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    state: Mapped[str] = mapped_column(String, nullable=False, default="created")
    dispatch_state: Mapped[str] = mapped_column(
        String(16), nullable=False, default="queued", server_default="queued"
    )
    available_at: Mapped[str] = mapped_column(
        String(64), nullable=False, default="", server_default=""
    )
    next_poll_at: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(String, nullable=True)
    lease_expires_at: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_token: Mapped[str | None] = mapped_column(String, nullable=True)
    extracted_pages: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_pages: Mapped[int | None] = mapped_column(Integer, nullable=True)
    result_zip_url: Mapped[str | None] = mapped_column(String, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String(64), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(64), nullable=False)
    started_at: Mapped[str | None] = mapped_column(String(64), nullable=True)
    completed_at: Mapped[str | None] = mapped_column(String(64), nullable=True)

    document: Mapped[Document] = relationship(back_populates="parse_tasks")


class DocumentArtifact(Base):
    """A durable file produced or retained by the ingestion pipeline."""

    __tablename__ = "document_artifacts"
    __table_args__ = (
        Index("idx_document_artifacts_document_kind", "document_id", "kind"),
        UniqueConstraint(
            "document_id", "kind", "relpath", name="uq_document_artifact_path"
        ),
        CheckConstraint(
            "kind IN ('original', 'result_zip', 'full_markdown', 'content_list', "
            "'layout', 'model', 'asset', 'parse_chunk_source', "
            "'parse_chunk_result', 'normalized_markdown', 'page_map', "
            "'enriched_markdown', 'tree', 'diagnostics')",
            name="ck_document_artifact_kind",
        ),
        CheckConstraint("size_bytes >= 0", name="ck_document_artifact_size"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String, nullable=False)
    relpath: Mapped[str] = mapped_column(String, nullable=False)
    mime_type: Mapped[str] = mapped_column(String(256), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    pipeline_generation: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )
    created_at: Mapped[str] = mapped_column(String(64), nullable=False)

    document: Mapped[Document] = relationship(back_populates="artifacts")
