"""SQLAlchemy ORM models for the API server SQLite database."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
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
    from .chat import Conversation
    from .documents import Document


class KnowledgeBase(Base):
    __tablename__ = "knowledge_bases"
    __table_args__ = (
        Index("idx_knowledge_bases_status_updated", "status", "updated_at"),
        CheckConstraint(
            "length(trim(name)) > 0", name="ck_knowledge_bases_name_not_blank"
        ),
        CheckConstraint(
            "status IN ('creating', 'ready', 'indexing', 'error')",
            name="ck_knowledge_bases_status",
        ),
        CheckConstraint(
            "document_count >= 0", name="ck_knowledge_bases_document_count"
        ),
        CheckConstraint(
            "content_version >= 0", name="ck_knowledge_bases_content_version"
        ),
        CheckConstraint(
            "fts_status IN ('disabled', 'pending', 'building', 'ready', 'failed')",
            name="ck_knowledge_bases_fts_status",
        ),
        CheckConstraint(
            "vector_status IN ('disabled', 'pending', 'building', 'ready', 'failed')",
            name="ck_knowledge_bases_vector_status",
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(String, nullable=False, default="ready")
    workspace_relpath: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    document_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    summary_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("1")
    )
    heading_recovery_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("1")
    )
    content_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    fts_status: Mapped[str] = mapped_column(String, nullable=False, default="disabled")
    fts_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fts_target_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fts_collection: Mapped[str | None] = mapped_column(String, nullable=True)
    fts_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    vector_status: Mapped[str] = mapped_column(
        String, nullable=False, default="disabled"
    )
    vector_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    vector_target_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    vector_collection: Mapped[str | None] = mapped_column(String, nullable=True)
    vector_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    vector_model_id: Mapped[str | None] = mapped_column(String, nullable=True)
    vector_model_updated_at: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    vector_dimension: Mapped[int | None] = mapped_column(Integer, nullable=True)
    vector_progress_stage: Mapped[str | None] = mapped_column(String(32), nullable=True)
    vector_documents_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    vector_documents_completed: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    vector_records_processed: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[str] = mapped_column(String(64), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(64), nullable=False)

    applications: Mapped[list[Application]] = relationship(
        back_populates="knowledge_base", passive_deletes=True
    )
    upload_operations: Mapped[list[UploadOperation]] = relationship(
        back_populates="knowledge_base",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    documents: Mapped[list[Document]] = relationship(
        back_populates="knowledge_base",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class Application(Base):
    __tablename__ = "applications"
    __table_args__ = (
        Index("idx_applications_knowledge_base_id", "knowledge_base_id"),
        CheckConstraint(
            "length(trim(name)) > 0", name="ck_applications_name_not_blank"
        ),
        CheckConstraint("search_mode = 'fts'", name="ck_applications_search_mode"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    knowledge_base_id: Mapped[str] = mapped_column(
        ForeignKey("knowledge_bases.id", ondelete="RESTRICT", onupdate="CASCADE"),
        nullable=False,
    )
    model: Mapped[str | None] = mapped_column(String, nullable=True)
    provider: Mapped[str] = mapped_column(String, nullable=False, default="default")
    search_mode: Mapped[str] = mapped_column(String, nullable=False, default="fts")
    config_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[str] = mapped_column(String(64), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(64), nullable=False)

    knowledge_base: Mapped[KnowledgeBase] = relationship(back_populates="applications")
    conversations: Mapped[list[Conversation]] = relationship(
        back_populates="application", passive_deletes=True
    )


class UploadOperation(Base):
    __tablename__ = "upload_operations"
    __table_args__ = (
        CheckConstraint(
            "status IN ('started', 'files_committed', 'committed', 'failed')",
            name="ck_upload_operations_status",
        ),
    )

    knowledge_base_id: Mapped[str] = mapped_column(
        ForeignKey("knowledge_bases.id", ondelete="CASCADE"), primary_key=True
    )
    idempotency_key: Mapped[str] = mapped_column(String, primary_key=True)
    request_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    document_id: Mapped[str] = mapped_column(String, nullable=False)
    source_relpath: Mapped[str | None] = mapped_column(String, nullable=True)
    artifact_relpath: Mapped[str | None] = mapped_column(String, nullable=True)
    source_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    artifact_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String(64), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(64), nullable=False)

    knowledge_base: Mapped[KnowledgeBase] = relationship(
        back_populates="upload_operations"
    )


class KnowledgeBaseWorkspaceRevision(Base):
    """Immutable V2 snapshot revision backing the knowledge-base visible view.

    SQLite points at exactly one ``committed`` revision per knowledge base; the
    referenced ``snapshots/r<N>/manifest.json`` is written and validated before
    the row commits (单向发布协议，设计文档 §11.2)。``superseded`` revisions stay
    readable for in-flight readers and index builders until GC.
    """

    __tablename__ = "knowledge_base_workspace_revisions"
    __table_args__ = (
        UniqueConstraint(
            "knowledge_base_id",
            "content_version",
            name="uq_kb_revision_content_version",
        ),
        Index(
            "idx_kb_revisions_kb_state",
            "knowledge_base_id",
            "state",
        ),
        Index(
            "uq_kb_revisions_current",
            "knowledge_base_id",
            unique=True,
            sqlite_where=text("state = 'committed'"),
        ),
        CheckConstraint(
            "state IN ('committed', 'superseded')",
            name="ck_kb_revisions_state",
        ),
        CheckConstraint(
            "content_version >= 0",
            name="ck_kb_revisions_content_version",
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    knowledge_base_id: Mapped[str] = mapped_column(
        ForeignKey("knowledge_bases.id", ondelete="CASCADE"), nullable=False
    )
    content_version: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot_relpath: Mapped[str] = mapped_column(String, nullable=False)
    manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String, nullable=False, default="committed")
    created_at: Mapped[str] = mapped_column(String(64), nullable=False)
