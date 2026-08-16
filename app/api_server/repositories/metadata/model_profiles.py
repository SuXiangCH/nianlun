"""Model-profile persistence operations for the metadata repository."""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api_server.database.connection import SQLiteConnectionFactory
from app.api_server.database.models import (
    Application,
    EmbeddingModelProfile,
    KnowledgeBase,
    LLMModelProfile,
    ModelProfile,
    ParserModelProfile,
)


def _model_profile_dict(item: ModelProfile) -> dict[str, Any]:
    llm = item.llm_config
    embedding = item.embedding_config
    parser = item.parser_config
    return {
        "id": item.id,
        "kind": item.kind,
        "name": item.name,
        "base_url": item.base_url,
        "api_key": item.api_key,
        "model": llm.model
        if llm is not None
        else embedding.model
        if embedding is not None
        else None,
        "context_window_tokens": llm.context_window_tokens if llm is not None else None,
        "dimension": embedding.dimension if embedding is not None else None,
        "enabled": bool(embedding.enabled) if embedding is not None else False,
        "api_mode": parser.api_mode if parser is not None else "saas_precision",
        "model_version": parser.model_version if parser is not None else "vlm",
        "language": parser.language if parser is not None else "ch",
        "is_ocr": bool(parser.is_ocr) if parser is not None else False,
        "enable_table": bool(parser.enable_table) if parser is not None else True,
        "enable_formula": bool(parser.enable_formula) if parser is not None else True,
        "page_ranges": parser.page_ranges if parser is not None else "",
        "is_default": bool(item.is_default),
        "created_at": item.created_at,
        "updated_at": item.updated_at,
    }


