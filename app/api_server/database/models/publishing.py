"""Application releases and write-only API credentials."""

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class ApplicationRelease(Base):
    __tablename__ = "application_releases"
    __table_args__ = (
        UniqueConstraint(
            "application_id", "sequence", name="uq_application_release_sequence"
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    application_id: Mapped[str] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"), index=True
    )
    sequence: Mapped[int] = mapped_column(Integer)
    draft_config_version: Mapped[int] = mapped_column(Integer)
    config_schema_version: Mapped[int] = mapped_column(Integer)
    config_json: Mapped[str] = mapped_column(Text)
    config_hash: Mapped[str] = mapped_column(String(64))
    description: Mapped[str] = mapped_column(Text, default="", server_default="")
    created_at: Mapped[str] = mapped_column(String(64))


class ApplicationDeployment(Base):
    __tablename__ = "application_deployments"

    application_id: Mapped[str] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"), primary_key=True
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    active_release_id: Mapped[str | None] = mapped_column(
        ForeignKey("application_releases.id", ondelete="SET NULL"), nullable=True
    )
    version: Mapped[int] = mapped_column(Integer, default=0)


class ApplicationDeploymentEvent(Base):
    __tablename__ = "application_deployment_events"
    __table_args__ = (
        UniqueConstraint(
            "application_id",
            "channel_type",
            "deployment_version",
            name="uq_application_deployment_channel_version",
        ),
        CheckConstraint(
            "channel_type IN ('api', 'wecom')",
            name="ck_application_deployment_channel_type",
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    application_id: Mapped[str] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"), index=True
    )
    channel_type: Mapped[str] = mapped_column(String(16), default="api")
    action: Mapped[str] = mapped_column(String(16))
    previous_release_id: Mapped[str | None] = mapped_column(String, nullable=True)
    release_id: Mapped[str | None] = mapped_column(String, nullable=True)
    previous_sequence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sequence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean)
    deployment_version: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[str] = mapped_column(String(64))


class ApplicationApiClient(Base):
    __tablename__ = "application_api_clients"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    application_id: Mapped[str] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(128))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[str] = mapped_column(String(64))
    updated_at: Mapped[str] = mapped_column(String(64))


class ApplicationApiKey(Base):
    __tablename__ = "application_api_keys"

    key_id: Mapped[str] = mapped_column(String, primary_key=True)
    client_id: Mapped[str] = mapped_column(
        ForeignKey("application_api_clients.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(128))
    secret_digest: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[str | None] = mapped_column(String(64), nullable=True)
    revoked_at: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[str] = mapped_column(String(64))


class ApplicationWecomChannel(Base):
    __tablename__ = "application_wecom_channels"
    __table_args__ = (
        UniqueConstraint("application_id", name="uq_application_wecom_application"),
        UniqueConstraint("bot_id", name="uq_application_wecom_bot"),
        CheckConstraint(
            "status IN ('offline', 'online', 'error')",
            name="ck_wecom_channel_status",
        ),
        CheckConstraint(
            "error_code IS NULL OR error_code IN "
            "('AUTH_FAILED', 'NETWORK_ERROR', 'CONNECTION_CONFLICT')",
            name="ck_wecom_channel_error_code",
        ),
        CheckConstraint(
            "status != 'error' OR error_code IS NOT NULL",
            name="ck_wecom_channel_error_required",
        ),
        CheckConstraint(
            "status = 'error' OR error_code IS NULL",
            name="ck_wecom_channel_error_forbidden",
        ),
        CheckConstraint(
            "config_revision >= 1 AND deployment_version >= 0",
            name="ck_wecom_channel_versions",
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    application_id: Mapped[str] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"), nullable=False
    )
    bot_id: Mapped[str] = mapped_column(String(256), nullable=False)
    secret: Mapped[str] = mapped_column(Text, nullable=False)
    default_response: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="暂时无法回答，请稍后再试。",
        server_default="暂时无法回答，请稍后再试。",
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    active_release_id: Mapped[str | None] = mapped_column(
        ForeignKey("application_releases.id", ondelete="SET NULL"), nullable=True
    )
    deployment_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    config_revision: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="offline", server_default="offline"
    )
    error_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    expires_at: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_reported_at: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[str] = mapped_column(String(64), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(64), nullable=False)


class ApplicationWecomBotRevision(Base):
    __tablename__ = "application_wecom_bot_revisions"

    bot_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    last_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[str] = mapped_column(String(64), nullable=False)
