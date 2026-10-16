"""Atomic public admission and run/message completion."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from typing import Any
import uuid

from sqlalchemy import func, select

from app.api_server.common.errors import ApiError
from app.api_server.database.connection import SQLiteConnectionFactory
from app.api_server.database.models.chat import Conversation
from app.api_server.database.models.application_runs import ApplicationRun
from app.api_server.database.models.publishing import (
    ApplicationApiClient,
    ApplicationApiKey,
    ApplicationDeployment,
    ApplicationRelease,
)
from app.api_server.repositories.sqlite_chat import SQLiteChatRepository


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
        self, app_id: str, client_id: str, user_id: str, conversation_id: str
    ) -> dict[str, Any]:
        with self.factory.session_scope() as session:
            item = session.get(Conversation, conversation_id)
            if (
                item is None
                or item.channel != "api"
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

    def interrupt_context(self, conversation_id: str) -> None:
        with self.factory.session_scope(write=True) as session:
            item = session.get(Conversation, conversation_id)
            if item and item.channel == "api":
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
            )
            session.add(row)
            session.flush()
            return run_dict(row)

    def complete(
        self,
        run_id: str,
        response: dict[str, Any],
        raw: dict[str, Any],
        retention_days: int,
    ) -> bool:
        now = utcnow()
        with self.factory.session_scope(write=True) as session:
            row = session.get(ApplicationRun, run_id)
            if row is None or row.status != "running":
                return False
            self.chat.complete_turn(
                application_id=row.application_id,
                conversation_id=row.conversation_id,
                assistant_message_id=row.message_id,
                answer=response["answer"],
                route=raw.get("route", "direct"),
                snippets=raw.get("retrieved_snippets", []),
                usage=raw.get("usage"),
                ttft_ms=raw.get("ttft_ms"),
                now=now,
                session=session,
            )
            row.status = "completed"
            row.result_json = json.dumps(response, ensure_ascii=False)
            row.updated_at = row.finished_at = now.isoformat()
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
                    select(Conversation).where(Conversation.channel == "api")
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
        query = select(ApplicationRun).where(ApplicationRun.application_id == app_id)
        if status:
            query = query.where(ApplicationRun.status == status)
        if client_id:
            query = query.where(ApplicationRun.client_id == client_id)
        with self.factory.session_scope() as session:
            return [
                run_dict(row)
                for row in session.scalars(
                    query.order_by(ApplicationRun.created_at.desc())
                    .limit(limit)
                    .offset(offset)
                )
            ]

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

    def checkpoint_cleanup_candidates(self) -> list[str]:
        with self.factory.session_scope(write=True) as session:
            active = select(ApplicationRun.conversation_id).where(
                ApplicationRun.status.in_(["running", "cancelling"])
            )
            rows = session.scalars(
                select(Conversation)
                .where(
                    Conversation.channel == "api",
                    Conversation.expires_at <= utcnow().isoformat(),
                    Conversation.context_state != "expired",
                    ~Conversation.id.in_(active),
                )
                .limit(100)
            ).all()
            for row in rows:
                row.context_state = "cleanup_pending"
            return [row.id for row in rows]

    def checkpoint_cleaned(self, conversation_id: str) -> None:
        with self.factory.session_scope(write=True) as session:
            item = session.get(Conversation, conversation_id)
            if item and item.context_state == "cleanup_pending":
                item.context_state = "expired"

    def conversation_ids_for_application(self, application_id: str) -> list[str]:
        """API conversation ids bound to an application, for checkpoint cleanup."""
        with self.factory.session_scope() as session:
            return list(
                session.scalars(
                    select(Conversation.id).where(
                        Conversation.application_id == application_id,
                        Conversation.channel == "api",
                    )
                )
            )
