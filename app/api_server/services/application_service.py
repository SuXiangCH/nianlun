"""Application configuration and lazy AgentRuntime management."""

from __future__ import annotations

import logging
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from fastapi import status

from nianlun.agent.contracts import AGENT_TOOL_SCHEMA_VERSION
from nianlun.agent.lead_agent.prompt import PROMPT_VERSION
from nianlun.agent.lead_agent.runtime import AgentRuntime
from nianlun.agent.lead_agent.factory import AgentRuntimeFactory
from app.api_server.config import ProviderConfig
from app.api_server.apis.v1.schemas import (
    ApplicationCreateRequest,
    ApplicationResponse,
)
from app.api_server.common.errors import ApiError
from app.api_server.repositories import SQLiteMetadataRepository
from app.api_server.services.model_config_service import ModelConfigService
from nianlun.knowledgebase import KnowledgeBaseConfig


logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


RuntimeFactory = Callable[..., AgentRuntime]
ProviderResolver = Callable[[str], ProviderConfig]
RuntimeCapabilityFingerprint = tuple[Any, ...]


def _default_runtime_factory(**kwargs: Any) -> AgentRuntime:
    return AgentRuntimeFactory(**kwargs).create()


def _default_provider_resolver(provider: str) -> ProviderConfig:
    if provider == "default":
        return ProviderConfig()
    raise ValueError(f"provider 未配置: {provider}")


