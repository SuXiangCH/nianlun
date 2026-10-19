"""Immutable application publication and application-scoped API access."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import threading
import weakref
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.api_server.apis.v1.publishing_schemas import (
    ApiClientCreateRequest,
    ApiClientResponse,
    ApiClientUpdateRequest,
    ApiKeyCreateRequest,
    ApiKeyResponse,
    ApplicationPublishRequest,
    ApplicationReleaseCreateRequest,
    ApplicationDeployRequest,
    ApplicationRestoreRequest,
    ApplicationDeploymentEventResponse,
    ApplicationDeploymentListResponse,
    ApplicationReleaseListResponse,
    ApplicationReleaseResponse,
    ApplicationUpdateRequest,
    PublicationResponse,
    PublicationUpdateRequest,
    PublishedConfig,
    PublishedLoopGuard,
)
from app.api_server.apis.v1.schemas import ApplicationResponse
from app.api_server.common.errors import ApiError
from app.api_server.repositories.publishing import PublishingRepository
from app.api_server.services.application_service import ApplicationService
from nianlun.agent.contracts import AGENT_TOOL_SCHEMA_VERSION
from nianlun.agent.lead_agent.prompt import PROMPT_VERSION
from nianlun.agent.middleware.retrieval_loop_guard_middleware import (
    AgentLoopGuardConfig,
)
from nianlun.agent.retrieval_answer import prompt as v1_prompt


@dataclass(frozen=True)
class ApiPrincipal:
    app_id: str
    client_id: str
    key_id: str

    @property
    def application_id(self) -> str:
        return self.app_id


def _v1_prompt_hash() -> str:
    return hashlib.sha256(Path(v1_prompt.__file__).read_bytes()).hexdigest()


def config_fingerprint(config: PublishedConfig) -> tuple[str, str]:
    canonical = json.dumps(
        config.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return canonical, hashlib.sha256(canonical.encode()).hexdigest()


class ApplicationReleaseService:
    def __init__(
        self, repository: PublishingRepository, applications: ApplicationService
    ) -> None:
        self.repository = repository
        self.applications = applications
        self._locks: weakref.WeakValueDictionary[str, threading.RLock] = (
            weakref.WeakValueDictionary()
        )
        self._locks_guard = threading.Lock()

    def entry_lock(self, application_id: str) -> threading.RLock:
        with self._locks_guard:
            return self._locks.setdefault(application_id, threading.RLock())

    def _draft_config(self, app_id: str) -> PublishedConfig:
        app = self.applications.require_record(app_id)
        knowledge = self.applications.knowledge_base_lookup(app["knowledge_base_id"])
        if not knowledge.get("snapshot_relpath") or not knowledge.get(
            "snapshot_manifest_sha256"
        ):
            raise ApiError("知识库 committed snapshot 尚未就绪", 503)
        models = self.applications.model_config_service
        if models is None:
            raise ApiError("API Server 未启用模型配置服务", 503)
        profile = models.require_llm_profile(app.get("model"))
        if not profile.get("api_key"):
            raise ApiError("模型调用凭据未配置", 503)
        if (
            app.get("provider", "default") != "default"
            or not profile.get("model")
            or not profile.get("base_url")
        ):
            raise ApiError("应用模型配置不可发布", 409)
        return PublishedConfig(
            application_id=app_id,
            agent_version=app.get("agent_version", "v0"),
            llm_model_id=str(profile["id"]),
            model=profile["model"],
            provider="default",
            base_url=profile["base_url"],
            context_window_tokens=profile.get("context_window_tokens") or 128_000,
            knowledge_base_id=app["knowledge_base_id"],
            clarification_enabled=app.get("clarification_enabled", True),
            loop_guard=PublishedLoopGuard.model_validate(
                asdict(AgentLoopGuardConfig())
            ),
            prompt_version=PROMPT_VERSION,
            tool_schema_version=AGENT_TOOL_SCHEMA_VERSION,
            v1_prompt_hash=_v1_prompt_hash(),
        )

    @staticmethod
    def _release(record: dict[str, Any]) -> ApplicationReleaseResponse:
        try:
            config = PublishedConfig.model_validate_json(record["config_json"])
        except ValidationError as exc:
            raise ApiError("发布配置版本不兼容，请重新发布", 409) from exc
        fields = {
            key: record[key]
            for key in ApplicationReleaseResponse.model_fields
            if key != "config"
        }
        return ApplicationReleaseResponse(**fields, config=config)

    def require_release(
        self, application_id: str, release_id: str | None = None
    ) -> ApplicationReleaseResponse:
        if release_id is not None:
            return self._release(self.repository.release(application_id, release_id))
        _, record = self.repository.publication(application_id)
        if record is None:
            raise ApiError("应用尚未发布", 403)
        return self._release(record)

    def validate_release(self, config: PublishedConfig) -> None:
        if (
            config.prompt_version != PROMPT_VERSION
            or config.tool_schema_version != AGENT_TOOL_SCHEMA_VERSION
            or config.checkpoint_schema_version != 1
            or config.v1_prompt_hash != _v1_prompt_hash()
        ):
            raise ApiError("发布版本与当前程序不兼容，请重新发布或新建会话", 409)
        models = self.applications.model_config_service
        if models is None:
            raise ApiError("API Server 未启用模型配置服务", 503)
        try:
            profile = models.require_llm_profile(config.llm_model_id)
        except ApiError as exc:
            raise ApiError("发布版本引用的模型不可用，请重新发布", 409) from exc
        if not profile.get("api_key"):
            raise ApiError("发布版本的模型调用凭据未配置", 503)
        if (
            profile.get("model") != config.model
            or profile.get("base_url") != config.base_url
            or (profile.get("context_window_tokens") or 128_000)
            != config.context_window_tokens
            or config.provider != "default"
        ):
            raise ApiError("发布版本与模型配置不匹配，请重新发布", 409)

    def get_publication(self, application_id: str) -> PublicationResponse:
        app = self.applications.require_record(application_id)
        deployment, record = self.repository.publication(application_id)
        release = self._release(record) if record else None
        draft_error: str | None = None
        error: str | None = None
        changed = True
        try:
            config = self._draft_config(application_id)
            changed = (
                release is None or config_fingerprint(config)[1] != release.config_hash
            )
        except ApiError as exc:
            draft_error = exc.message
        try:
            if release is not None:
                self.validate_release(release.config)
                knowledge = self.applications.knowledge_base_lookup(
                    release.config.knowledge_base_id
                )
                if not knowledge.get("snapshot_relpath") or not knowledge.get(
                    "snapshot_manifest_sha256"
                ):
                    raise ApiError("知识库 committed snapshot 尚未就绪", 503)
            else:
                error = draft_error
        except ApiError as exc:
            error = exc.message
        return PublicationResponse(
            application_id=application_id,
            api_channel_created=deployment is not None,
            enabled=bool(deployment and deployment["enabled"]),
            active_release=release,
            deployment_version=int(deployment["version"]) if deployment else 0,
            last_deployed_at=self.repository.last_deployed_at(application_id),
            config_version=int(app.get("config_version", 1)),
            has_unpublished_changes=changed,
            draft_ready=draft_error is None,
            draft_error=draft_error,
            dependencies_ready=error is None,
            dependency_error=error,
        )

    def create_api_channel(self, application_id: str) -> PublicationResponse:
        with self.entry_lock(application_id):
            self.repository.create_api_channel(application_id)
            return self.get_publication(application_id)

    def publish(
        self, application_id: str, request: ApplicationPublishRequest
    ) -> PublicationResponse:
        with self.entry_lock(application_id):
            config = self._draft_config(application_id)
            canonical, fingerprint = config_fingerprint(config)
            self.repository.publish(
                config,
                canonical,
                fingerprint,
                request.expected_config_version,
                request.expected_deployment_version,
            )
            return self.get_publication(application_id)

    def set_enabled(
        self, application_id: str, request: PublicationUpdateRequest
    ) -> PublicationResponse:
        with self.entry_lock(application_id):
            config = (
                self.require_release(application_id).config if request.enabled else None
            )
            if config is not None:
                self.validate_release(config)
                knowledge = self.applications.knowledge_base_lookup(
                    config.knowledge_base_id
                )
                if not knowledge.get("snapshot_relpath") or not knowledge.get(
                    "snapshot_manifest_sha256"
                ):
                    raise ApiError("知识库 committed snapshot 尚未就绪", 503)
            self.repository.set_enabled(
                application_id,
                request.enabled,
                request.expected_deployment_version,
                config,
            )
            return self.get_publication(application_id)

    def create_release(
        self, application_id: str, request: ApplicationReleaseCreateRequest
    ) -> ApplicationReleaseResponse:
        with self.entry_lock(application_id):
            config = self._draft_config(application_id)
            canonical, fingerprint = config_fingerprint(config)
            return self._release(
                self.repository.create_release(
                    config,
                    canonical,
                    fingerprint,
                    request.expected_config_version,
                    request.description.strip(),
                )
            )

    def deploy(
        self, application_id: str, request: ApplicationDeployRequest
    ) -> PublicationResponse:
        with self.entry_lock(application_id):
            release = self.require_release(application_id, request.release_id)
            self.validate_release(release.config)
            knowledge = self.applications.knowledge_base_lookup(
                release.config.knowledge_base_id
            )
            if not knowledge.get("snapshot_relpath") or not knowledge.get(
                "snapshot_manifest_sha256"
            ):
                raise ApiError("知识库 committed snapshot 尚未就绪", 503)
            self.repository.deploy(
                application_id,
                release.config,
                release.id,
                request.expected_deployment_version,
            )
            return self.get_publication(application_id)

    def restore_release(
        self, application_id: str, release_id: str, request: ApplicationRestoreRequest
    ) -> ApplicationResponse:
        with self.entry_lock(application_id):
            config = self.require_release(application_id, release_id).config
            return self.update(
                application_id,
                ApplicationUpdateRequest(
                    expected_config_version=request.expected_config_version,
                    knowledge_base_id=config.knowledge_base_id,
                    llm_model_id=config.llm_model_id,
                    agent_version=config.agent_version,
                    clarification_enabled=config.clarification_enabled,
                ),
            )

    def list_deployments(
        self, application_id: str, limit: int = 20, offset: int = 0
    ) -> ApplicationDeploymentListResponse:
        if not 1 <= limit <= 100 or offset < 0:
            raise ApiError("分页参数无效", 422)
        records, total = self.repository.deployment_events(
            application_id, limit, offset
        )
        return ApplicationDeploymentListResponse(
            items=[
                ApplicationDeploymentEventResponse.model_validate(item)
                for item in records
            ],
            total=total,
        )

    def list_releases(
        self, application_id: str, limit: int = 20, offset: int = 0
    ) -> ApplicationReleaseListResponse:
        if not 1 <= limit <= 100 or offset < 0:
            raise ApiError("分页参数无效", 422)
        records, total = self.repository.releases(application_id, limit, offset)
        return ApplicationReleaseListResponse(
            items=[self._release(record) for record in records], total=total
        )

    def update(
        self, application_id: str, request: ApplicationUpdateRequest
    ) -> ApplicationResponse:
        values = request.model_dump(
            exclude_unset=True, exclude={"expected_config_version"}
        )
        if any(value is None for key, value in values.items() if key != "llm_model_id"):
            raise ApiError("配置字段不能为 null", 422)
        if "knowledge_base_id" in values:
            knowledge = self.applications.knowledge_base_lookup(
                values["knowledge_base_id"]
            )
            if not knowledge.get("snapshot_relpath") or not knowledge.get(
                "snapshot_manifest_sha256"
            ):
                raise ApiError("知识库 committed snapshot 尚未就绪", 409)
        if "llm_model_id" in values:
            models = self.applications.model_config_service
            if models is None:
                raise ApiError("API Server 未启用模型配置服务", 503)
            values["model"] = str(
                models.require_llm_profile(values.pop("llm_model_id"))["id"]
            )
        with self.entry_lock(application_id):
            self.repository.update_application(
                application_id, values, request.expected_config_version
            )
            return self.applications.get(application_id)

    def create_client(
        self, application_id: str, request: ApiClientCreateRequest
    ) -> ApiClientResponse:
        return ApiClientResponse.model_validate(
            self.repository.create_client(application_id, request.name)
        )

    def list_clients(self, application_id: str) -> list[ApiClientResponse]:
        return [
            ApiClientResponse.model_validate(item)
            for item in self.repository.list_clients(application_id)
        ]

    def update_client(
        self, application_id: str, client_id: str, request: ApiClientUpdateRequest
    ) -> ApiClientResponse:
        values = request.model_dump(exclude_unset=True)
        if any(value is None for value in values.values()):
            raise ApiError("客户端字段不能为 null", 422)
        with self.entry_lock(application_id):
            return ApiClientResponse.model_validate(
                self.repository.update_client(application_id, client_id, values)
            )

    @staticmethod
    def _key(record: dict[str, Any]) -> ApiKeyResponse:
        return ApiKeyResponse.model_validate(
            {key: record[key] for key in ApiKeyResponse.model_fields}
        )

    def register_key(
        self, application_id: str, client_id: str, request: ApiKeyCreateRequest
    ) -> ApiKeyResponse:
        key_id = request.api_key.split(".", 1)[0][len("nl_app_") :]
        digest = hashlib.sha256(request.api_key.encode()).hexdigest()
        with self.entry_lock(application_id):
            return self._key(
                self.repository.register_key(
                    application_id,
                    client_id,
                    key_id,
                    digest,
                    request.name,
                    request.expires_at.astimezone(timezone.utc).isoformat()
                    if request.expires_at
                    else None,
                )
            )

    def list_keys(self, application_id: str, client_id: str) -> list[ApiKeyResponse]:
        return [
            self._key(item)
            for item in self.repository.list_keys(application_id, client_id)
        ]

    def revoke_key(
        self, application_id: str, client_id: str, key_id: str
    ) -> ApiKeyResponse:
        with self.entry_lock(application_id):
            return self._key(
                self.repository.revoke_key(application_id, client_id, key_id)
            )

    def _credential(
        self, application_id: str, key_id: str
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        records = self.repository.credential(key_id)
        if records is None:
            raise ApiError("API Key 无效", 401)
        key, client, deployment = records
        if key["revoked_at"] is not None or (
            key["expires_at"] is not None
            and datetime.fromisoformat(key["expires_at"]) <= datetime.now(timezone.utc)
        ):
            raise ApiError("API Key 无效", 401)
        if client["application_id"] != application_id:
            raise ApiError("应用不存在", 404)
        if not client["enabled"]:
            raise ApiError("调用客户端已停用", 403)
        if deployment is None or not deployment["enabled"]:
            raise ApiError("应用 API 已停用", 403)
        return key, client

    def authenticate(self, application_id: str | None, raw_key: str) -> ApiPrincipal:
        if (
            re.fullmatch(r"nl_app_[A-Za-z0-9_-]{16,64}\.[A-Za-z0-9_-]{43,128}", raw_key)
            is None
        ):
            raise ApiError("API Key 无效", 401)
        key_id = raw_key.split(".", 1)[0][len("nl_app_") :]
        if application_id is None:
            records = self.repository.credential(key_id)
            if records is None:
                raise ApiError("API Key 无效", 401)
            application_id = str(records[1]["application_id"])
        with self.entry_lock(application_id):
            records = self.repository.credential(key_id)
            stored_digest = records[0]["secret_digest"] if records else "0" * 64
            if (
                not hmac.compare_digest(
                    stored_digest, hashlib.sha256(raw_key.encode()).hexdigest()
                )
                or records is None
            ):
                raise ApiError("API Key 无效", 401)
            _, client = self._credential(application_id, key_id)
            return ApiPrincipal(application_id, str(client["id"]), key_id)

    def runtime_release(
        self, principal: ApiPrincipal, release_id: str | None = None
    ) -> ApplicationReleaseResponse:
        with self.entry_lock(principal.application_id):
            self.validate_principal(principal)
            release = self.require_release(principal.application_id, release_id)
            self.validate_release(release.config)
            return release

    def validate_principal(self, principal: ApiPrincipal) -> None:
        with self.entry_lock(principal.application_id):
            _, client = self._credential(principal.application_id, principal.key_id)
            if client["id"] != principal.client_id:
                raise ApiError("API Key 无效", 401)
