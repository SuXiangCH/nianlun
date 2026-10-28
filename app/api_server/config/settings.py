"""Environment-backed settings for the HTTP service."""

from __future__ import annotations

import os
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit
from typing import Literal

from pydantic import SecretStr

from nianlun.config import PROJECT_ROOT
from nianlun.agent.contracts import AnswerImagePolicy


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
    mineru_submit_workers: int = 4
    mineru_poll_workers: int = 4
    enrich_workers: int = 4
    llm_max_concurrent: int = 8
    llm_per_document_concurrent: int = 4
    llm_request_timeout_seconds: float = 600.0
    pdf_chunk_max_pages: int = 180
    pdf_chunk_overlap_pages: int = 0
    pdf_max_chunks: int = 100
    document_staging_max_bytes: int = 2 * 1024 * 1024 * 1024
    mineru_zip_max_entries: int = 10_000
    mineru_zip_max_compression_ratio: float = 200.0
    pipeline_lease_seconds: int = 5 * 60
    snapshot_min_retention_seconds: int = 24 * 60 * 60
    staging_orphan_retention_seconds: int = 24 * 60 * 60
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
    application_api_enabled: bool = False
    application_api_max_concurrent: int = 8
    application_api_app_concurrent: int = 4
    application_api_client_concurrent: int = 2
    application_api_requests_per_minute: int = 60
    application_api_timeout_seconds: float = 300.0
    application_api_retention_days: int = 30
    application_api_result_retention_days: int = 30
    application_api_public_base_url: str | None = None
    answer_images_enabled: bool = True
    answer_image_storage_backend: Literal["filesystem", "s3"] = "filesystem"
    answer_image_storage_profiles: Mapping = field(default_factory=dict, repr=False)
    answer_image_storage_profile_id: str | None = None
    answer_image_s3_endpoint_url: str | None = None
    answer_image_s3_bucket: str = ""
    answer_image_s3_region: str = ""
    answer_image_s3_addressing_style: Literal["auto", "path", "virtual"] = "auto"
    answer_image_s3_access_key_id: SecretStr | None = field(default=None, repr=False)
    answer_image_s3_secret_access_key: SecretStr | None = field(
        default=None, repr=False
    )
    answer_image_s3_session_token: SecretStr | None = field(default=None, repr=False)
    answer_image_s3_ca_bundle: str | None = None
    answer_image_s3_connect_timeout_seconds: float = 5.0
    answer_image_s3_read_timeout_seconds: float = 30.0
    answer_image_s3_total_max_attempts: int = 3
    answer_image_orphan_grace_hours: int = 24
    answer_image_grant_hours: int = 24
    answer_image_max_image_bytes: int = 8 * 1024 * 1024
    answer_image_max_image_pixels: int = 40_000_000
    answer_image_max_candidates: int = 64
    answer_image_max_answer_images: int = 32
    answer_image_max_answer_bytes: int = 128 * 1024 * 1024
    answer_image_max_preview_images: int = 8
    answer_image_max_preview_bytes: int = 32 * 1024 * 1024
    answer_image_max_occurrences: int = 128
    answer_image_max_candidate_line_bytes: int = 4 * 1024
    answer_image_max_canonical_bytes: int = 1024 * 1024
    answer_image_inline_max_bytes: int = 1024 * 1024
    answer_image_inline_total_bytes: int = 2 * 1024 * 1024
    answer_image_inline_response_max_bytes: int = 8 * 1024 * 1024
    internal_token: str | None = None
    gateway_control_urls: tuple[str, ...] = ()
    gateway_control_token: str | None = None
    gateway_control_timeout_seconds: float = 2.0
    wecom_max_concurrent: int = 32
    wecom_run_timeout_seconds: float = 120.0
    wecom_conversation_retention_days: int = 30

    def __post_init__(self) -> None:
        from app.api_server.storage.image_storage_profiles import normalize_profiles

        selected, profiles = normalize_profiles(self)
        object.__setattr__(self, "answer_image_storage_profile_id", selected)
        object.__setattr__(self, "answer_image_storage_profiles", profiles)
        self.freeze_image_policy()
        api_limits = (
            self.application_api_max_concurrent,
            self.application_api_app_concurrent,
            self.application_api_client_concurrent,
            self.application_api_requests_per_minute,
            self.application_api_retention_days,
            self.application_api_timeout_seconds,
        )
        if any(value <= 0 for value in api_limits):
            raise ValueError("应用 API 限额、保留期与超时时间必须大于 0")
        if any(
            value <= 0
            for value in (
                self.application_api_result_retention_days,
                self.answer_image_grant_hours,
                self.answer_image_orphan_grace_hours,
                self.answer_image_inline_max_bytes,
                self.answer_image_inline_total_bytes,
                self.answer_image_inline_response_max_bytes,
            )
        ):
            raise ValueError("图片限额、授权和结果保留期必须大于 0")
        if self.application_api_public_base_url is not None:
            parsed = urlsplit(self.application_api_public_base_url)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.query
                or parsed.fragment
                or self.application_api_public_base_url
                != self.application_api_public_base_url.strip()
            ):
                raise ValueError(
                    "应用 API 公开地址必须是无凭据、查询参数和片段的 HTTP(S) URL"
                )
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
        if self.mineru_submit_workers < 1 or self.mineru_poll_workers < 1:
            raise ValueError("MinerU worker 数必须至少为 1")
        if self.enrich_workers < 1:
            raise ValueError("增强 worker 数必须至少为 1")
        if self.llm_max_concurrent < 1:
            raise ValueError("LLM 总并发必须至少为 1")
        if not 1 <= self.llm_per_document_concurrent <= self.llm_max_concurrent:
            raise ValueError("单文档 LLM 并发必须在 1 到总并发之间")
        if self.llm_request_timeout_seconds <= 0:
            raise ValueError("LLM 请求超时时间必须大于 0")
        if self.pdf_chunk_max_pages < 1:
            raise ValueError("PDF 分段页数必须至少为 1")
        if self.pdf_chunk_overlap_pages != 0:
            raise ValueError("当前版本尚不支持 PDF 重叠分段，重叠页数必须为 0")
        if self.pdf_max_chunks < 1:
            raise ValueError("PDF 最大分段数必须至少为 1")
        if self.document_staging_max_bytes < 1:
            raise ValueError("文档 staging 配额必须大于 0")
        if self.mineru_zip_max_entries < 1:
            raise ValueError("MinerU ZIP 文件项限制必须至少为 1")
        if self.mineru_zip_max_compression_ratio <= 1:
            raise ValueError("MinerU ZIP 压缩比限制必须大于 1")
        if self.pipeline_lease_seconds < 1:
            raise ValueError("流水线租约必须大于 0 秒")
        if self.snapshot_min_retention_seconds < 1:
            raise ValueError("snapshot 保留期必须大于 0 秒")
        if self.staging_orphan_retention_seconds < 1:
            raise ValueError("staging orphan 保留期必须大于 0 秒")
        if self.fts_build_workers < 1:
            raise ValueError("FTS 构建 worker 数必须至少为 1")
        if self.vector_build_workers < 1:
            raise ValueError("向量索引构建 worker 数必须至少为 1")
        if self.embedding_dim is not None and self.embedding_dim <= 0:
            raise ValueError("embedding 维度必须是正整数")
        if self.internal_token is not None and (
            len(self.internal_token) < 32
            or self.internal_token != self.internal_token.strip()
        ):
            raise ValueError(
                "NIANLUN_INTERNAL_TOKEN 长度必须至少为 32 且不能包含首尾空白"
            )
        if self.gateway_control_urls:
            if (
                self.gateway_control_token is None
                or len(self.gateway_control_token) < 32
            ):
                raise ValueError("配置网关控制地址时必须设置至少 32 字符的控制令牌")
            if self.gateway_control_token != self.gateway_control_token.strip():
                raise ValueError("NIANLUN_GATEWAY_CONTROL_TOKEN 不能包含首尾空白")
            for url in self.gateway_control_urls:
                parsed = urlsplit(url)
                if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                    raise ValueError("网关控制地址必须是 http 或 https URL")
                if parsed.username is not None or parsed.password is not None:
                    raise ValueError("网关控制地址不能包含用户名或密码")
        if self.gateway_control_timeout_seconds <= 0:
            raise ValueError("网关控制超时时间必须大于 0")
        if (
            self.wecom_max_concurrent <= 0
            or self.wecom_run_timeout_seconds <= 0
            or self.wecom_conversation_retention_days <= 0
        ):
            raise ValueError("企业微信并发、超时和保留期必须大于 0")

    def freeze_image_policy(
        self, *, transport: Literal["url", "data_url"] = "url"
    ) -> AnswerImagePolicy:
        return AnswerImagePolicy(
            **{
                name: getattr(self, "answer_image_" + name)
                for name in AnswerImagePolicy.__dataclass_fields__
                if name != "inline_budget_enabled"
            },
            inline_budget_enabled=transport == "data_url",
        )

    def resolve_provider(self, provider: str) -> ProviderConfig:
        if provider == "default":
            return ProviderConfig()
        try:
            return self.provider_configs[provider]
        except KeyError as exc:
            raise ValueError(f"provider 未配置: {provider}") from exc

    @classmethod
    def from_env(cls) -> ApiServerSettings:
        def image_storage_env() -> dict:
            prefix = "NIANLUN_API_ANSWER_IMAGE_"
            values = {}
            for name in (
                "storage_backend",
                "storage_profile_id",
                "s3_endpoint_url",
                "s3_bucket",
                "s3_region",
                "s3_addressing_style",
                "s3_ca_bundle",
            ):
                value = os.environ.get(prefix + name.upper())
                if value:
                    values["answer_image_" + name] = value
            try:
                raw = os.environ.get(prefix + "STORAGE_PROFILES")
                if raw:
                    values["answer_image_storage_profiles"] = json.loads(raw)
                for name in ("access_key_id", "secret_access_key", "session_token"):
                    value = os.environ.get(prefix + "S3_" + name.upper())
                    if value:
                        values["answer_image_s3_" + name] = SecretStr(value)
                for name, convert in (
                    ("connect_timeout_seconds", float),
                    ("read_timeout_seconds", float),
                    ("total_max_attempts", int),
                ):
                    value = os.environ.get(prefix + "S3_" + name.upper())
                    if value is not None:
                        values["answer_image_s3_" + name] = convert(value)
            except (ValueError, TypeError):
                raise ValueError("Invalid answer image storage environment") from None
            return values

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
            **image_storage_env(),
            answer_image_max_image_bytes=int(
                os.environ.get(
                    "NIANLUN_API_ANSWER_IMAGE_MAX_IMAGE_BYTES",
                    str(cls.answer_image_max_image_bytes),
                )
            ),
            answer_image_max_image_pixels=int(
                os.environ.get(
                    "NIANLUN_API_ANSWER_IMAGE_MAX_IMAGE_PIXELS",
                    str(cls.answer_image_max_image_pixels),
                )
            ),
            answer_image_max_candidates=int(
                os.environ.get(
                    "NIANLUN_API_ANSWER_IMAGE_MAX_CANDIDATES",
                    str(cls.answer_image_max_candidates),
                )
            ),
            answer_image_max_answer_images=int(
                os.environ.get(
                    "NIANLUN_API_ANSWER_IMAGE_MAX_ANSWER_IMAGES",
                    str(cls.answer_image_max_answer_images),
                )
            ),
            answer_image_max_answer_bytes=int(
                os.environ.get(
                    "NIANLUN_API_ANSWER_IMAGE_MAX_ANSWER_BYTES",
                    str(cls.answer_image_max_answer_bytes),
                )
            ),
            answer_image_max_preview_images=int(
                os.environ.get(
                    "NIANLUN_API_ANSWER_IMAGE_MAX_PREVIEW_IMAGES",
                    str(cls.answer_image_max_preview_images),
                )
            ),
            answer_image_max_preview_bytes=int(
                os.environ.get(
                    "NIANLUN_API_ANSWER_IMAGE_MAX_PREVIEW_BYTES",
                    str(cls.answer_image_max_preview_bytes),
                )
            ),
            answer_image_max_occurrences=int(
                os.environ.get(
                    "NIANLUN_API_ANSWER_IMAGE_MAX_OCCURRENCES",
                    str(cls.answer_image_max_occurrences),
                )
            ),
            answer_image_max_candidate_line_bytes=int(
                os.environ.get(
                    "NIANLUN_API_ANSWER_IMAGE_MAX_CANDIDATE_LINE_BYTES",
                    str(cls.answer_image_max_candidate_line_bytes),
                )
            ),
            answer_image_max_canonical_bytes=int(
                os.environ.get(
                    "NIANLUN_API_ANSWER_IMAGE_MAX_CANONICAL_BYTES",
                    str(cls.answer_image_max_canonical_bytes),
                )
            ),
            application_api_enabled=os.environ.get(
                "NIANLUN_API_APPLICATION_ENABLED", "false"
            ).lower()
            == "true",
            application_api_max_concurrent=int(
                os.environ.get("NIANLUN_API_APPLICATION_MAX_CONCURRENT", "8")
            ),
            application_api_app_concurrent=int(
                os.environ.get("NIANLUN_API_APPLICATION_APP_CONCURRENT", "4")
            ),
            application_api_client_concurrent=int(
                os.environ.get("NIANLUN_API_APPLICATION_CLIENT_CONCURRENT", "2")
            ),
            application_api_requests_per_minute=int(
                os.environ.get("NIANLUN_API_APPLICATION_REQUESTS_PER_MINUTE", "60")
            ),
            application_api_timeout_seconds=float(
                os.environ.get("NIANLUN_API_APPLICATION_TIMEOUT_SECONDS", "300")
            ),
            application_api_retention_days=int(
                os.environ.get("NIANLUN_API_APPLICATION_RETENTION_DAYS", "30")
            ),
            application_api_result_retention_days=int(
                os.environ.get("NIANLUN_API_APPLICATION_RESULT_RETENTION_DAYS", "30")
            ),
            application_api_public_base_url=os.environ.get(
                "NIANLUN_API_APPLICATION_PUBLIC_BASE_URL"
            )
            or None,
            answer_images_enabled=parse_bool("NIANLUN_API_ANSWER_IMAGES_ENABLED", True),
            answer_image_orphan_grace_hours=int(
                os.environ.get("NIANLUN_API_ANSWER_IMAGE_ORPHAN_GRACE_HOURS", "24")
            ),
            answer_image_grant_hours=int(
                os.environ.get("NIANLUN_API_ANSWER_IMAGE_GRANT_HOURS", "24")
            ),
            answer_image_inline_max_bytes=int(
                os.environ.get(
                    "NIANLUN_API_ANSWER_IMAGE_INLINE_MAX_BYTES",
                    str(cls.answer_image_inline_max_bytes),
                )
            ),
            answer_image_inline_total_bytes=int(
                os.environ.get(
                    "NIANLUN_API_ANSWER_IMAGE_INLINE_TOTAL_BYTES",
                    str(cls.answer_image_inline_total_bytes),
                )
            ),
            answer_image_inline_response_max_bytes=int(
                os.environ.get(
                    "NIANLUN_API_ANSWER_IMAGE_INLINE_RESPONSE_MAX_BYTES",
                    str(cls.answer_image_inline_response_max_bytes),
                )
            ),
            internal_token=os.environ.get("NIANLUN_INTERNAL_TOKEN") or None,
            gateway_control_urls=_csv(
                os.environ.get("NIANLUN_GATEWAY_CONTROL_URLS", "")
            ),
            gateway_control_token=os.environ.get("NIANLUN_GATEWAY_CONTROL_TOKEN")
            or None,
            gateway_control_timeout_seconds=float(
                os.environ.get("NIANLUN_GATEWAY_CONTROL_TIMEOUT_SECONDS", "2")
            ),
            wecom_max_concurrent=int(
                os.environ.get("NIANLUN_WECOM_MAX_CONCURRENT", "32")
            ),
            wecom_run_timeout_seconds=float(
                os.environ.get("NIANLUN_WECOM_RUN_TIMEOUT_SECONDS", "120")
            ),
            wecom_conversation_retention_days=int(
                os.environ.get("NIANLUN_WECOM_CONVERSATION_RETENTION_DAYS", "30")
            ),
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
            mineru_submit_workers=int(
                os.environ.get(
                    "NIANLUN_API_MINERU_SUBMIT_WORKERS",
                    str(cls.mineru_submit_workers),
                )
            ),
            mineru_poll_workers=int(
                os.environ.get(
                    "NIANLUN_API_MINERU_POLL_WORKERS", str(cls.mineru_poll_workers)
                )
            ),
            enrich_workers=int(
                os.environ.get("NIANLUN_API_ENRICH_WORKERS", str(cls.enrich_workers))
            ),
            llm_max_concurrent=int(
                os.environ.get(
                    "NIANLUN_API_LLM_MAX_CONCURRENT", str(cls.llm_max_concurrent)
                )
            ),
            llm_per_document_concurrent=int(
                os.environ.get(
                    "NIANLUN_API_LLM_PER_DOCUMENT_CONCURRENT",
                    str(cls.llm_per_document_concurrent),
                )
            ),
            llm_request_timeout_seconds=float(
                os.environ.get(
                    "NIANLUN_API_LLM_REQUEST_TIMEOUT_SECONDS",
                    str(cls.llm_request_timeout_seconds),
                )
            ),
            pdf_chunk_max_pages=int(
                os.environ.get(
                    "NIANLUN_API_PDF_CHUNK_MAX_PAGES", str(cls.pdf_chunk_max_pages)
                )
            ),
            pdf_chunk_overlap_pages=int(
                os.environ.get(
                    "NIANLUN_API_PDF_CHUNK_OVERLAP_PAGES",
                    str(cls.pdf_chunk_overlap_pages),
                )
            ),
            pdf_max_chunks=int(
                os.environ.get("NIANLUN_API_PDF_MAX_CHUNKS", str(cls.pdf_max_chunks))
            ),
            document_staging_max_bytes=int(
                os.environ.get(
                    "NIANLUN_API_DOCUMENT_STAGING_MAX_BYTES",
                    str(cls.document_staging_max_bytes),
                )
            ),
            mineru_zip_max_entries=int(
                os.environ.get(
                    "NIANLUN_API_MINERU_ZIP_MAX_ENTRIES",
                    str(cls.mineru_zip_max_entries),
                )
            ),
            mineru_zip_max_compression_ratio=float(
                os.environ.get(
                    "NIANLUN_API_MINERU_ZIP_MAX_COMPRESSION_RATIO",
                    str(cls.mineru_zip_max_compression_ratio),
                )
            ),
            pipeline_lease_seconds=int(
                os.environ.get(
                    "NIANLUN_API_PIPELINE_LEASE_SECONDS",
                    str(cls.pipeline_lease_seconds),
                )
            ),
            snapshot_min_retention_seconds=int(
                os.environ.get(
                    "NIANLUN_API_SNAPSHOT_MIN_RETENTION_SECONDS",
                    str(cls.snapshot_min_retention_seconds),
                )
            ),
            staging_orphan_retention_seconds=int(
                os.environ.get(
                    "NIANLUN_API_STAGING_ORPHAN_RETENTION_SECONDS",
                    str(cls.staging_orphan_retention_seconds),
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
            vector_enabled=parse_bool("NIANLUN_API_VECTOR_ENABLED", cls.vector_enabled),
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
