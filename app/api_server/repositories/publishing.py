"""Short atomic transactions for releases, deployments and credentials."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.api_server.apis.v1.publishing_schemas import PublishedConfig
from app.api_server.common.errors import ApiError
from app.api_server.database.connection import SQLiteConnectionFactory
from app.api_server.database.models import (
    Application,
    ModelProfile,
    KnowledgeBaseWorkspaceRevision,
    Conversation,
)
from app.api_server.database.models.application_runs import ApplicationRun
from app.api_server.database.models.publishing import (
    ApplicationApiClient,
    ApplicationApiKey,
    ApplicationDeployment,
    ApplicationDeploymentEvent,
    ApplicationRelease,
    ApplicationWecomChannel,
)


def _record(entity: Any) -> dict[str, Any]:
    return {
        column.name: getattr(entity, column.name) for column in entity.__table__.columns
    }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class PublishingRepository:
    def __init__(self, factory: SQLiteConnectionFactory) -> None:
        self.factory = factory

    @staticmethod
    def live_release_ids(session: Session) -> set[str]:
        """References whose execution dependencies still have to remain available."""
        enabled = session.scalars(
            select(ApplicationDeployment.active_release_id).where(
                ApplicationDeployment.enabled.is_(True),
                ApplicationDeployment.active_release_id.is_not(None),
            )
        )
        enabled_wecom = session.scalars(
            select(ApplicationWecomChannel.active_release_id).where(
                ApplicationWecomChannel.enabled.is_(True),
                ApplicationWecomChannel.active_release_id.is_not(None),
            )
        )
        retained = session.scalars(
            select(Conversation.release_id)
            .where(
                Conversation.channel.in_(("api", "wecom")),
                Conversation.status != "deleted",
                or_(
                    Conversation.expires_at.is_(None), Conversation.expires_at > _now()
                ),
                Conversation.release_id.is_not(None),
            )
            .distinct()
        )
        running = session.scalars(
            select(ApplicationRun.release_id)
            .where(
                ApplicationRun.status.in_(["running", "cancelling"]),
            )
            .distinct()
        )
        return {
            release_id
            for release_id in [*enabled, *enabled_wecom, *retained, *running]
            if release_id is not None
        }

    @staticmethod
    def count_live_resource_references(
        session: Session, resource: str, resource_id: str
    ) -> int:
        if resource not in {"llm_model_id", "knowledge_base_id"}:
            raise ValueError("unsupported release resource")
        live = PublishingRepository.live_release_ids(session)
        if not live:
            return 0
        return int(
            session.scalar(
                select(func.count(ApplicationRelease.id)).where(
                    ApplicationRelease.id.in_(live),
                    func.json_extract(ApplicationRelease.config_json, f"$.{resource}")
                    == resource_id,
                )
            )
            or 0
        )

    @staticmethod
    def assert_application_deletable(session: Session, app_id: str) -> None:
        deployment = session.get(ApplicationDeployment, app_id)
        if deployment is not None and deployment.enabled:
            raise ApiError("应用 API 已启用，请先停用后删除", 409)
        wecom_enabled = session.scalar(
            select(ApplicationWecomChannel.id)
            .where(
                ApplicationWecomChannel.application_id == app_id,
                ApplicationWecomChannel.enabled.is_(True),
            )
            .limit(1)
        )
        if wecom_enabled is not None:
            raise ApiError("企业微信渠道已启用，请先停用后删除", 409)
        retained = session.scalar(
            select(Conversation.id)
            .where(
                Conversation.application_id == app_id,
                Conversation.channel.in_(("api", "wecom")),
                Conversation.status != "deleted",
                or_(
                    Conversation.expires_at.is_(None), Conversation.expires_at > _now()
                ),
            )
            .limit(1)
        )
        if retained is not None:
            raise ApiError("应用仍有未过期会话，暂不能删除", 409)
        running = session.scalar(
            select(ApplicationRun.id)
            .where(
                ApplicationRun.application_id == app_id,
                ApplicationRun.status.in_(["running", "cancelling"]),
            )
            .limit(1)
        )
        if running is not None:
            raise ApiError("应用仍有执行中的调用，暂不能删除", 409)

    @staticmethod
    def _application(session: Session, app_id: str) -> Application:
        item = session.get(Application, app_id)
        if item is None:
            raise ApiError("应用不存在", 404)
        return item

    @staticmethod
    def _client(session: Session, app_id: str, client_id: str) -> ApplicationApiClient:
        item = session.get(ApplicationApiClient, client_id)
        if item is None or item.application_id != app_id:
            raise ApiError("调用客户端不存在", 404)
        return item

    @staticmethod
    def _verify_config(
        session: Session, config: PublishedConfig, *, draft: bool
    ) -> None:
        app = PublishingRepository._application(session, config.application_id)
        if draft and (
            app.knowledge_base_id != config.knowledge_base_id
            or app.agent_version != config.agent_version
            or app.provider != config.provider
            or app.clarification_enabled != config.clarification_enabled
            or (app.model is not None and app.model != config.llm_model_id)
        ):
            raise ApiError("应用配置已变化，请刷新后重试", 409)
        profile = session.get(ModelProfile, config.llm_model_id)
        llm = profile.llm_config if profile is not None else None
        if profile is not None and not profile.api_key:
            raise ApiError("模型调用凭据未配置", 503)
        if (
            profile is None
            or profile.kind != "llm"
            or llm is None
            or profile.base_url != config.base_url
            or llm.model != config.model
            or (llm.context_window_tokens or 128_000) != config.context_window_tokens
        ):
            raise ApiError("发布版本与模型配置不匹配，请重新发布", 409)
        if draft and app.model is None and not profile.is_default:
            raise ApiError("默认模型已变化，请刷新后重试", 409)
        revision = session.scalar(
            select(KnowledgeBaseWorkspaceRevision.id).where(
                KnowledgeBaseWorkspaceRevision.knowledge_base_id
                == config.knowledge_base_id,
                KnowledgeBaseWorkspaceRevision.state == "committed",
            )
        )
        if revision is None:
            raise ApiError("知识库 committed snapshot 尚未就绪", 503)

    def publication(
        self, app_id: str
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        with self.factory.session_scope() as session:
            self._application(session, app_id)
            deployment = session.get(ApplicationDeployment, app_id)
            release = (
                session.get(ApplicationRelease, deployment.active_release_id)
                if deployment and deployment.active_release_id
                else None
            )
            return (
                _record(deployment) if deployment else None,
                _record(release) if release else None,
            )

    def release(self, app_id: str, release_id: str) -> dict[str, Any]:
        with self.factory.session_scope() as session:
            item = session.get(ApplicationRelease, release_id)
            if item is None or item.application_id != app_id:
                raise ApiError("发布版本不存在", 404)
            return _record(item)

    def releases(
        self, app_id: str, limit: int, offset: int
    ) -> tuple[list[dict[str, Any]], int]:
        with self.factory.session_scope() as session:
            self._application(session, app_id)
            statement = select(ApplicationRelease).where(
                ApplicationRelease.application_id == app_id
            )
            count = session.scalar(
                select(func.count()).select_from(statement.subquery())
            )
            items = session.scalars(
                statement.order_by(ApplicationRelease.sequence.desc())
                .limit(limit)
                .offset(offset)
            )
            return [_record(item) for item in items], int(count or 0)

    def create_api_channel(self, app_id: str) -> None:
        with self.factory.session_scope(write=True) as session:
            self._application(session, app_id)
            if session.get(ApplicationDeployment, app_id) is not None:
                raise ApiError("API 渠道已创建", 409)
            session.add(
                ApplicationDeployment(
                    application_id=app_id,
                    enabled=False,
                    active_release_id=None,
                    version=0,
                )
            )

    @staticmethod
    def _insert_release(
        session: Session,
        app: Application,
        config_json: str,
        config_hash: str,
        description: str = "",
    ) -> ApplicationRelease:
        sequence = (
            int(
                session.scalar(
                    select(func.max(ApplicationRelease.sequence)).where(
                        ApplicationRelease.application_id == app.id
                    )
                )
                or 0
            )
            + 1
        )
        release = ApplicationRelease(
            id=str(uuid.uuid4()),
            application_id=app.id,
            sequence=sequence,
            draft_config_version=app.config_version,
            config_schema_version=1,
            config_json=config_json,
            config_hash=config_hash,
            description=description,
            created_at=_now(),
        )
        session.add(release)
        session.flush()
        return release

    @staticmethod
    def record_deployment_event(
        session: Session,
        deployment: Any,
        previous: ApplicationRelease | None,
        release: ApplicationRelease | None,
        action: str,
        *,
        channel_type: str = "api",
        deployment_version: int | None = None,
    ) -> None:
        session.add(
            ApplicationDeploymentEvent(
                id=str(uuid.uuid4()),
                application_id=deployment.application_id,
                channel_type=channel_type,
                action=action,
                previous_release_id=previous.id if previous else None,
                release_id=release.id if release else None,
                previous_sequence=previous.sequence if previous else None,
                sequence=release.sequence if release else None,
                enabled=deployment.enabled,
                deployment_version=(
                    deployment_version
                    if deployment_version is not None
                    else deployment.version
                ),
                created_at=_now(),
            )
        )

    def create_release(
        self,
        config: PublishedConfig,
        config_json: str,
        config_hash: str,
        expected_config_version: int,
        description: str,
    ) -> dict[str, Any]:
        with self.factory.session_scope(write=True) as session:
            app = self._application(session, config.application_id)
            if app.config_version != expected_config_version:
                raise ApiError("应用配置已变化，请刷新后重试", 409)
            self._verify_config(session, config, draft=True)
            latest = session.scalar(
                select(ApplicationRelease)
                .where(ApplicationRelease.application_id == app.id)
                .order_by(ApplicationRelease.sequence.desc())
                .limit(1)
            )
            if (
                latest is not None
                and latest.config_hash == config_hash
                and latest.description == description
            ):
                return _record(latest)
            return _record(
                self._insert_release(
                    session, app, config_json, config_hash, description
                )
            )

    def deploy(
        self,
        app_id: str,
        config: PublishedConfig,
        release_id: str,
        expected_version: int,
    ) -> None:
        with self.factory.session_scope(write=True) as session:
            self._application(session, app_id)
            release = session.get(ApplicationRelease, release_id)
            if release is None or release.application_id != app_id:
                raise ApiError("发布版本不存在", 404)
            deployment = session.get(ApplicationDeployment, app_id)
            if deployment is None:
                raise ApiError("请先创建 API 发布渠道", 409)
            if deployment.version != expected_version:
                raise ApiError("发布状态已变化，请刷新后重试", 409)
            if PublishedConfig.model_validate_json(release.config_json) != config:
                raise ApiError("发布配置已变化，请刷新后重试", 409)
            self._verify_config(session, config, draft=False)
            if deployment.active_release_id == release.id:
                return
            previous = (
                session.get(ApplicationRelease, deployment.active_release_id)
                if deployment.active_release_id
                else None
            )
            if deployment.active_release_id is None:
                deployment.enabled = True
            deployment.active_release_id = release.id
            deployment.version += 1
            self.record_deployment_event(
                session, deployment, previous, release, "deploy"
            )

    def deployment_events(
        self,
        app_id: str,
        limit: int,
        offset: int,
        *,
        channel_type: str = "api",
    ) -> tuple[list[dict[str, Any]], int]:
        with self.factory.session_scope() as session:
            self._application(session, app_id)
            statement = select(ApplicationDeploymentEvent).where(
                ApplicationDeploymentEvent.application_id == app_id,
                ApplicationDeploymentEvent.channel_type == channel_type,
            )
            total = int(
                session.scalar(select(func.count()).select_from(statement.subquery()))
                or 0
            )
            items = session.scalars(
                statement.order_by(ApplicationDeploymentEvent.deployment_version.desc())
                .limit(limit)
                .offset(offset)
            )
            return [_record(item) for item in items], total

    def last_deployed_at(
        self, app_id: str, *, channel_type: str = "api"
    ) -> datetime | None:
        with self.factory.session_scope() as session:
            value = session.scalar(
                select(ApplicationDeploymentEvent.created_at)
                .where(
                    ApplicationDeploymentEvent.application_id == app_id,
                    ApplicationDeploymentEvent.channel_type == channel_type,
                    ApplicationDeploymentEvent.action == "deploy",
                )
                .order_by(ApplicationDeploymentEvent.deployment_version.desc())
                .limit(1)
            )
            return datetime.fromisoformat(value) if value else None

    def publish(
        self,
        config: PublishedConfig,
        config_json: str,
        config_hash: str,
        expected_config_version: int,
        expected_deployment_version: int,
    ) -> None:
        with self.factory.session_scope(write=True) as session:
            app = self._application(session, config.application_id)
            deployment = session.get(ApplicationDeployment, app.id)
            if (
                app.config_version != expected_config_version
                or (deployment.version if deployment else 0)
                != expected_deployment_version
            ):
                raise ApiError("应用或发布状态已变化，请刷新后重试", 409)
            self._verify_config(session, config, draft=True)
            previous = (
                session.get(ApplicationRelease, deployment.active_release_id)
                if deployment and deployment.active_release_id
                else None
            )
            if previous is not None and previous.config_hash == config_hash:
                return
            release = self._insert_release(session, app, config_json, config_hash)
            if deployment is None:
                deployment = ApplicationDeployment(
                    application_id=app.id, enabled=True, version=0
                )
                session.add(deployment)
            elif deployment.active_release_id is None:
                deployment.enabled = True
            deployment.active_release_id = release.id
            deployment.version += 1
            self.record_deployment_event(
                session, deployment, previous, release, "deploy"
            )

    def set_enabled(
        self,
        app_id: str,
        enabled: bool,
        expected_version: int,
        config: PublishedConfig | None,
    ) -> None:
        with self.factory.session_scope(write=True) as session:
            self._application(session, app_id)
            deployment = session.get(ApplicationDeployment, app_id)
            if deployment is None or deployment.version != expected_version:
                raise ApiError("发布状态已变化，请刷新后重试", 409)
            if enabled:
                if config is None or not deployment.active_release_id:
                    raise ApiError("应用尚未发布", 409)
                self._verify_config(session, config, draft=False)
                active = session.get(ApplicationRelease, deployment.active_release_id)
                if (
                    active is None
                    or PublishedConfig.model_validate_json(active.config_json) != config
                ):
                    raise ApiError("发布版本已变化，请刷新后重试", 409)
            deployment.enabled = enabled
            deployment.version += 1
            current = (
                session.get(ApplicationRelease, deployment.active_release_id)
                if deployment.active_release_id
                else None
            )
            self.record_deployment_event(
                session,
                deployment,
                current,
                current,
                "enable" if enabled else "disable",
            )

    def update_application(
        self, app_id: str, values: dict[str, Any], expected_version: int
    ) -> dict[str, Any]:
        with self.factory.session_scope(write=True) as session:
            app = self._application(session, app_id)
            if app.config_version != expected_version:
                raise ApiError("应用配置已变化，请刷新后重试", 409)
            if "model" in values:
                profile = session.get(ModelProfile, values["model"])
                if profile is None or profile.kind != "llm":
                    raise ApiError("所选模型已变化，请刷新后重试", 409)
            if "knowledge_base_id" in values:
                revision = session.scalar(
                    select(KnowledgeBaseWorkspaceRevision.id).where(
                        KnowledgeBaseWorkspaceRevision.knowledge_base_id
                        == values["knowledge_base_id"],
                        KnowledgeBaseWorkspaceRevision.state == "committed",
                    )
                )
                if revision is None:
                    raise ApiError("知识库 committed snapshot 尚未就绪", 409)
            execution_fields = {
                "knowledge_base_id",
                "model",
                "provider",
                "agent_version",
                "clarification_enabled",
            }
            changed = any(
                getattr(app, key) != value
                for key, value in values.items()
                if key in execution_fields
            )
            for key, value in values.items():
                setattr(app, key, value)
            if changed:
                app.config_version += 1
            app.updated_at = _now()
            session.flush()
            return _record(app)

    def create_client(self, app_id: str, name: str) -> dict[str, Any]:
        with self.factory.session_scope(write=True) as session:
            self._application(session, app_id)
            now = _now()
            item = ApplicationApiClient(
                id=str(uuid.uuid4()),
                application_id=app_id,
                name=name,
                enabled=True,
                created_at=now,
                updated_at=now,
            )
            session.add(item)
            session.flush()
            return _record(item)

    def list_clients(self, app_id: str) -> list[dict[str, Any]]:
        with self.factory.session_scope() as session:
            self._application(session, app_id)
            return [
                _record(item)
                for item in session.scalars(
                    select(ApplicationApiClient)
                    .where(ApplicationApiClient.application_id == app_id)
                    .order_by(ApplicationApiClient.created_at)
                )
            ]

    def update_client(
        self, app_id: str, client_id: str, values: dict[str, Any]
    ) -> dict[str, Any]:
        with self.factory.session_scope(write=True) as session:
            item = self._client(session, app_id, client_id)
            for key, value in values.items():
                setattr(item, key, value)
            item.updated_at = _now()
            session.flush()
            return _record(item)

    def register_key(
        self,
        app_id: str,
        client_id: str,
        key_id: str,
        digest: str,
        name: str,
        expires_at: str | None,
    ) -> dict[str, Any]:
        with self.factory.session_scope(write=True) as session:
            self._client(session, app_id, client_id)
            if session.get(ApplicationApiKey, key_id) is not None:
                raise ApiError("Key ID 已被注册", 409)
            item = ApplicationApiKey(
                key_id=key_id,
                client_id=client_id,
                secret_digest=digest,
                name=name,
                expires_at=expires_at,
                revoked_at=None,
                created_at=_now(),
            )
            session.add(item)
            session.flush()
            return _record(item)

    def list_keys(self, app_id: str, client_id: str) -> list[dict[str, Any]]:
        with self.factory.session_scope() as session:
            self._client(session, app_id, client_id)
            return [
                _record(item)
                for item in session.scalars(
                    select(ApplicationApiKey)
                    .where(ApplicationApiKey.client_id == client_id)
                    .order_by(ApplicationApiKey.created_at)
                )
            ]

    def revoke_key(self, app_id: str, client_id: str, key_id: str) -> dict[str, Any]:
        with self.factory.session_scope(write=True) as session:
            self._client(session, app_id, client_id)
            item = session.get(ApplicationApiKey, key_id)
            if item is None or item.client_id != client_id:
                raise ApiError("API Key 不存在", 404)
            if item.revoked_at is None:
                item.revoked_at = _now()
            session.flush()
            return _record(item)

    def credential(
        self, key_id: str
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any] | None] | None:
        with self.factory.session_scope() as session:
            key = session.get(ApplicationApiKey, key_id)
            client = session.get(ApplicationApiClient, key.client_id) if key else None
            if key is None or client is None:
                return None
            deployment = session.get(ApplicationDeployment, client.application_id)
            return (
                _record(key),
                _record(client),
                _record(deployment) if deployment else None,
            )
