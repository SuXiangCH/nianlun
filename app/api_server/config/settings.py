"""Environment-backed settings for the HTTP service."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from nianlun.config import PROJECT_ROOT


def _csv(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


@dataclass(frozen=True)
class ProviderConfig:
    """Server-owned model settings selected by an application provider key."""

    model: str | None = None
    base_url: str | None = None
    api_key: str | None = None


@dataclass(frozen=True)
class ApiServerSettings:
    """Runtime configuration kept independent from FastAPI route code."""

    host: str = "127.0.0.1"
    port: int = 8000
    data_dir: Path = PROJECT_ROOT / "data" / "api_server"
    workspace_root: Path = PROJECT_ROOT / "data" / "workspaces"
    database_path: Path | None = None
    database_timeout_seconds: float = 30.0
    database_busy_timeout_ms: int = 5_000
    max_upload_bytes: int = 200 * 1024 * 1024
    mineru_poll_interval_seconds: float = 3.0
    mineru_poll_timeout_seconds: float = 60 * 60
    cors_origins: tuple[str, ...] = ("http://localhost:3000", "http://127.0.0.1:3000")
    environment: str = "development"
    log_level: str = "INFO"
    provider_configs: Mapping[str, ProviderConfig] = field(default_factory=dict)
    fts_enabled: bool = True
    milvus_uri: str | None = None
    milvus_token: str | None = None
    fts_collection: str | None = None
    fts_build_workers: int = 1
    vector_enabled: bool = False
    vector_collection: str | None = None
    vector_build_workers: int = 1
    embedding_model: str | None = None
    embedding_dim: int | None = None

    def __post_init__(self) -> None:
        if self.port < 1 or self.port > 65_535:
            raise ValueError("API port 必须在 1 到 65535 之间")
        if self.database_timeout_seconds <= 0:
            raise ValueError("数据库超时时间必须大于 0")
        if self.database_busy_timeout_ms < 0:
            raise ValueError("数据库 busy timeout 不能为负数")
        if self.max_upload_bytes <= 0:
            raise ValueError("上传大小限制必须大于 0")
        if self.mineru_poll_interval_seconds < 0:
            raise ValueError("MinerU 轮询间隔不能为负数")
        if self.mineru_poll_timeout_seconds <= 0:
            raise ValueError("MinerU 轮询超时时间必须大于 0")
        if self.fts_build_workers < 1:
            raise ValueError("FTS 构建 worker 数必须至少为 1")
        if self.vector_build_workers < 1:
            raise ValueError("向量索引构建 worker 数必须至少为 1")
        if self.embedding_dim is not None and self.embedding_dim <= 0:
            raise ValueError("embedding 维度必须是正整数")

    def resolve_provider(self, provider: str) -> ProviderConfig:
        if provider == "default":
            return ProviderConfig()
        try:
            return self.provider_configs[provider]
        except KeyError as exc:
            raise ValueError(f"provider 未配置: {provider}") from exc

    @classmethod
    def from_env(cls) -> ApiServerSettings:
        def parse_bool(name: str, default: bool) -> bool:
            value = os.environ.get(name)
            if value is None:
                return default
            normalized = value.strip().lower()
            if normalized in {"1", "true", "yes", "on"}:
                return True
            if normalized in {"0", "false", "no", "off"}:
                return False
            raise ValueError(f"{name} 必须是 true/false")

        data_dir = Path(
            os.environ.get("NIANLUN_API_DATA_DIR", str(cls.data_dir))
        ).expanduser()
        configured_database_path = os.environ.get("NIANLUN_API_DATABASE_PATH")
        provider_configs: dict[str, ProviderConfig] = {}
        for provider in _csv(os.environ.get("NIANLUN_API_PROVIDERS", "")):
            env_name = provider.upper().replace("-", "_")
            provider_configs[provider] = ProviderConfig(
                model=os.environ.get(f"NIANLUN_API_PROVIDER_{env_name}_MODEL"),
                base_url=os.environ.get(f"NIANLUN_API_PROVIDER_{env_name}_BASE_URL"),
                api_key=os.environ.get(f"NIANLUN_API_PROVIDER_{env_name}_API_KEY"),
            )
        return cls(
            host=os.environ.get("NIANLUN_API_HOST", cls.host),
            port=int(os.environ.get("NIANLUN_API_PORT", str(cls.port))),
            data_dir=data_dir,
            workspace_root=Path(
                os.environ.get("NIANLUN_WORKSPACE_ROOT", str(cls.workspace_root))
            ).expanduser(),
            database_path=(
                Path(configured_database_path).expanduser()
                if configured_database_path
                else data_dir / "nianlun.sqlite3"
            ),
            database_timeout_seconds=float(
                os.environ.get("NIANLUN_API_DB_TIMEOUT_SECONDS", "30")
            ),
            database_busy_timeout_ms=int(
                os.environ.get("NIANLUN_API_DB_BUSY_TIMEOUT_MS", "5000")
            ),
            max_upload_bytes=int(
                os.environ.get(
                    "NIANLUN_API_MAX_UPLOAD_BYTES", str(cls.max_upload_bytes)
                )
            ),
            mineru_poll_interval_seconds=float(
                os.environ.get(
                    "NIANLUN_API_MINERU_POLL_INTERVAL_SECONDS",
                    str(cls.mineru_poll_interval_seconds),
                )
            ),
            mineru_poll_timeout_seconds=float(
                os.environ.get(
                    "NIANLUN_API_MINERU_POLL_TIMEOUT_SECONDS",
                    str(cls.mineru_poll_timeout_seconds),
                )
            ),
            cors_origins=_csv(
                os.environ.get("NIANLUN_API_CORS_ORIGINS", ",".join(cls.cors_origins))
            ),
            environment=os.environ.get("NIANLUN_ENVIRONMENT", cls.environment),
            log_level=os.environ.get("NIANLUN_API_LOG_LEVEL", cls.log_level),
            provider_configs=provider_configs,
            fts_enabled=parse_bool("NIANLUN_API_FTS_ENABLED", cls.fts_enabled),
            milvus_uri=os.environ.get("NIANLUN_API_MILVUS_URI"),
            milvus_token=os.environ.get("NIANLUN_API_MILVUS_TOKEN"),
            fts_collection=os.environ.get("NIANLUN_API_FTS_COLLECTION"),
            fts_build_workers=int(
                os.environ.get(
                    "NIANLUN_API_FTS_BUILD_WORKERS", str(cls.fts_build_workers)
                )
            ),
            vector_enabled=parse_bool(
                "NIANLUN_API_VECTOR_ENABLED", cls.vector_enabled
            ),
            vector_collection=os.environ.get("NIANLUN_API_VECTOR_COLLECTION"),
            vector_build_workers=int(
                os.environ.get(
                    "NIANLUN_API_VECTOR_BUILD_WORKERS", str(cls.vector_build_workers)
                )
            ),
            embedding_model=os.environ.get("NIANLUN_API_EMBEDDING_MODEL"),
            embedding_dim=(
                int(os.environ["NIANLUN_API_EMBEDDING_DIM"])
                if os.environ.get("NIANLUN_API_EMBEDDING_DIM")
                else None
            ),
        )


def get_settings() -> ApiServerSettings:
    """Build settings for an application instance."""
    return ApiServerSettings.from_env()


__all__ = ["ApiServerSettings", "ProviderConfig", "get_settings"]
