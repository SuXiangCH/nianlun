"""Model catalog and connectivity tests for the single-user API server."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import status

from app.api_server.apis.v1.schemas import (
    ModelConfigTestRequest,
    ModelConfigTestResponse,
    ModelProfileRequest,
    ModelProfileResponse,
)
from app.api_server.common.errors import ApiError
from app.api_server.config import ProviderConfig
from app.api_server.repositories import SQLiteMetadataRepository
from nianlun.models.embedding import build_embeddings_model
from nianlun.models.llm import build_chat_model


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ModelConfigService:
    """Read and persist the single-user model catalog."""

    def __init__(
        self,
        repository: SQLiteMetadataRepository,
    ) -> None:
        self.repository = repository

    def _test_endpoint_values(
        self,
        request: ModelConfigTestRequest,
        defaults: tuple[str | None, str | None, str | None, int | None] | None = None,
    ) -> tuple[str | None, str | None, str | None, int | None]:
        """Resolve an optional saved profile and overlay draft values."""
        if defaults is None:
            default_model = default_base_url = default_api_key = None
            default_dimension = None
        else:
            default_model, default_base_url, default_api_key, default_dimension = (
                defaults
            )
        return (
            request.model if "model" in request.model_fields_set else default_model,
            request.base_url
            if "base_url" in request.model_fields_set
            else default_base_url,
            request.api_key
            if "api_key" in request.model_fields_set
            else default_api_key,
            request.dimension
            if "dimension" in request.model_fields_set
            else default_dimension,
        )

    @staticmethod
    def _test_error_message(error: Exception, secrets: tuple[str | None, ...]) -> str:
        message = str(error).strip() or error.__class__.__name__
        for secret in secrets:
            if secret:
                message = message.replace(secret, "***")
        return message

    @staticmethod
    def _test_parser_endpoint(
        base_url: str | None, api_key: str | None, api_mode: str = "saas_precision"
    ) -> str:
        if not base_url:
            raise RuntimeError("未设置 MinerU API URL")
        endpoint = (
            f"{base_url.rstrip('/')}/health"
            if api_mode == "self_hosted"
            else f"{base_url.rstrip('/')}/api/v4/extract-results/batch/{uuid.uuid4()}"
        )
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        try:
            response = httpx.get(endpoint, headers=headers, timeout=15.0)
        except httpx.HTTPError as exc:
            raise RuntimeError("MinerU API 连接失败，请检查 URL 和网络") from exc
        if response.status_code in {401, 403}:
            raise RuntimeError("MinerU API Key 无效或没有访问权限")
        if response.status_code == 404:
            return "MinerU API 连接成功，查询接口可用"
        if response.status_code >= 400:
            raise RuntimeError(f"MinerU API 返回 HTTP {response.status_code}")
        return "MinerU API 连接成功"

    def test_connection(
        self,
        request: ModelConfigTestRequest,
        defaults: tuple[str | None, str | None, str | None, int | None] | None = None,
    ) -> ModelConfigTestResponse:
        api_key: str | None = None
        try:
            model, base_url, api_key, dimension = self._test_endpoint_values(
                request, defaults
            )
            if not base_url:
                raise RuntimeError("模型 API URL 未配置")
            if request.target in {"llm", "embedding"} and not model:
                raise RuntimeError("模型名称未配置")
            if request.target == "llm":
                build_chat_model(
                    model=model,
                    base_url=base_url,
                    api_key=api_key,
                    allow_env_fallback=False,
                ).invoke("连接测试：请仅回复 OK。")
                message = "LLM 连接成功"
            elif request.target == "embedding":
                vector = build_embeddings_model(
                    model=model,
                    base_url=base_url,
                    api_key=api_key,
                    dimensions=dimension,
                    allow_env_fallback=False,
                ).embed_query("连接测试")
                actual_dimension = len(vector)
                if actual_dimension == 0:
                    raise RuntimeError("Embedding 返回了空向量")
                if dimension is not None and actual_dimension != dimension:
                    raise RuntimeError(
                        f"Embedding 返回维度为 {actual_dimension}，配置维度为 {dimension}"
                    )
                message = f"Embedding 连接成功，向量维度 {actual_dimension}"
            else:
                message = self._test_parser_endpoint(
                    base_url, api_key, getattr(request, "api_mode", "saas_precision")
                )
            return ModelConfigTestResponse(
                target=request.target, ok=True, message=message
            )
        except Exception as exc:
            return ModelConfigTestResponse(
                target=request.target,
                ok=False,
                message=self._test_error_message(exc, (api_key,)),
            )

    @staticmethod
    def _profile_response(item: dict[str, Any]) -> ModelProfileResponse:
        payload = {key: value for key, value in item.items() if key != "api_key"}
        payload["api_key_configured"] = bool(item.get("api_key"))
        return ModelProfileResponse.model_validate(payload)

    def list_profiles(self, kind: str | None = None) -> list[ModelProfileResponse]:
        return [
            self._profile_response(item)
            for item in self.repository.list_model_profiles(kind)
        ]

    def get_profile(self, profile_id: str) -> ModelProfileResponse:
        item = self.repository.get_model_profile(profile_id)
        if item is None:
            raise ApiError("模型不存在", status.HTTP_404_NOT_FOUND)
        return self._profile_response(item)

    def require_llm_profile(self, profile_id: str | None = None) -> dict[str, Any]:
        item = (
            self.repository.get_model_profile(profile_id)
            if profile_id
            else self.repository.get_default_model_profile("llm")
        )
        if item is None:
            message = "LLM 模型不存在" if profile_id else "未配置默认 LLM 模型"
            raise ApiError(message, status.HTTP_422_UNPROCESSABLE_ENTITY)
        if item["kind"] != "llm":
            raise ApiError(
                "Agent 只能选择 LLM 模型", status.HTTP_422_UNPROCESSABLE_ENTITY
            )
        return item

    def create_profile(self, request: ModelProfileRequest) -> ModelProfileResponse:
        now = _now().isoformat()
        is_default = (
            request.is_default
            or not self.repository.has_default_model_profile(request.kind)
        )
        values = request.model_dump()
        values["id"] = str(uuid.uuid4())
        values["is_default"] = is_default
        try:
            item = self.repository.create_model_profile(values, now)
        except Exception as exc:
            if "UNIQUE constraint failed" in str(exc):
                raise ApiError(
                    "同一类别下模型名称不能重复", status.HTTP_409_CONFLICT
                ) from exc
            raise
        return self._profile_response(item)

    def update_profile(
        self, profile_id: str, request: ModelProfileRequest
    ) -> ModelProfileResponse:
        current = self.repository.get_model_profile(profile_id)
        if current is None:
            raise ApiError("模型不存在", status.HTTP_404_NOT_FOUND)
        if current["kind"] != request.kind:
            raise ApiError("模型类型不可修改", status.HTTP_422_UNPROCESSABLE_ENTITY)
        values = request.model_dump()
        values["id"] = profile_id
        values["is_default"] = bool(request.is_default or current["is_default"])
        if "api_key" not in request.model_fields_set:
            values["api_key"] = current["api_key"]
        try:
            item = self.repository.update_model_profile(values, _now().isoformat())
        except Exception as exc:
            if "UNIQUE constraint failed" in str(exc):
                raise ApiError(
                    "同一类别下模型名称不能重复", status.HTTP_409_CONFLICT
                ) from exc
            raise
        return self._profile_response(item)

    def set_default_profile(self, profile_id: str) -> ModelProfileResponse:
        current = self.repository.get_model_profile(profile_id)
        if current is None:
            raise ApiError("模型不存在", status.HTTP_404_NOT_FOUND)
        now = _now().isoformat()
        item = self.repository.set_default_model_profile(profile_id, now)
        return self._profile_response(item)

    def delete_profile(self, profile_id: str) -> None:
        current = self.repository.get_model_profile(profile_id)
        if current is None:
            raise ApiError("模型不存在", status.HTTP_404_NOT_FOUND)
        if current["kind"] == "embedding" and self.repository.count_knowledge_bases_for_vector_model(profile_id):
            raise ApiError(
                "该 Embedding 模型已被知识库使用，请先为这些知识库选择其他模型",
                status.HTTP_409_CONFLICT,
            )
        if current["kind"] == "llm" and self.repository.count_applications_for_llm_model(profile_id):
            raise ApiError(
                "该 LLM 模型已被 Agent 使用，请先删除使用该模型的 Agent",
                status.HTTP_409_CONFLICT,
            )
        if current["is_default"]:
            raise ApiError(
                "默认模型不能删除，请先切换其他模型", status.HTTP_409_CONFLICT
            )
        try:
            self.repository.delete_model_profile(profile_id)
        except KeyError as exc:
            raise ApiError("模型不存在", status.HTTP_404_NOT_FOUND) from exc

    def test_profile(
        self, profile_id: str, override: ModelConfigTestRequest | None = None
    ) -> ModelConfigTestResponse:
        item = self.repository.get_model_profile(profile_id)
        if item is None:
            raise ApiError("模型不存在", status.HTTP_404_NOT_FOUND)
        if override is not None and override.target != item["kind"]:
            raise ApiError(
                "模型类型与目录记录不一致", status.HTTP_422_UNPROCESSABLE_ENTITY
            )
        model = (
            override.model
            if override is not None and "model" in override.model_fields_set
            else item["model"]
        )
        base_url = (
            override.base_url
            if override is not None and "base_url" in override.model_fields_set
            else item["base_url"]
        )
        api_key = (
            override.api_key
            if override is not None and "api_key" in override.model_fields_set
            else item["api_key"]
        )
        dimension = (
            override.dimension
            if override is not None and "dimension" in override.model_fields_set
            else item["dimension"]
        )
        request = ModelConfigTestRequest(
            target=item["kind"],
            model=model,
            base_url=base_url,
            api_key=api_key,
            dimension=dimension,
            api_mode=(
                override.api_mode
                if override is not None and "api_mode" in override.model_fields_set
                else item["api_mode"]
            ),
        )
        return self.test_connection(
            request,
            (item["model"], item["base_url"], item["api_key"], item["dimension"]),
        )

    def _default_catalog_profile(self, kind: str) -> dict[str, Any]:
        profile = self.repository.get_default_model_profile(kind)
        if profile is None:
            raise ApiError(
                f"未配置默认{ {'llm': 'LLM', 'embedding': 'Embedding', 'parser': '文档解析'}[kind] }模型",
                status.HTTP_422_UNPROCESSABLE_ENTITY,
            )
        return profile

    def build_llm(self) -> Any:
        """Build the strict catalog LLM used by API-server indexing tasks."""
        profile = self._default_catalog_profile("llm")
        model = str(profile.get("model") or "").strip()
        if not model:
            raise ApiError(
                f"默认模型“{profile['name']}”未配置模型名称",
                status.HTTP_422_UNPROCESSABLE_ENTITY,
            )
        base_url = self._catalog_base_url(profile)
        api_key = str(profile.get("api_key") or "").strip()
        if not api_key:
            raise ApiError(
                f"默认模型“{profile['name']}”未配置 API Key",
                status.HTTP_422_UNPROCESSABLE_ENTITY,
            )
        return build_chat_model(
            model=model,
            base_url=base_url,
            api_key=api_key,
            allow_env_fallback=False,
        )

    def parser_runtime_config(self) -> dict[str, Any]:
        """Return the strict, workspace-managed MinerU configuration."""
        parser = self._default_catalog_profile("parser")
        base_url = self._catalog_base_url(parser)
        api_key = str(parser.get("api_key") or "").strip()
        api_mode = str(parser.get("api_mode") or "saas_precision")
        if api_mode == "saas_precision" and not api_key:
            raise ApiError(
                f"默认解析模型“{parser['name']}”未配置 API Key",
                status.HTTP_422_UNPROCESSABLE_ENTITY,
            )
        return {
            "base_url": base_url,
            "api_key": api_key,
            "api_mode": api_mode,
            "model_version": parser["model_version"],
            "language": parser["language"],
            "is_ocr": bool(parser["is_ocr"]),
            "enable_table": bool(parser["enable_table"]),
            "enable_formula": bool(parser["enable_formula"]),
            "page_ranges": str(parser["page_ranges"] or ""),
        }

    def embedding_runtime_config(
        self, profile_id: str | None = None, *, strict: bool = True
    ) -> dict[str, Any]:
        """Return the selected Embedding configuration.

        API-server callers pass the knowledge base's selected profile ID. The
        optional default lookup remains for older standalone callers and does
        not act as a knowledge-base fallback.
        """
        if profile_id is None:
            profile = self._default_catalog_profile("embedding")
        else:
            profile = self.repository.get_model_profile(profile_id)
            if profile is None or profile.get("kind") != "embedding":
                raise ApiError(
                    "选中的 Embedding 模型不存在",
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                )
        result = {
            # Older databases contain embedding_model_profiles.enabled. It is
            # no longer a second activation switch: selecting the profile on a
            # knowledge base is the source of truth.
            "enabled": True,
            "profile_id": str(profile["id"]),
            "profile_updated_at": str(profile["updated_at"]),
            "model": profile.get("model"),
            "base_url": str(profile.get("base_url") or "").strip() or None,
            "api_key": str(profile.get("api_key") or "").strip(),
            "dimension": profile.get("dimension"),
        }
        if not strict:
            return result
        if not result["base_url"]:
            raise ApiError(
                f"Embedding 模型“{profile['name']}”未配置 API URL",
                status.HTTP_422_UNPROCESSABLE_ENTITY,
            )
        if not result["model"]:
            raise ApiError(
                f"Embedding 模型“{profile['name']}”未配置模型名称",
                status.HTTP_422_UNPROCESSABLE_ENTITY,
            )
        if not result["api_key"]:
            raise ApiError(
                f"Embedding 模型“{profile['name']}”未配置 API Key",
                status.HTTP_422_UNPROCESSABLE_ENTITY,
            )
        if result["dimension"] is None:
            raise ApiError(
                f"Embedding 模型“{profile['name']}”未配置向量维度",
                status.HTTP_422_UNPROCESSABLE_ENTITY,
            )
        return result

    def require_embedding_profile(self, profile_id: str) -> dict[str, Any]:
        """Validate a knowledge base's Embedding model selection."""
        profile = self.repository.get_model_profile(profile_id)
        if profile is None or profile.get("kind") != "embedding":
            raise ApiError(
                "选中的 Embedding 模型不存在",
                status.HTTP_422_UNPROCESSABLE_ENTITY,
            )
        return profile

    @staticmethod
    def _catalog_base_url(profile: dict[str, Any]) -> str:
        base_url = str(profile.get("base_url") or "").strip()
        if not base_url:
            raise ApiError(
                f"模型“{profile['name']}”未配置 API URL",
                status.HTTP_422_UNPROCESSABLE_ENTITY,
            )
        return base_url

    def runtime_config(
        self,
        application_provider: str,
        application_model_profile_id: str | None,
    ) -> tuple[str | None, ProviderConfig, bool, str | None, int | None, int | None]:
        """Resolve app overrides and catalog defaults for AgentRuntime.

        The Agent core currently accepts one API key/base URL pair. The LLM
        endpoint is therefore resolved here. URL resolution intentionally uses
        only the selected model catalog entry.
        """
        if application_provider != "default":
            raise ApiError(
                "应用仍使用已废弃的 Provider，请在模型管理中设置默认 LLM 模型",
                status.HTTP_422_UNPROCESSABLE_ENTITY,
            )

        llm = self.require_llm_profile(application_model_profile_id)
        llm_base_url = self._catalog_base_url(llm)
        if not llm["model"]:
            raise ApiError(
                f"默认模型“{llm['name']}”未配置模型名称",
                status.HTTP_422_UNPROCESSABLE_ENTITY,
            )
        profile = ProviderConfig(
            model=llm["model"],
            base_url=llm_base_url,
            api_key=llm["api_key"],
        )
        # A legacy application could override only the model name while keeping
        # this profile's URL and API key. Always use one complete LLM profile.
        return (
            llm["model"],
            profile,
            False,
            None,
            None,
            llm.get("context_window_tokens"),
        )


__all__ = ["ModelConfigService"]
