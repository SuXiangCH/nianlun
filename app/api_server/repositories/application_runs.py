"""Atomic public admission and run/message completion."""

from __future__ import annotations

from nianlun.agent.contracts import AnswerImagePolicy

from datetime import datetime, timedelta, timezone
import json
from typing import Any
import uuid

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.api_server.common.errors import ApiError
from app.api_server.database.connection import SQLiteConnectionFactory
from app.api_server.database.models.chat import Conversation, Message
from app.api_server.database.models.application_runs import ApplicationRun
from app.api_server.database.models.publishing import (
    ApplicationApiClient,
    ApplicationApiKey,
    ApplicationDeployment,
    ApplicationRelease,
    ApplicationWecomChannel,
)
from app.api_server.repositories.sqlite_chat import SQLiteChatRepository
from app.api_server.database.models import ImageAccessGrant
from app.api_server.repositories.answer_images import normalize_completion_images


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def run_dict(run: ApplicationRun) -> dict[str, Any]:
    return {column.name: getattr(run, column.name) for column in run.__table__.columns}


class ApplicationRunRepository:
    def __init__(self, factory: SQLiteConnectionFactory) -> None:
        self.factory = factory
        self.chat = SQLiteChatRepository(factory)

    def find(
        self, app_id: str, client_id: str, user_id: str, key: str
    ) -> dict[str, Any] | None:
        with self.factory.session_scope() as session:
            row = session.scalar(
                select(ApplicationRun).where(
                    ApplicationRun.application_id == app_id,
                    ApplicationRun.client_id == client_id,
                    ApplicationRun.user_id == user_id,
                    ApplicationRun.idempotency_key == key,
                )
            )
            return run_dict(row) if row else None

    def conversation(
        self,
        app_id: str,
        client_id: str,
        user_id: str,
        conversation_id: str,
        *,
        channel: str = "api",
    ) -> dict[str, Any]:
        with self.factory.session_scope() as session:
            item = session.get(Conversation, conversation_id)
            if (
                item is None
                or item.channel != channel
                or item.application_id != app_id
                or item.api_client_id != client_id
                or item.external_user_id != user_id
                or item.status == "deleted"
            ):
                raise ApiError("会话不存在", 404)
            if item.context_state != "ready":
                raise ApiError("会话上下文已中断，请创建新会话", 409)
            if item.expires_at and item.expires_at <= utcnow().isoformat():
                raise ApiError("会话已过期，请创建新会话", 409)
            if session.scalar(
                select(ApplicationRun.id).where(
                    ApplicationRun.conversation_id == conversation_id,
                    ApplicationRun.status.in_(["running", "cancelling"]),
                )
            ):
                raise ApiError("会话正忙", 409)
            return {"id": item.id, "release_id": item.release_id}

    def latest_channel_conversation(
        self, app_id: str, user_key: str
    ) -> dict[str, Any] | None:
        with self.factory.session_scope() as session:
            item = session.scalar(
                select(Conversation)
                .where(
                    Conversation.application_id == app_id,
                    Conversation.channel == "wecom",
                    Conversation.external_user_id == user_key,
                    Conversation.status == "active",
                )
                .order_by(Conversation.updated_at.desc())
                .limit(1)
            )
            if item is None:
                return None
            return {
                "id": item.id,
                "release_id": item.release_id,
                "context_state": item.context_state,
                "expires_at": item.expires_at,
            }

    def has_active_run(self, conversation_id: str) -> bool:
        with self.factory.session_scope() as session:
            return (
                session.scalar(
                    select(ApplicationRun.id).where(
                        ApplicationRun.conversation_id == conversation_id,
                        ApplicationRun.status.in_(["running", "cancelling"]),
                    )
                )
                is not None
            )

    def archive_conversation(self, conversation_id: str) -> None:
        with self.factory.session_scope(write=True) as session:
            item = session.get(Conversation, conversation_id)
            if item is not None and item.status == "active":
                item.status = "archived"

    def interrupt_context(self, conversation_id: str) -> None:
        with self.factory.session_scope(write=True) as session:
            item = session.get(Conversation, conversation_id)
            if item and item.status != "deleted":
                item.context_state = "interrupted"

    def admit(
        self,
        *,
        app_id: str,
        client_id: str,
        user_id: str,
        release_id: str,
        conversation_id: str | None,
        message: str,
        key: str,
        fingerprint: str,
        content_version: int,
        request_id: str | None,
        retention_days: int,
        key_id: str,
        image_transport: str = "url",
    ) -> dict[str, Any]:
        now = utcnow()
        with self.factory.session_scope(write=True) as session:
            deployment = session.get(ApplicationDeployment, app_id)
            client = session.get(ApplicationApiClient, client_id)
            credential = session.get(ApplicationApiKey, key_id)
            release = session.get(ApplicationRelease, release_id)
            if (
                credential is None
                or credential.client_id != client_id
                or credential.revoked_at
                or (credential.expires_at and credential.expires_at <= now.isoformat())
            ):
                raise ApiError("无效的应用凭据", 401)
            if (
                client is None
                or client.application_id != app_id
                or release is None
                or release.application_id != app_id
            ):
                raise ApiError("应用不存在", 404)
            if not client.enabled or deployment is None or not deployment.enabled:
                raise ApiError("应用 API 已停用", 403)
            if conversation_id is None and deployment.active_release_id != release_id:
                raise ApiError("发布入口已变化，请重试", 409)
            duplicate = session.scalar(
                select(ApplicationRun).where(
                    ApplicationRun.application_id == app_id,
                    ApplicationRun.client_id == client_id,
                    ApplicationRun.user_id == user_id,
                    ApplicationRun.idempotency_key == key,
                )
            )
            if duplicate:
                raise ApiError(
                    "调用已被受理，请查询结果",
                    409,
                    headers={
                        "Location": f"/api/v1/runtime/apps/{app_id}/runs/{duplicate.id}",
                        "Retry-After": "1",
                    },
                )
            if conversation_id:
                item = session.get(Conversation, conversation_id)
                if (
                    item is None
                    or item.channel != "api"
                    or item.application_id != app_id
                    or item.api_client_id != client_id
                    or item.external_user_id != user_id
                    or item.release_id != release_id
                ):
                    raise ApiError("会话不存在", 404)
                if item.context_state != "ready" or (
                    item.expires_at and item.expires_at <= now.isoformat()
                ):
                    raise ApiError("会话上下文不可继续，请创建新会话", 409)
                if session.scalar(
                    select(ApplicationRun.id).where(
                        ApplicationRun.conversation_id == conversation_id,
                        ApplicationRun.status.in_(["running", "cancelling"]),
                    )
                ):
                    raise ApiError("会话正忙", 409)
            else:
                conversation_id = str(uuid.uuid4())
                session.add(
                    Conversation(
                        id=conversation_id,
                        application_id=app_id,
                        title=message[:40],
                        channel="api",
                        api_client_id=client_id,
                        external_user_id=user_id,
                        release_id=release_id,
                        context_state="ready",
                        status="active",
                        created_at=now.isoformat(),
                        updated_at=now.isoformat(),
                        expires_at=(now + timedelta(days=retention_days)).isoformat(),
                    )
                )
                session.flush()
            message_id = str(uuid.uuid4())
            self.chat.begin_turn(
                application_id=app_id,
                conversation_id=conversation_id,
                user_message_id=str(uuid.uuid4()),
                assistant_message_id=message_id,
                user_content=message,
                now=now,
                session=session,
                channel="api",
            )
            row = ApplicationRun(
                id=str(uuid.uuid4()),
                application_id=app_id,
                client_id=client_id,
                user_id=user_id,
                conversation_id=conversation_id,
                release_id=release_id,
                message_id=message_id,
                idempotency_key=key,
                request_fingerprint=fingerprint,
                status="running",
                knowledge_base_content_version=content_version,
                request_id=request_id,
                created_at=now.isoformat(),
                updated_at=now.isoformat(),
                image_transport=image_transport,
            )
            session.add(row)
            session.flush()
            return run_dict(row)

    def admit_channel(
        self,
        *,
        app_id: str,
        channel_id: str,
        revision: int,
        user_key: str,
        release_id: str,
        conversation_id: str | None,
        message: str,
        key: str,
        fingerprint: str,
        content_version: int,
        request_id: str | None,
        retention_days: int,
    ) -> dict[str, Any]:
        now = utcnow()
        client_id = f"wecom:{channel_id}"
        try:
            with self.factory.session_scope(write=True) as session:
                channel = session.get(ApplicationWecomChannel, channel_id)
                if channel is None or channel.application_id != app_id:
                    raise ApiError("渠道不存在", 404)
                if not channel.enabled:
                    raise ApiError("企业微信渠道已停用", 403)
                if channel.expires_at and (
                    datetime.fromisoformat(channel.expires_at) <= now
                ):
                    raise ApiError("企业微信渠道已过期", 403)
                if channel.config_revision != revision:
                    raise ApiError("渠道配置已变化，请重试", 409)
                release = session.get(ApplicationRelease, release_id)
                if release is None or release.application_id != app_id:
                    raise ApiError("发布版本不存在", 404)
                if session.scalar(
                    select(ApplicationRun.id).where(
                        ApplicationRun.application_id == app_id,
                        ApplicationRun.client_id == client_id,
                        ApplicationRun.user_id == user_key,
                        ApplicationRun.idempotency_key == key,
                    )
                ):
                    raise ApiError(
                        "调用已被受理，请查询结果",
                        409,
                        headers={"Retry-After": "1"},
                    )
                if conversation_id is None:
                    if channel.active_release_id != release_id:
                        raise ApiError("发布入口已变化，请重试", 409)
                    conversation_id = str(uuid.uuid4())
                    session.add(
                        Conversation(
                            id=conversation_id,
                            application_id=app_id,
                            title=message[:40],
                            channel="wecom",
                            api_client_id=None,
                            external_user_id=user_key,
                            release_id=release_id,
                            context_state="ready",
                            status="active",
                            created_at=now.isoformat(),
                            updated_at=now.isoformat(),
                            expires_at=(
                                now + timedelta(days=retention_days)
                            ).isoformat(),
                        )
                    )
                    session.flush()
                else:
                    item = session.get(Conversation, conversation_id)
                    if (
                        item is None
                        or item.channel != "wecom"
                        or item.application_id != app_id
                        or item.external_user_id != user_key
                        or item.release_id != release_id
                        or item.status != "active"
                    ):
                        raise ApiError("会话不可继续，请稍后重试", 409)
                    if item.context_state != "ready" or (
                        item.expires_at
                        and datetime.fromisoformat(item.expires_at) <= now
                    ):
                        raise ApiError("会话上下文不可继续", 409)
                    if session.scalar(
                        select(ApplicationRun.id).where(
                            ApplicationRun.conversation_id == conversation_id,
                            ApplicationRun.status.in_(["running", "cancelling"]),
                        )
                    ):
                        raise ApiError("会话正忙", 409)
                    item.expires_at = (now + timedelta(days=retention_days)).isoformat()
                message_id = str(uuid.uuid4())
                self.chat.begin_turn(
                    application_id=app_id,
                    conversation_id=conversation_id,
                    user_message_id=str(uuid.uuid4()),
                    assistant_message_id=message_id,
                    user_content=message,
                    now=now,
                    session=session,
                    channel="wecom",
                )
                row = ApplicationRun(
                    id=str(uuid.uuid4()),
                    application_id=app_id,
                    client_id=client_id,
                    user_id=user_key,
                    conversation_id=conversation_id,
                    release_id=release_id,
                    message_id=message_id,
                    idempotency_key=key,
                    request_fingerprint=fingerprint,
                    status="running",
                    knowledge_base_content_version=content_version,
                    request_id=request_id,
                    created_at=now.isoformat(),
                    updated_at=now.isoformat(),
                )
                session.add(row)
                session.flush()
                return run_dict(row)
        except IntegrityError as exc:
            raise ApiError("会话正忙，请稍后重试", 409) from exc

    def complete(
        self,
        run_id: str,
        response: dict[str, Any],
        raw: dict[str, Any],
        retention_days: int,
        *,
        grants: list[dict[str, Any]] | None = None,
        principal_key_id: str | None = None,
        result_retention_days: int = 30,
        image_policy: AnswerImagePolicy = AnswerImagePolicy(),
    ) -> bool:
        now = utcnow()
        with self.factory.session_scope(write=True) as session:
            row = session.get(ApplicationRun, run_id)
            if row is None or row.status != "running":
                return False
            if principal_key_id is not None:
                credential = session.get(ApplicationApiKey, principal_key_id)
                client = session.get(ApplicationApiClient, row.client_id)
                deployment = session.get(ApplicationDeployment, row.application_id)
                if (
                    credential is None
                    or credential.revoked_at
                    or credential.client_id != row.client_id
                    or (
                        credential.expires_at
                        and datetime.fromisoformat(credential.expires_at) <= now
                    )
                    or client is None
                    or not client.enabled
                    or deployment is None
                    or not deployment.enabled
                ):
                    raise ApiError("应用凭据或渠道已失效", 403)
            normalize_completion_images(session, raw, policy=image_policy)
            from app.api_server.services.answer_image_projection import (
                reconcile_completion_projection,
            )

            response = reconcile_completion_projection(
                response, raw, policy=image_policy
            )
            self.chat.complete_turn(
                application_id=row.application_id,
                conversation_id=row.conversation_id,
                assistant_message_id=row.message_id,
                answer=raw["answer"],
                route=raw.get("route", "direct"),
                snippets=raw.get("retrieved_snippets", []),
                usage=raw.get("usage"),
                ttft_ms=raw.get("ttft_ms"),
                now=now,
                session=session,
                images=raw.get("images", []),
                image_policy=image_policy,
                answer_format_version=raw.get("answer_format_version", 1),
                answer_output_mode=raw.get("answer_output_mode", "text"),
                image_previews_unavailable=raw.get("image_previews_unavailable", 0),
            )
            session.flush()
            available = {
                image["id"]
                for image in raw.get("images", [])
                if image.get("status") == "available"
            }
            for grant in grants or []:
                if grant["attachment_id"] in available:
                    session.add(ImageAccessGrant(**grant))
            row.status = "completed"
            row.result_json = json.dumps(response, ensure_ascii=False)
            row.updated_at = row.finished_at = now.isoformat()
            row.result_expires_at = (
                now + timedelta(days=result_retention_days)
            ).isoformat()
            conversation = session.get(Conversation, row.conversation_id)
            assert conversation is not None
            conversation.expires_at = (now + timedelta(days=retention_days)).isoformat()
            return True

    def cancel(
        self, run_id: str, error_type: str, code: int, message: str, *, exited: bool
    ) -> None:
        now = utcnow()
        with self.factory.session_scope(write=True) as session:
            row = session.get(ApplicationRun, run_id)
            if (
                row is None
                or row.status == "completed"
                or row.status not in ("running", "cancelling")
                or (row.status == "cancelling" and not exited)
            ):
                return
            row.status = error_type if exited else "cancelling"
            row.error_type, row.error_status, row.error_message = (
                error_type,
                code,
                message,
            )
            row.updated_at = now.isoformat()
            if exited:
                row.finished_at = now.isoformat()
            conversation = session.get(Conversation, row.conversation_id)
            assert conversation is not None
            conversation.context_state = "interrupted"
            self.chat.fail_turn(
                application_id=row.application_id,
                conversation_id=row.conversation_id,
                assistant_message_id=row.message_id,
                error_message=message,
                now=now,
                session=session,
            )

    def recover(self, *, checkpoint_missing: bool) -> None:
        with self.factory.session_scope() as session:
            active = session.scalars(
                select(ApplicationRun.id).where(
                    ApplicationRun.status.in_(["running", "cancelling"])
                )
            ).all()
        for run_id in active:
            self.cancel(
                run_id, "interrupted", 409, "服务中断，请创建新会话", exited=True
            )
        if checkpoint_missing:
            with self.factory.session_scope(write=True) as session:
                for item in session.scalars(
                    select(Conversation).where(
                        Conversation.channel.in_(("api", "wecom"))
                    )
                ):
                    item.context_state = "interrupted"

    def get(self, run_id: str) -> dict[str, Any] | None:
        with self.factory.session_scope() as session:
            row = session.get(ApplicationRun, run_id)
            return run_dict(row) if row else None

    def list(
        self,
        app_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
        status: str | None = None,
        client_id: str | None = None,
    ) -> list[dict[str, Any]]:
        columns = [
            column
            for column in ApplicationRun.__table__.columns
            if column.name != "result_json"
        ]
        query = (
            select(*columns, Message.usage_json)
            .outerjoin(Message, Message.id == ApplicationRun.message_id)
            .where(ApplicationRun.application_id == app_id)
        )
        if status:
            query = query.where(ApplicationRun.status == status)
        if client_id:
            query = query.where(ApplicationRun.client_id == client_id)
        with self.factory.session_scope() as session:
            rows = session.execute(
                query.order_by(ApplicationRun.created_at.desc())
                .limit(limit)
                .offset(offset)
            ).mappings()
            result = []
            for row in rows:
                item = dict(row)
                usage = item.pop("usage_json")
                item["usage"] = json.loads(usage) if usage is not None else None
                result.append(item)
            return result

    def count(
        self, app_id: str, *, status: str | None = None, client_id: str | None = None
    ) -> int:
        query = select(func.count(ApplicationRun.id)).where(
            ApplicationRun.application_id == app_id
        )
        if status:
            query = query.where(ApplicationRun.status == status)
        if client_id:
            query = query.where(ApplicationRun.client_id == client_id)
        with self.factory.session_scope() as session:
            return int(session.scalar(query) or 0)

    def checkpoint_cleanup_candidates(self) -> list[tuple[str, str]]:
        with self.factory.session_scope(write=True) as session:
            active = select(ApplicationRun.conversation_id).where(
                ApplicationRun.status.in_(["running", "cancelling"])
            )
            rows = session.scalars(
                select(Conversation)
                .where(
                    Conversation.channel.in_(("api", "wecom")),
                    Conversation.expires_at <= utcnow().isoformat(),
                    Conversation.context_state != "expired",
                    ~Conversation.id.in_(active),
                )
                .limit(100)
            ).all()
            for row in rows:
                row.context_state = "cleanup_pending"
            return [(row.id, row.channel) for row in rows]

    def checkpoint_cleaned(self, conversation_id: str) -> None:
        with self.factory.session_scope(write=True) as session:
            item = session.get(Conversation, conversation_id)
            if item and item.context_state == "cleanup_pending":
                item.context_state = "expired"

    def conversation_ids_for_application(
        self, application_id: str
    ) -> list[tuple[str, str]]:
        """Conversation ids and channels bound to an application."""
        with self.factory.session_scope() as session:
            rows = session.execute(
                select(Conversation.id, Conversation.channel).where(
                    Conversation.application_id == application_id,
                    Conversation.channel.in_(("api", "wecom")),
                )
            )
            return [(str(row[0]), str(row[1])) for row in rows]
