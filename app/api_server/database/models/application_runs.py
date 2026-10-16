"""Durable public invocation state, separate from Agent checkpoints."""

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


class ApplicationRun(Base):
    __tablename__ = "application_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('running', 'cancelling', 'completed', 'failed', 'cancelled', 'timed_out', 'interrupted')",
            name="ck_application_runs_status",
        ),
        UniqueConstraint(
            "application_id",
            "client_id",
            "user_id",
            "idempotency_key",
            name="uq_application_run_idempotency",
        ),
        Index(
            "uq_application_run_active_conversation",
            "conversation_id",
            unique=True,
            sqlite_where=text("status IN ('running', 'cancelling')"),
        ),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True)
    application_id: Mapped[str] = mapped_column(
        ForeignKey("applications.id"), nullable=False
    )
    client_id: Mapped[str] = mapped_column(String, nullable=False)
    user_id: Mapped[str] = mapped_column(String, nullable=False)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.id"), nullable=False
    )
    release_id: Mapped[str] = mapped_column(String, nullable=False)
    message_id: Mapped[str] = mapped_column(String, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String, nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    error_type: Mapped[str | None] = mapped_column(String)
    error_message: Mapped[str | None] = mapped_column(Text)
    error_status: Mapped[int | None] = mapped_column(Integer)
    knowledge_base_content_version: Mapped[int] = mapped_column(Integer, nullable=False)
    result_json: Mapped[str | None] = mapped_column(Text)
    request_id: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)
    finished_at: Mapped[str | None] = mapped_column(String)
