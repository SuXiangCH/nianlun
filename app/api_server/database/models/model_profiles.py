"""SQLAlchemy ORM models for the API server SQLite database."""

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
)
from sqlalchemy.orm import Mapped, mapped_column, relationship


from .base import Base


class ModelProfile(Base):
    """Common metadata for one user-managed model catalog entry."""

    __tablename__ = "model_profiles"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('llm', 'embedding', 'parser')",
            name="ck_model_profiles_kind",
        ),
        CheckConstraint("is_default IN (0, 1)", name="ck_model_profiles_is_default"),
        UniqueConstraint("kind", "name", name="uq_model_profiles_kind_name"),
        Index("idx_model_profiles_kind_default", "kind", "is_default"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    base_url: Mapped[str | None] = mapped_column(String, nullable=True)
    api_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[str] = mapped_column(String(64), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(64), nullable=False)

    llm_config: Mapped[LLMModelProfile | None] = relationship(
        back_populates="profile",
        uselist=False,
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    embedding_config: Mapped[EmbeddingModelProfile | None] = relationship(
        back_populates="profile",
        uselist=False,
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    parser_config: Mapped[ParserModelProfile | None] = relationship(
        back_populates="profile",
        uselist=False,
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class LLMModelProfile(Base):
    __tablename__ = "llm_model_profiles"

    profile_id: Mapped[str] = mapped_column(
        ForeignKey("model_profiles.id", ondelete="CASCADE"), primary_key=True
    )
    model: Mapped[str | None] = mapped_column(String, nullable=True)
    context_window_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)

    profile: Mapped[ModelProfile] = relationship(back_populates="llm_config")


class EmbeddingModelProfile(Base):
    __tablename__ = "embedding_model_profiles"
    __table_args__ = (
        CheckConstraint(
            "dimension IS NULL OR dimension > 0",
            name="ck_embedding_model_profiles_dimension",
        ),
        CheckConstraint(
            "enabled IN (0, 1)", name="ck_embedding_model_profiles_enabled"
        ),
    )

    profile_id: Mapped[str] = mapped_column(
        ForeignKey("model_profiles.id", ondelete="CASCADE"), primary_key=True
    )
    model: Mapped[str | None] = mapped_column(String, nullable=True)
    dimension: Mapped[int | None] = mapped_column(Integer, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    profile: Mapped[ModelProfile] = relationship(back_populates="embedding_config")


class ParserModelProfile(Base):
    __tablename__ = "parser_model_profiles"
    __table_args__ = (
        CheckConstraint(
            "model_version IN ('pipeline', 'vlm')",
            name="ck_parser_model_profiles_model_version",
        ),
        CheckConstraint("is_ocr IN (0, 1)", name="ck_parser_model_profiles_is_ocr"),
        CheckConstraint(
            "enable_table IN (0, 1)",
            name="ck_parser_model_profiles_enable_table",
        ),
        CheckConstraint(
            "enable_formula IN (0, 1)",
            name="ck_parser_model_profiles_enable_formula",
        ),
    )

    profile_id: Mapped[str] = mapped_column(
        ForeignKey("model_profiles.id", ondelete="CASCADE"), primary_key=True
    )
    model_version: Mapped[str] = mapped_column(String, nullable=False, default="vlm")
    api_mode: Mapped[str] = mapped_column(
        String, nullable=False, default="saas_precision"
    )
    language: Mapped[str] = mapped_column(String, nullable=False, default="ch")
    is_ocr: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    enable_table: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    enable_formula: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    page_ranges: Mapped[str] = mapped_column(Text, nullable=False, default="")

    profile: Mapped[ModelProfile] = relationship(back_populates="parser_config")
