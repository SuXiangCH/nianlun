"""Application releases and write-only API credentials."""

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text, UniqueConstraint
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
    __table_args__ = (UniqueConstraint("application_id", "deployment_version"),)

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
