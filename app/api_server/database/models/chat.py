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
    from .knowledge_bases import Application


class Conversation(Base):
    __tablename__ = "conversations"
    __table_args__ = (
        Index(
            "idx_conversations_application_updated",
            "application_id",
            text("updated_at DESC"),
        ),
        CheckConstraint(
            "status IN ('active', 'archived', 'deleted')",
            name="ck_conversations_status",
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    application_id: Mapped[str] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(String, nullable=False, default="active")
    created_at: Mapped[str] = mapped_column(String(64), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(64), nullable=False)
    last_message_at: Mapped[str | None] = mapped_column(String(64), nullable=True)

    application: Mapped[Application] = relationship(back_populates="conversations")
    messages: Mapped[list[Message]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="Message.seq_no",
    )


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        Index("idx_messages_conversation_seq", "conversation_id", "seq_no"),
        UniqueConstraint(
            "conversation_id", "seq_no", name="uq_messages_conversation_seq"
        ),
        UniqueConstraint(
            "conversation_id",
            "idempotency_key",
            name="uq_messages_conversation_idempotency",
        ),
        CheckConstraint("role IN ('user', 'assistant')", name="ck_messages_role"),
        CheckConstraint(
            "status IN ('pending', 'completed', 'failed')", name="ck_messages_status"
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    seq_no: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(String, nullable=False, default="completed")
    route: Mapped[str | None] = mapped_column(String, nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    tool_calls_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    usage_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    ttft_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[str] = mapped_column(String(64), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(64), nullable=False)

    conversation: Mapped[Conversation] = relationship(back_populates="messages")
    sources: Mapped[list[MessageSource]] = relationship(
        back_populates="message",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="MessageSource.source_order",
    )


class MessageSource(Base):
    __tablename__ = "message_sources"
    __table_args__ = (
        Index("idx_message_sources_message_order", "message_id", "source_order"),
        UniqueConstraint(
            "message_id", "source_order", name="uq_message_sources_message_order"
        ),
        CheckConstraint(
            "text_truncated IN (0, 1)", name="ck_message_sources_text_truncated"
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    message_id: Mapped[str] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"), nullable=False
    )
    source_order: Mapped[int] = mapped_column(Integer, nullable=False)
    doc_id: Mapped[str] = mapped_column(String, nullable=False)
    node_id: Mapped[str | None] = mapped_column(String, nullable=True)
    line_spec: Mapped[str | None] = mapped_column(String, nullable=True)
    line_num: Mapped[int | None] = mapped_column(Integer, nullable=True)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    retrieved_text: Mapped[str] = mapped_column(Text, nullable=False)
    char_offset: Mapped[int | None] = mapped_column(Integer, nullable=True)
    char_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_chars: Mapped[int | None] = mapped_column(Integer, nullable=True)
    text_truncated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    content_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    metadata_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[str] = mapped_column(String(64), nullable=False)

    message: Mapped[Message] = relationship(back_populates="sources")