class ModelProfileRepositoryMixin:
    """Operations backed by ``model_profiles`` and its type-specific tables."""

    factory: SQLiteConnectionFactory

    def list_model_profiles(self, kind: str | None = None) -> list[dict[str, Any]]:
        with self.factory.session_scope() as session:
            statement = select(ModelProfile).order_by(
                ModelProfile.kind,
                ModelProfile.is_default.desc(),
                ModelProfile.created_at,
            )
            if kind is not None:
                statement = statement.where(ModelProfile.kind == kind)
            return [
                _model_profile_dict(item) for item in session.scalars(statement).all()
            ]

    def get_model_profile(self, profile_id: str) -> dict[str, Any] | None:
        with self.factory.session_scope() as session:
            item = session.get(ModelProfile, profile_id)
            return _model_profile_dict(item) if item is not None else None

    def get_default_model_profile(self, kind: str) -> dict[str, Any] | None:
        with self.factory.session_scope() as session:
            item = session.scalars(
                select(ModelProfile)
                .where(ModelProfile.kind == kind, ModelProfile.is_default.is_(True))
                .limit(1)
            ).first()
            return _model_profile_dict(item) if item is not None else None

    def count_knowledge_bases_for_vector_model(self, profile_id: str) -> int:
        with self.factory.session_scope() as session:
            count = session.scalar(
                select(func.count(KnowledgeBase.id)).where(
                    KnowledgeBase.vector_model_id == profile_id
                )
            )
            return int(count or 0)

    def count_applications_for_llm_model(self, profile_id: str) -> int:
        with self.factory.session_scope() as session:
            count = session.scalar(
                select(func.count(Application.id)).where(
                    Application.model == profile_id
                )
            )
            return int(count or 0)

    def has_default_model_profile(self, kind: str) -> bool:
        return self.get_default_model_profile(kind) is not None

    @staticmethod
    def _clear_default_profiles(session: Session, kind: str) -> None:
        for item in session.scalars(
            select(ModelProfile).where(ModelProfile.kind == kind)
        ).all():
            item.is_default = False

    @staticmethod
    def _add_profile_config(session: Session, config: dict[str, Any]) -> None:
        profile_id = str(config["id"])
        kind = str(config["kind"])
        if kind == "llm":
            session.add(
                LLMModelProfile(
                    profile_id=profile_id,
                    model=config.get("model"),
                    context_window_tokens=config.get("context_window_tokens"),
                )
            )
        elif kind == "embedding":
            session.add(
                EmbeddingModelProfile(
                    profile_id=profile_id,
                    model=config.get("model"),
                    dimension=config.get("dimension"),
                    enabled=bool(config.get("enabled", False)),
                )
            )
        else:
            session.add(
                ParserModelProfile(
                    profile_id=profile_id,
                    api_mode=config.get("api_mode", "saas_precision"),
                    model_version=config.get("model_version", "vlm"),
                    language=config.get("language", "ch"),
                    is_ocr=bool(config.get("is_ocr", False)),
                    enable_table=bool(config.get("enable_table", True)),
                    enable_formula=bool(config.get("enable_formula", True)),
                    page_ranges=config.get("page_ranges", ""),
                )
            )

    @staticmethod
    def _update_profile_config(session: Session, config: dict[str, Any]) -> None:
        profile_id = str(config["id"])
        kind = str(config["kind"])
        if kind == "llm":
            item = session.get(LLMModelProfile, profile_id) or LLMModelProfile(
                profile_id=profile_id
            )
            session.add(item)
            item.model = config.get("model")
            item.context_window_tokens = config.get("context_window_tokens")
        elif kind == "embedding":
            item = session.get(
                EmbeddingModelProfile, profile_id
            ) or EmbeddingModelProfile(profile_id=profile_id)
            session.add(item)
            item.model = config.get("model")
            item.dimension = config.get("dimension")
            item.enabled = bool(config.get("enabled", False))
        else:
            item = session.get(ParserModelProfile, profile_id) or ParserModelProfile(
                profile_id=profile_id
            )
            session.add(item)
            item.api_mode = config.get("api_mode", "saas_precision")
            item.model_version = config.get("model_version", "vlm")
            item.language = config.get("language", "ch")
            item.is_ocr = bool(config.get("is_ocr", False))
            item.enable_table = bool(config.get("enable_table", True))
            item.enable_formula = bool(config.get("enable_formula", True))
            item.page_ranges = config.get("page_ranges", "")

    def create_model_profile(self, config: dict[str, Any], now: str) -> dict[str, Any]:
        with self.factory.session_scope(write=True) as session:
            kind = str(config["kind"])
            has_default = (
                session.scalars(
                    select(ModelProfile).where(
                        ModelProfile.kind == kind, ModelProfile.is_default.is_(True)
                    )
                ).first()
                is not None
            )
            if config.get("is_default") or not has_default:
                self._clear_default_profiles(session, kind)
                config["is_default"] = True
            item = ModelProfile(
                id=str(config["id"]),
                kind=kind,
                name=str(config["name"]),
                base_url=config.get("base_url"),
                api_key=config.get("api_key"),
                is_default=bool(config.get("is_default", False)),
                created_at=now,
                updated_at=now,
            )
            session.add(item)
            self._add_profile_config(session, config)
            session.flush()
            return _model_profile_dict(item)

    def update_model_profile(self, config: dict[str, Any], now: str) -> dict[str, Any]:
        with self.factory.session_scope(write=True) as session:
            item = session.get(ModelProfile, str(config["id"]))
            if item is None:
                raise KeyError(config["id"])
            if item.kind != config["kind"]:
                raise ValueError("模型类型不可修改")
            if config.get("is_default"):
                self._clear_default_profiles(session, item.kind)
            item.name = str(config["name"])
            item.base_url = config.get("base_url")
            item.api_key = config.get("api_key")
            item.is_default = bool(config.get("is_default", False))
            item.updated_at = now
            self._update_profile_config(session, config)
            session.flush()
            return _model_profile_dict(item)

    def set_default_model_profile(self, profile_id: str, now: str) -> dict[str, Any]:
        with self.factory.session_scope(write=True) as session:
            item = session.get(ModelProfile, profile_id)
            if item is None:
                raise KeyError(profile_id)
            self._clear_default_profiles(session, item.kind)
            item.is_default = True
            item.updated_at = now
            session.flush()
            return _model_profile_dict(item)

    def delete_model_profile(self, profile_id: str) -> None:
        with self.factory.session_scope(write=True) as session:
            item = session.get(ModelProfile, profile_id)
            if item is None:
                raise KeyError(profile_id)
            session.delete(item)
