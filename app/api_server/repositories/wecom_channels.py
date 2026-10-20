"""WeCom channel configuration, revision allocation and status persistence."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api_server.common.errors import ApiError
from app.api_server.database.connection import SQLiteConnectionFactory
from app.api_server.database.models import Application, ApplicationRelease
from app.api_server.database.models.publishing import (
    ApplicationWecomBotRevision,
    ApplicationWecomChannel,
)
from app.api_server.repositories.publishing import PublishingRepository


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _record(entity: Any) -> dict[str, Any]:
    return {
        column.name: getattr(entity, column.name) for column in entity.__table__.columns
    }


def _normalize_expires_at(value: str | None) -> str | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("expires_at 必须包含时区")
    return parsed.astimezone(timezone.utc).isoformat()


def _expired(expires_at: str | None, now: datetime) -> bool:
    return expires_at is not None and datetime.fromisoformat(expires_at) <= now


def _next_revision(session: Session, bot_id: str, *, after: int = 0) -> int:
    timestamp = _now()
    row = session.get(ApplicationWecomBotRevision, bot_id)
    # Rebinding must fence both the channel's old requests and the bot's history.
    revision = max(row.last_revision if row is not None else 0, after) + 1
    if row is None:
        row = ApplicationWecomBotRevision(
            bot_id=bot_id, last_revision=revision, updated_at=timestamp
        )
        session.add(row)
    else:
        row.last_revision = revision
        row.updated_at = timestamp
    return revision


class WecomChannelRepository:
    def __init__(self, factory: SQLiteConnectionFactory) -> None:
        self.factory = factory

    @staticmethod
    def _application(session: Session, app_id: str) -> Application:
        item = session.get(Application, app_id)
        if item is None:
            raise ApiError("应用不存在", 404)
        return item

    @staticmethod
    def _channel(session: Session, app_id: str) -> ApplicationWecomChannel:
        item = session.scalar(
            select(ApplicationWecomChannel).where(
                ApplicationWecomChannel.application_id == app_id
            )
        )
        if item is None:
            raise ApiError("企业微信渠道尚未创建", 404)
        return item

    @staticmethod
    def _check_bot_available(
        session: Session, bot_id: str, *, exclude_channel_id: str | None = None
    ) -> None:
        statement = select(ApplicationWecomChannel.id).where(
            ApplicationWecomChannel.bot_id == bot_id
        )
        if exclude_channel_id is not None:
            statement = statement.where(
                ApplicationWecomChannel.id != exclude_channel_id
            )
        if session.scalar(statement.limit(1)) is not None:
            raise ApiError("该机器人已绑定其他应用", 409)

    def create(
        self,
        app_id: str,
        *,
        bot_id: str,
        secret: str,
        default_response: str,
        expires_at: str | None,
    ) -> dict[str, Any]:
        with self.factory.session_scope(write=True) as session:
            self._application(session, app_id)
            if (
                session.scalar(
                    select(ApplicationWecomChannel.id)
                    .where(ApplicationWecomChannel.application_id == app_id)
                    .limit(1)
                )
                is not None
            ):
                raise ApiError("企业微信渠道已创建", 409)
            self._check_bot_available(session, bot_id)
            timestamp = _now()
            item = ApplicationWecomChannel(
                id=uuid.uuid4().hex,
                application_id=app_id,
                bot_id=bot_id,
                secret=secret,
                default_response=default_response,
                enabled=False,
                active_release_id=None,
                deployment_version=0,
                config_revision=_next_revision(session, bot_id),
                status="offline",
                error_code=None,
                expires_at=_normalize_expires_at(expires_at),
                last_reported_at=None,
                created_at=timestamp,
                updated_at=timestamp,
            )
            session.add(item)
            session.flush()
            return _record(item)

    def get(self, app_id: str) -> dict[str, Any] | None:
        with self.factory.session_scope() as session:
            item = session.scalar(
                select(ApplicationWecomChannel).where(
                    ApplicationWecomChannel.application_id == app_id
                )
            )
            return _record(item) if item is not None else None

    def by_channel_id(self, channel_id: str) -> dict[str, Any] | None:
        with self.factory.session_scope() as session:
            item = session.get(ApplicationWecomChannel, channel_id)
            return _record(item) if item is not None else None

    def update(
        self,
        app_id: str,
        values: dict[str, Any],
        *,
        expected_config_revision: int,
    ) -> dict[str, Any]:
        with self.factory.session_scope(write=True) as session:
            item = self._channel(session, app_id)
            if item.config_revision != expected_config_revision:
                raise ApiError("渠道配置已变化，请刷新后重试", 409)
            if "bot_id" in values:
                bot_id = values["bot_id"]
                if not bot_id.strip():
                    raise ApiError("机器人 ID 不能为空", 422)
                self._check_bot_available(session, bot_id, exclude_channel_id=item.id)
            if "expires_at" in values:
                values = {
                    **values,
                    "expires_at": _normalize_expires_at(values["expires_at"]),
                }
            target_bot_id = values.get("bot_id", item.bot_id)
            item.config_revision = _next_revision(
                session, target_bot_id, after=item.config_revision
            )
            for key, value in values.items():
                setattr(item, key, value)
            item.status = "offline"
            item.error_code = None
            item.updated_at = _now()
            session.flush()
            return _record(item)

    def set_enabled(
        self,
        app_id: str,
        enabled: bool,
        expected_deployment_version: int,
    ) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        with self.factory.session_scope(write=True) as session:
            item = self._channel(session, app_id)
            if item.deployment_version != expected_deployment_version:
                raise ApiError("发布状态已变化，请刷新后重试", 409)
            if enabled:
                if item.active_release_id is None:
                    raise ApiError("应用尚未发布", 409)
                if _expired(item.expires_at, now):
                    raise ApiError("企业微信渠道已过期，请更新有效期后重试", 409)
            item.enabled = enabled
            item.deployment_version += 1
            item.config_revision = _next_revision(
                session, item.bot_id, after=item.config_revision
            )
            item.status = "offline"
            item.error_code = None
            item.updated_at = now.isoformat()
            current = (
                session.get(ApplicationRelease, item.active_release_id)
                if item.active_release_id
                else None
            )
            PublishingRepository.record_deployment_event(
                session,
                item,
                current,
                current,
                "enable" if enabled else "disable",
                channel_type="wecom",
                deployment_version=item.deployment_version,
            )
            session.flush()
            return _record(item)

    def deploy(
        self,
        app_id: str,
        release_id: str,
        *,
        expected_deployment_version: int,
        content_version: int,
    ) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        with self.factory.session_scope(write=True) as session:
            item = self._channel(session, app_id)
            release = session.get(ApplicationRelease, release_id)
            if release is None or release.application_id != app_id:
                raise ApiError("发布版本不存在", 404)
            if item.deployment_version != expected_deployment_version:
                raise ApiError("发布状态已变化，请刷新后重试", 409)
            if _expired(item.expires_at, now):
                raise ApiError("企业微信渠道已过期，请更新有效期后重试", 409)
            if content_version < 1:
                raise ApiError("知识库 snapshot 尚未就绪", 503)
            if item.active_release_id == release.id:
                return _record(item)
            previous = (
                session.get(ApplicationRelease, item.active_release_id)
                if item.active_release_id
                else None
            )
            if item.active_release_id is None:
                item.enabled = True
                item.config_revision = _next_revision(
                    session, item.bot_id, after=item.config_revision
                )
            item.active_release_id = release.id
            item.deployment_version += 1
            item.updated_at = now.isoformat()
            PublishingRepository.record_deployment_event(
                session,
                item,
                previous,
                release,
                "deploy",
                channel_type="wecom",
                deployment_version=item.deployment_version,
            )
            session.flush()
            return _record(item)

    def snapshot(self) -> list[dict[str, Any]]:
        with self.factory.session_scope() as session:
            items = session.scalars(
                select(ApplicationWecomChannel)
                .where(
                    ApplicationWecomChannel.enabled.is_(True),
                    ApplicationWecomChannel.active_release_id.is_not(None),
                )
                .order_by(
                    ApplicationWecomChannel.created_at, ApplicationWecomChannel.id
                )
            )
            return [
                {
                    "channel_id": item.id,
                    "application_id": item.application_id,
                    "revision": item.config_revision,
                    "enabled": True,
                    "connection": {"bot_id": item.bot_id, "secret": item.secret},
                    "runtime": {
                        "default_response": item.default_response,
                        "expires_at": item.expires_at,
                    },
                }
                for item in items
            ]

    def report_status(
        self,
        channel_id: str,
        revision: int,
        status: str,
        error_code: str | None,
    ) -> bool:
        now = datetime.now(timezone.utc)
        with self.factory.session_scope(write=True) as session:
            item = session.get(ApplicationWecomChannel, channel_id)
            if item is None or item.config_revision != revision:
                return False
            if _expired(item.expires_at, now) and status != "offline":
                return False
            if status == "error":
                if error_code not in {
                    "AUTH_FAILED",
                    "NETWORK_ERROR",
                    "CONNECTION_CONFLICT",
                }:
                    return False
            else:
                error_code = None
            item.status = status
            item.error_code = error_code
            item.last_reported_at = now.isoformat()
            return True

    def last_deployed_at(self, app_id: str) -> datetime | None:
        return PublishingRepository(self.factory).last_deployed_at(
            app_id, channel_type="wecom"
        )
