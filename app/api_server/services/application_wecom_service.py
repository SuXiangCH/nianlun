"""WeCom channel management, publication state and gateway control."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from app.api_server.apis.v1.publishing_schemas import (
    ApplicationDeployRequest,
    ApplicationReleaseResponse,
    PublicationResponse,
    PublicationUpdateRequest,
    WecomChannelCreateRequest,
    WecomChannelState,
    WecomChannelUpdateRequest,
    WecomStatusRequest,
)
from app.api_server.common.errors import ApiError
from app.api_server.config import ApiServerSettings
from app.api_server.repositories.wecom_channels import WecomChannelRepository
from app.api_server.services.application_release_service import (
    ApplicationReleaseService,
    config_fingerprint,
)
from app.api_server.services.application_service import ApplicationService
from app.api_server.services.gateway_control import GatewayControlClient

logger = logging.getLogger(__name__)


class ApplicationWecomService:
    def __init__(
        self,
        repository: WecomChannelRepository,
        releases: ApplicationReleaseService,
        applications: ApplicationService,
        settings: ApiServerSettings,
        control: GatewayControlClient,
    ) -> None:
        self.repository = repository
        self.releases = releases
        self.applications = applications
        self.settings = settings
        self.control = control

    def _require_internal_token(self) -> None:
        if not self.settings.internal_token:
            raise ApiError("企业微信内部接口未配置访问令牌", 503)

    def _refresh(self) -> None:
        try:
            self.control.refresh()
        except Exception:
            logger.exception("wecom.gateway_refresh_failed")

    def _publication(self, application_id: str) -> PublicationResponse:
        return self.attach(self.releases.get_publication(application_id))

    def _validate_release(self, release: ApplicationReleaseResponse) -> int:
        self.releases.validate_release(release.config)
        knowledge = self.applications.knowledge_base_lookup(
            release.config.knowledge_base_id
        )
        if not knowledge.get("snapshot_relpath") or not knowledge.get(
            "snapshot_manifest_sha256"
        ):
            raise ApiError("知识库 committed snapshot 尚未就绪", 503)
        return int(knowledge.get("content_version", 0))

    def _dependency_error(self, release: ApplicationReleaseResponse) -> str | None:
        try:
            self._validate_release(release)
        except ApiError as exc:
            return exc.message
        return None

    def _state(self, channel: dict[str, Any]) -> WecomChannelState:
        application_id = str(channel["application_id"])
        release = (
            self.releases.require_release(application_id, channel["active_release_id"])
            if channel["active_release_id"]
            else None
        )
        draft_error: str | None = None
        changed = True
        try:
            draft = self.releases.draft_config(application_id)
            changed = (
                release is None or config_fingerprint(draft)[1] != release.config_hash
            )
        except ApiError as exc:
            draft_error = exc.message
        dependency_error = (
            draft_error if release is None else self._dependency_error(release)
        )
        return WecomChannelState(
            id=str(channel["id"]),
            bot_id=str(channel["bot_id"]),
            has_secret=bool(channel["secret"]),
            default_response=str(channel["default_response"]),
            enabled=bool(channel["enabled"]),
            active_release=release,
            deployment_version=int(channel["deployment_version"]),
            config_revision=int(channel["config_revision"]),
            status=channel["status"],
            error_code=channel["error_code"],
            expires_at=channel["expires_at"],
            has_unpublished_changes=changed,
            dependencies_ready=dependency_error is None,
            dependency_error=dependency_error,
            last_deployed_at=self.repository.last_deployed_at(application_id),
            last_reported_at=channel["last_reported_at"],
        )

    def attach(self, publication: PublicationResponse) -> PublicationResponse:
        channel = self.repository.get(publication.application_id)
        if channel is None:
            return publication
        return publication.model_copy(update={"wecom_channel": self._state(channel)})

    def create(
        self, application_id: str, request: WecomChannelCreateRequest
    ) -> PublicationResponse:
        self._require_internal_token()
        expires_at = (
            request.expires_at.astimezone(timezone.utc).isoformat()
            if request.expires_at is not None
            else None
        )
        with self.releases.entry_lock(application_id):
            self.repository.create(
                application_id,
                bot_id=request.bot_id,
                secret=request.secret,
                default_response=request.default_response,
                expires_at=expires_at,
            )
        self._refresh()
        return self._publication(application_id)

    def update(
        self, application_id: str, request: WecomChannelUpdateRequest
    ) -> PublicationResponse:
        values = request.model_dump(
            exclude_unset=True, exclude={"expected_config_revision"}
        )
        for key in ("bot_id", "secret", "default_response"):
            if key in values and values[key] is None:
                raise ApiError("渠道配置字段不能为 null", 422)
        if values.get("expires_at") is not None:
            values["expires_at"] = (
                values["expires_at"].astimezone(timezone.utc).isoformat()
            )
        with self.releases.entry_lock(application_id):
            self.repository.update(
                application_id,
                values,
                expected_config_revision=request.expected_config_revision,
            )
        self._refresh()
        return self._publication(application_id)

    def set_enabled(
        self, application_id: str, request: PublicationUpdateRequest
    ) -> PublicationResponse:
        with self.releases.entry_lock(application_id):
            if request.enabled:
                channel = self.repository.get(application_id)
                if channel is None:
                    raise ApiError("企业微信渠道尚未创建", 404)
                if channel["active_release_id"] is None:
                    raise ApiError("应用尚未发布", 409)
                release = self.releases.require_release(
                    application_id, channel["active_release_id"]
                )
                self._validate_release(release)
            self.repository.set_enabled(
                application_id,
                request.enabled,
                request.expected_deployment_version,
            )
        self._refresh()
        return self._publication(application_id)

    def deploy(
        self, application_id: str, request: ApplicationDeployRequest
    ) -> PublicationResponse:
        with self.releases.entry_lock(application_id):
            release = self.releases.require_release(application_id, request.release_id)
            content_version = self._validate_release(release)
            self.repository.deploy(
                application_id,
                release.id,
                expected_deployment_version=request.expected_deployment_version,
                content_version=content_version,
            )
        self._refresh()
        return self._publication(application_id)

    def connect(self, application_id: str) -> dict[str, Any]:
        if not self.settings.gateway_control_urls:
            raise ApiError("网关控制地址未配置", 503)
        channel = self.repository.get(application_id)
        if channel is None:
            raise ApiError("企业微信渠道尚未创建", 404)
        if not channel["enabled"] or channel["active_release_id"] is None:
            raise ApiError("企业微信渠道未启用", 409)
        expires_at = channel["expires_at"]
        if expires_at is not None and datetime.fromisoformat(
            expires_at
        ) <= datetime.now(timezone.utc):
            raise ApiError("企业微信渠道已过期，请更新有效期后重试", 409)
        result = self.control.connect(channel["id"], channel["config_revision"])
        if result is None:
            raise ApiError("网关连接失败", 502)
        return result

    def snapshot(self) -> dict[str, Any]:
        return {"channels": self.repository.snapshot()}

    def report_status(self, channel_id: str, request: WecomStatusRequest) -> bool:
        return self.repository.report_status(
            channel_id, request.revision, request.status, request.error_code
        )
