"""Durable answer-image blobs, message attachments and public grants."""

from __future__ import annotations

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
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class ImageStorageProfile(Base):
    __tablename__ = "image_storage_profiles"
    __table_args__ = (
        CheckConstraint(
            "backend IN ('filesystem', 's3')", name="ck_image_storage_profile_backend"
        ),
    )

    profile_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    backend: Mapped[str] = mapped_column(String(32), nullable=False)
    locator_json: Mapped[str] = mapped_column(Text, nullable=False)
    locator_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[str] = mapped_column(String(64), nullable=False)


class AnswerImageBlob(Base):
    __tablename__ = "answer_image_blobs"
    __table_args__ = (
        UniqueConstraint(
            "knowledge_base_id", "sha256", "object_key", name="uq_answer_image_blob_key"
        ),
        Index(
            "idx_answer_image_blobs_identity", "knowledge_base_id", "sha256", "status"
        ),
        Index(
            "uq_answer_image_blob_active",
            "knowledge_base_id",
            "sha256",
            unique=True,
            sqlite_where=text("status IN ('uploading', 'ready')"),
        ),
        CheckConstraint(
            "status IN ('uploading', 'ready', 'retired', 'deleting', 'deleted')",
            name="ck_answer_image_blob_status",
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    # Cleanup receipts must outlive the knowledge base and its source documents.
    knowledge_base_id: Mapped[str] = mapped_column(String, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    media_type: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    width: Mapped[int] = mapped_column(Integer, nullable=False)
    height: Mapped[int] = mapped_column(Integer, nullable=False)
    orientation: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    backend: Mapped[str] = mapped_column(String(32), nullable=False)
    storage_profile_id: Mapped[str] = mapped_column(String(128), nullable=False)
    object_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    version_id: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    lease_token: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_expires_at: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[str] = mapped_column(String(64), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(64), nullable=False)
    retired_at: Mapped[str | None] = mapped_column(String(64), nullable=True)


class MessageImage(Base):
    __tablename__ = "message_images"
    __table_args__ = (
        UniqueConstraint("message_id", "sequence", name="uq_message_image_sequence"),
        Index("idx_message_images_message", "message_id", "sequence"),
        CheckConstraint(
            "status IN ('available', 'unavailable', 'deleted')",
            name="ck_message_image_status",
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    message_id: Mapped[str] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"), nullable=False
    )
    blob_id: Mapped[str | None] = mapped_column(
        ForeignKey("answer_image_blobs.id", ondelete="SET NULL"), nullable=True
    )
    image_id: Mapped[str] = mapped_column(String(128), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    ref_ids_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    occurrences_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    source_bindings_json: Mapped[str] = mapped_column(
        Text, nullable=False, default="[]"
    )
    caption: Mapped[str] = mapped_column(Text, nullable=False, default="")
    display_in_answer: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    unavailable_reason: Mapped[str | None] = mapped_column(String(128), nullable=True)
    media_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    orientation: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[str] = mapped_column(String(64), nullable=False)


class ImageAccessGrant(Base):
    __tablename__ = "image_access_grants"
    __table_args__ = (
        Index("idx_image_access_grants_attachment", "attachment_id"),
        Index("idx_image_access_grants_expiry", "expires_at"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    attachment_id: Mapped[str] = mapped_column(
        ForeignKey("message_images.id", ondelete="CASCADE"), nullable=False
    )
    application_id: Mapped[str] = mapped_column(String, nullable=False)
    client_id: Mapped[str] = mapped_column(String, nullable=False)
    key_id: Mapped[str] = mapped_column(String, nullable=False)
    token_sha256: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    expires_at: Mapped[str] = mapped_column(String(64), nullable=False)
    revoked_at: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[str] = mapped_column(String(64), nullable=False)


class ImageBlobReadLease(Base):
    __tablename__ = "image_blob_read_leases"
    token: Mapped[str] = mapped_column(String(128), primary_key=True)
    blob_id: Mapped[str] = mapped_column(
        ForeignKey("answer_image_blobs.id"), nullable=False, index=True
    )
    locator_json: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[str] = mapped_column(String(64), nullable=False)