class ApplicationService:
    """Store app definitions and cache one isolated runtime per app.

    ``MemorySaver`` lives inside each runtime, so the cache is also the process
    boundary for conversation history in this MVP. A durable checkpointer is a
    planned replacement before horizontal scaling.
    """

    def __init__(
        self,
        repository: SQLiteMetadataRepository,
        knowledge_base_lookup: Callable[[str], dict[str, Any]],
        runtime_factory: RuntimeFactory = _default_runtime_factory,
        provider_resolver: ProviderResolver = _default_provider_resolver,
        *,
        fts_enabled: bool = True,
        milvus_uri: str | None = None,
        milvus_token: str | None = None,
        vector_enabled: bool = False,
        vector_collection: str | None = None,
        embedding_model: str | None = None,
        embedding_dim: int | None = None,
        model_config_service: ModelConfigService | None = None,
    ) -> None:
        self.repository = repository
        self.knowledge_base_lookup = knowledge_base_lookup
        self.runtime_factory = runtime_factory
        self.provider_resolver = provider_resolver
        self.fts_enabled = fts_enabled
        self.milvus_uri = milvus_uri
        self.milvus_token = milvus_token
        self.vector_enabled = vector_enabled
        self.vector_collection = vector_collection
        self.embedding_model = embedding_model
        self.embedding_dim = embedding_dim
        self.model_config_service = model_config_service
        self._runtimes: dict[
            str, tuple[RuntimeCapabilityFingerprint, AgentRuntime]
        ] = {}
        self._lock = threading.RLock()

    @staticmethod
    def _response(item: dict[str, Any]) -> ApplicationResponse:
        fields = ApplicationResponse.model_fields
        payload = {key: item[key] for key in fields if key in item}
        payload["llm_model_id"] = item.get("model")
        return ApplicationResponse.model_validate(payload)

    def list(self) -> list[ApplicationResponse]:
        return [self._response(item) for item in self.repository.list("applications")]

    def get(self, application_id: str) -> ApplicationResponse:
        item = self.repository.get("applications", application_id)
        if item is None:
            raise ApiError("应用不存在", status.HTTP_404_NOT_FOUND)
        return self._response(item)

    def require_record(self, application_id: str) -> dict[str, Any]:
        item = self.repository.get("applications", application_id)
        if item is None:
            raise ApiError("应用不存在", status.HTTP_404_NOT_FOUND)
        return item

    def create(self, request: ApplicationCreateRequest) -> ApplicationResponse:
        knowledge_base = self.knowledge_base_lookup(request.knowledge_base_id)
        if knowledge_base.get("status") != "ready":
            raise ApiError("知识库当前不可绑定", status.HTTP_409_CONFLICT)
        if not self.fts_enabled:
            raise ApiError("API Server 未启用 FTS", status.HTTP_503_SERVICE_UNAVAILABLE)
        content_version = int(knowledge_base.get("content_version", 0))
        fts_revision = knowledge_base.get("fts_revision")
        if (
            knowledge_base.get("fts_status") != "ready"
            or fts_revision is None
            or int(fts_revision) != content_version
            or not knowledge_base.get("fts_collection")
        ):
            raise ApiError(
                "知识库 FTS 索引尚未就绪，请先等待或触发索引构建",
                status.HTTP_409_CONFLICT,
            )
        try:
            self.provider_resolver(request.provider)
        except ValueError as exc:
            raise ApiError(str(exc), status.HTTP_422_UNPROCESSABLE_ENTITY) from exc
        selected_llm_model_id: str | None = None
        if self.model_config_service is not None:
            selected_llm_model_id = str(
                self.model_config_service.require_llm_profile(request.llm_model_id)[
                    "id"
                ]
            )
        elif request.llm_model_id is not None:
            raise ApiError(
                "API Server 未启用模型配置服务",
                status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        application_id = str(uuid.uuid4())
        timestamp = _now()
        item = {
            "id": application_id,
            "name": request.name,
            "description": request.description,
            "knowledge_base_id": request.knowledge_base_id,
            # The existing column stores a model-profile ID. Keeping the column
            # avoids a pre-release migration while the API exposes clear naming.
            "model": selected_llm_model_id,
            "provider": request.provider,
            "search_mode": "fts",
            "config_version": 1,
            "created_at": timestamp.isoformat(),
            "updated_at": timestamp.isoformat(),
        }
        self.repository.put("applications", application_id, item)
        return self._response(item)

    def delete(self, application_id: str) -> None:
        """Hard-delete an application and its persisted conversations."""
        with self._lock:
            if not self.repository.delete_application(application_id):
                raise ApiError("应用不存在", status.HTTP_404_NOT_FOUND)
            self._runtimes.pop(application_id, None)

    def runtime(self, application_id: str) -> AgentRuntime:
        item = self.require_record(application_id)
        knowledge_base = self.knowledge_base_lookup(item["knowledge_base_id"])
        content_version = int(knowledge_base.get("content_version", 0))
        fingerprint = self._runtime_capability_fingerprint(item, knowledge_base)
        with self._lock:
            cached = self._runtimes.get(application_id)
            if cached is not None and cached[0] == fingerprint:
                logger.info(
                    "agent.runtime_reused application_id=%s knowledge_base_id=%s "
                    "content_version=%s model=%s base_url=%s",
                    application_id,
                    item["knowledge_base_id"],
                    content_version,
                    getattr(cached[1], "model", "unknown"),
                    getattr(cached[1], "effective_url", "unknown"),
                )
                return cached[1]

            embedding_model: str | None = None
            embedding_dim: int | None = None
            embedding_base_url: str | None = None
            embedding_api_key: str | None = None
            if self.model_config_service is not None:
                (
                    effective_model,
                    provider,
                    _,
                    _,
                    _,
                    context_window_tokens,
                ) = self.model_config_service.runtime_config(
                    str(item.get("provider", "default")), item.get("model")
                )
                try:
                    selected_model_id = knowledge_base.get("embedding_model_id")
                    embedding = (
                        self.model_config_service.embedding_runtime_config(
                            str(selected_model_id), strict=False
                        )
                        if selected_model_id
                        else {"enabled": False}
                    )
                except ApiError:
                    # A broken optional embedding profile must not block chat;
                    # the vector index task exposes the configuration error.
                    embedding = {"enabled": False}
                vector_enabled = self._vector_index_ready(knowledge_base, embedding)
                embedding_model = (
                    embedding.get("model")
                    if isinstance(embedding.get("model"), str)
                    else None
                )
                embedding_dim = (
                    int(embedding["dimension"])
                    if isinstance(embedding.get("dimension"), int)
                    else None
                )
                embedding_base_url = (
                    embedding.get("base_url")
                    if isinstance(embedding.get("base_url"), str)
                    else None
                )
                embedding_api_key = (
                    embedding.get("api_key")
                    if isinstance(embedding.get("api_key"), str)
                    else None
                )
            else:
                effective_model = item["model"]
                try:
                    provider = self.provider_resolver(item.get("provider", "default"))
                except ValueError as exc:
                    raise ApiError(
                        str(exc), status.HTTP_422_UNPROCESSABLE_ENTITY
                    ) from exc
                vector_enabled = self.vector_enabled
                embedding_model = self.embedding_model
                embedding_dim = self.embedding_dim
                embedding_base_url = None
                embedding_api_key = None
                context_window_tokens = None

            config = KnowledgeBaseConfig(
                workspace_dir=Path(knowledge_base["workspace_dir"]),
                fts_enabled=True,
                milvus_uri=self.milvus_uri,
                milvus_token=self.milvus_token,
                fts_collection=knowledge_base.get("fts_collection"),
                knowledge_base_id=item["knowledge_base_id"],
                vector_enabled=vector_enabled,
                vector_collection=knowledge_base.get("vector_collection")
                or self.vector_collection,
                embedding_model=embedding_model,
                embedding_dim=embedding_dim,
                embedding_base_url=embedding_base_url,
                embedding_api_key=embedding_api_key,
            )
            runtime = self.runtime_factory(
                knowledge_base_config=config,
                model=effective_model or provider.model,
                base_url=provider.base_url,
                api_key=provider.api_key,
                context_window_tokens=context_window_tokens,
                allow_env_fallback=False,
                tool_logging=False,
            )
            runtime_kb = getattr(runtime, "kb", None)
            if vector_enabled and runtime_kb is not None and not runtime_kb.has_vector:
                logger.warning(
                    "agent.runtime_not_cached application_id=%s reason=vector_unavailable",
                    application_id,
                )
            else:
                self._runtimes[application_id] = (fingerprint, runtime)
            logger.info(
                "agent.runtime_created application_id=%s knowledge_base_id=%s "
                "content_version=%s model=%s base_url=%s vector_enabled=%s",
                application_id,
                item["knowledge_base_id"],
                content_version,
                getattr(runtime, "model", "unknown"),
                getattr(runtime, "effective_url", "unknown"),
                vector_enabled,
            )
            return runtime

    @staticmethod
    def _runtime_capability_fingerprint(
        application: dict[str, Any], knowledge_base: dict[str, Any]
    ) -> RuntimeCapabilityFingerprint:
        """标识会改变 graph、工具 schema 或知识库绑定的应用能力。"""
        return (
            AGENT_TOOL_SCHEMA_VERSION,
            PROMPT_VERSION,
            application.get("knowledge_base_id"),
            application.get("provider"),
            application.get("model"),
            application.get("config_version"),
            knowledge_base.get("content_version"),
            knowledge_base.get("fts_status"),
            knowledge_base.get("fts_revision"),
            knowledge_base.get("fts_collection"),
            knowledge_base.get("vector_status"),
            knowledge_base.get("vector_revision"),
            knowledge_base.get("vector_collection"),
            knowledge_base.get("vector_model_id"),
            knowledge_base.get("vector_model_updated_at"),
            knowledge_base.get("vector_dimension"),
        )

    @staticmethod
    def _vector_index_ready(
        knowledge_base: dict[str, Any], embedding: dict[str, Any]
    ) -> bool:
        return bool(
            embedding.get("model")
            and embedding.get("base_url")
            and embedding.get("api_key")
            and knowledge_base.get("vector_status") == "ready"
            and knowledge_base.get("vector_revision") is not None
            and int(knowledge_base["vector_revision"])
            == int(knowledge_base.get("content_version", 0))
            and knowledge_base.get("vector_model_id") == embedding.get("profile_id")
            and knowledge_base.get("vector_model_updated_at")
            == embedding.get("profile_updated_at")
            and int(knowledge_base.get("vector_dimension") or 0)
            == int(embedding.get("dimension") or 0)
            and bool(knowledge_base.get("vector_collection"))
        )

    def invalidate_runtime(self, application_id: str) -> None:
        with self._lock:
            self._runtimes.pop(application_id, None)

    def invalidate_all(self) -> None:
        """Drop cached runtimes after workspace model settings change."""
        with self._lock:
            self._runtimes.clear()


__all__ = [
    "ApplicationService",
    "RuntimeCapabilityFingerprint",
    "RuntimeFactory",
]
