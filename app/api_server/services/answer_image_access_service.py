"""Short-lived single-image bearer grants over the private image archive."""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode

from app.api_server.apis.v1.runtime_schemas import PublicAnswerImage
from app.api_server.common.errors import ApiError
from app.api_server.config import ApiServerSettings
from app.api_server.repositories.application_runs import ApplicationRunRepository
from app.api_server.services.answer_image_service import AnswerImageService
from app.api_server.services.application_release_service import (
    ApiPrincipal,
    ApplicationReleaseService,
)


class AnswerImageAccessService:
    def __init__(
        self,
        images: AnswerImageService,
        publishing: ApplicationReleaseService,
        runs: ApplicationRunRepository,
        settings: ApiServerSettings,
    ) -> None:
        self.images, self.publishing, self.runs, self.settings = (
            images,
            publishing,
            runs,
            settings,
        )

    def plan_grant(
        self, principal: ApiPrincipal, attachment_id: str, result_expires_at: str | None
    ) -> tuple[dict[str, Any], str]:
        if not result_expires_at:
            raise ApiError("历史运行不支持图片授权", 409)
        now = datetime.now(timezone.utc)
        deadline = datetime.fromisoformat(result_expires_at)
        if deadline <= now:
            raise ApiError("调用结果已过期", 410)
        if not self.settings.application_api_public_base_url:
            raise ApiError("公开图片地址未配置", 503)
        token = secrets.token_urlsafe(32)
        values = {
            "id": str(uuid.uuid4()),
            "attachment_id": attachment_id,
            "application_id": principal.application_id,
            "client_id": principal.client_id,
            "key_id": principal.key_id,
            "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
            "expires_at": min(
                now + timedelta(hours=self.settings.answer_image_grant_hours), deadline
            ).isoformat(),
            "revoked_at": None,
            "created_at": now.isoformat(),
        }
        url = (
            self.settings.application_api_public_base_url.rstrip("/")
            + f"/api/v1/runtime/images/{values['id']}?{urlencode({'token': token})}"
        )
        return values, url

    def read(self, grant_id: str, token: str) -> tuple[bytes, str]:
        if not self.settings.application_api_enabled:
            raise ApiError("图片不存在", 404)
        context = self.images.repository.grant_context(grant_id, token)
        run, grant = context["run"], context["grant"]
        result = self.images.read_content(
            run["application_id"],
            run["conversation_id"],
            run["message_id"],
            grant["attachment_id"],
        )
        self.images.repository.grant_context(grant_id, token)
        return result

    def refresh(
        self, principal: ApiPrincipal, run_id: str, attachment_id: str, user_id: str
    ) -> PublicAnswerImage:
        self.publishing.validate_principal(principal)
        run = self.runs.get(run_id)
        if (
            run is None
            or run["application_id"] != principal.application_id
            or run["client_id"] != principal.client_id
            or run["user_id"] != user_id
        ):
            raise ApiError("调用不存在", 404)
        if run["image_transport"] != "url":
            raise ApiError("内联图片结果不支持刷新链接", 409)
        image = self.images.repository.attachment(
            principal.application_id,
            run["conversation_id"],
            run["message_id"],
            attachment_id,
        )
        if (
            image is None
            or image["status"] != "available"
            or not image["display_in_answer"]
        ):
            raise ApiError("图片不存在", 404)
        values, url = self.plan_grant(
            principal, attachment_id, run["result_expires_at"]
        )
        self.images.repository.save_grant(values, run_id, user_id)
        return PublicAnswerImage(
            id=image["id"],
            ref_ids=image["ref_ids"],
            citation_ids=list(
                dict.fromkeys(
                    binding["citation_id"]
                    for binding in image["source_bindings"]
                    if isinstance(binding.get("citation_id"), int)
                )
            ),
            caption=image["caption"],
            width=image["width"],
            height=image["height"],
            orientation=image["orientation"],
            status="available",
            url=url,
            expires_at=values["expires_at"],
        )


__all__ = ["AnswerImageAccessService"]
