"""Durable document-pipeline tasks, events, and staging reservations."""

from __future__ import annotations

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
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class DocumentNormalizationTask(Base):
    __tablename__ = "document_normalization_tasks"
    __table_args__ = (
        UniqueConstraint(
            "document_id",
            "pipeline_generation",
            "attempt",
            name="uq_document_normalization_attempt",
        ),
        Index(
            "uq_document_normalization_active",
            "document_id",
            "pipeline_generation",
            unique=True,
            sqlite_where=text("state IN ('queued', 'running', 'succeeded')"),
        ),
        Index("idx_document_normalization_dispatch", "state", "available_at"),
        CheckConstraint(
            "state IN ('queued', 'running', 'succeeded', 'failed', 'canceled')",
            name="ck_document_normalization_state",
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    pipeline_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    available_at: Mapped[str] = mapped_column(String(64), nullable=False)
    input_manifest_json: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_markdown_relpath: Mapped[str | None] = mapped_column(
        String, nullable=True
    )
    page_map_relpath: Mapped[str | None] = mapped_column(String, nullable=True)
    output_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(String, nullable=True)
    lease_expires_at: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_token: Mapped[str | None] = mapped_column(String, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String(64), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(64), nullable=False)
    started_at: Mapped[str | None] = mapped_column(String(64), nullable=True)
    completed_at: Mapped[str | None] = mapped_column(String(64), nullable=True)


class DocumentEnrichmentTask(Base):
    __tablename__ = "document_enrichment_tasks"
    __table_args__ = (
        UniqueConstraint(
            "document_id",
            "pipeline_generation",
            "attempt",
            name="uq_document_enrichment_attempt",
        ),
        Index(
            "uq_document_enrichment_active",
            "document_id",
            "pipeline_generation",
            unique=True,
            sqlite_where=text("state IN ('queued', 'running', 'succeeded', 'partial')"),
        ),
        Index("idx_document_enrichment_dispatch", "state", "available_at"),
        CheckConstraint(
            "state IN ('queued', 'running', 'succeeded', 'partial', 'failed', 'canceled')",
            name="ck_document_enrichment_state",
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    pipeline_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    available_at: Mapped[str] = mapped_column(String(64), nullable=False)
    input_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    llm_profile_id: Mapped[str | None] = mapped_column(String, nullable=True)
    llm_profile_updated_at: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    prompt_schema_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1
    )
    options_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    node_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    nodes_completed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    nodes_failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_markdown_relpath: Mapped[str | None] = mapped_column(String, nullable=True)
    output_tree_relpath: Mapped[str | None] = mapped_column(String, nullable=True)
    output_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(String, nullable=True)
    lease_expires_at: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_token: Mapped[str | None] = mapped_column(String, nullable=True)
    warning_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    error_code: Mapped[str | None] = mapped_column(String, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String(64), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(64), nullable=False)
    started_at: Mapped[str | None] = mapped_column(String(64), nullable=True)
    completed_at: Mapped[str | None] = mapped_column(String(64), nullable=True)


class DocumentEnrichmentCallResult(Base):
    __tablename__ = "document_enrichment_call_results"
    __table_args__ = (
        UniqueConstraint(
            "document_id",
            "pipeline_generation",
            "purpose",
            "unit_key",
            "input_sha256",
            "model_fingerprint",
            "prompt_schema_version",
            name="uq_document_enrichment_call_result",
        ),
        CheckConstraint(
            "purpose IN ('heading_recovery', 'node_summary', 'doc_description')",
            name="ck_document_enrichment_call_purpose",
        ),
        CheckConstraint(
            "state IN ('succeeded', 'failed')",
            name="ck_document_enrichment_call_state",
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    pipeline_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    purpose: Mapped[str] = mapped_column(String(32), nullable=False)
    unit_key: Mapped[str] = mapped_column(String, nullable=False)
    input_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    model_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    output_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String(64), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(64), nullable=False)


class DocumentPipelineEvent(Base):
    __tablename__ = "document_pipeline_events"
    __table_args__ = (
        Index(
            "idx_document_pipeline_events_document",
            "document_id",
            "pipeline_generation",
            "created_at",
        ),
        CheckConstraint(
            "stage IN ('parse', 'normalize', 'enrich', 'publish', 'index', 'complete')",
            name="ck_document_pipeline_event_stage",
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    pipeline_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    stage: Mapped[str] = mapped_column(String(16), nullable=False)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    progress_completed: Mapped[int | None] = mapped_column(Integer, nullable=True)
    progress_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String(64), nullable=False)


class DocumentStagingAllocation(Base):
    __tablename__ = "document_staging_allocations"
    __table_args__ = (
        UniqueConstraint(
            "document_id",
            "pipeline_generation",
            "allocation_key",
            name="uq_document_staging_allocation",
        ),
        CheckConstraint("bytes_reserved >= 0", name="ck_staging_allocation_bytes"),
        CheckConstraint(
            "kind IN ('chunk_pdf', 'result_zip', 'extracted', 'temp_output')",
            name="ck_staging_allocation_kind",
        ),
        CheckConstraint(
            "state IN ('active', 'released')",
            name="ck_staging_allocation_state",
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    pipeline_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    allocation_key: Mapped[str] = mapped_column(String, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    bytes_reserved: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    updated_at: Mapped[str] = mapped_column(String(64), nullable=False)


__all__ = [
    "DocumentEnrichmentCallResult",
    "DocumentEnrichmentTask",
    "DocumentNormalizationTask",
    "DocumentPipelineEvent",
    "DocumentStagingAllocation",
]
