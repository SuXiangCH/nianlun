"""Private publication contracts and immutable executable configuration."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Literal

from pydantic import ConfigDict, Field, field_validator, model_validator

from app.api_server.apis.v1.schemas import ApiSchema
from nianlun.models.reasoning import ReasoningEffort, ReasoningEffortOverride


class PublishedLoopGuard(ApiSchema):
    model_config = ConfigDict(extra="forbid", frozen=True)
    enabled: bool = True
    repeat_warn_threshold: int = 2
    repeat_hard_limit: int = 3
    no_progress_warn_rounds: int = 2
    no_progress_hard_rounds: int = 3
    model_round_warn_threshold: int = 24
    max_model_rounds: int = 32
    tool_round_warn_threshold: int = 24
    max_tool_rounds: int = 32
    total_tool_call_warn_threshold: int = 240
    max_total_tool_calls: int = 300
    per_tool_hard_limits: dict[str, int]
    recursion_limit: int = 192
    case_timeout_seconds: float = 1200.0


class PublishedConfig(ApiSchema):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[1] = 1
    application_id: str
    agent_version: Literal["v0", "v1"]
    llm_model_id: str
    model: str
    provider: str
    base_url: str
    context_window_tokens: int
    knowledge_base_id: str
    knowledge_update_policy: Literal["latest_committed"] = "latest_committed"
    clarification_enabled: bool = True
    temperature: float | None = Field(default=0.0, ge=0, le=2)
    sampling_config_version: Literal[1, 2] = 1
    thinking: bool | None = None
    reasoning_effort: ReasoningEffort | None = None
    loop_guard: PublishedLoopGuard
    prompt_version: int
    tool_schema_version: int
    checkpoint_schema_version: int = 1
    v1_prompt_hash: str


class ApplicationUpdateRequest(ApiSchema):
    expected_config_version: int = Field(ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=2000)
    knowledge_base_id: str | None = Field(default=None, min_length=1)
    llm_model_id: str | None = Field(default=None, max_length=128)
    provider: Literal["default"] | None = None
    agent_version: Literal["v0", "v1"] | None = None
    clarification_enabled: bool | None = None
    reasoning_effort: ReasoningEffortOverride = "inherit"

    @field_validator("name")
    @classmethod
    def nonblank_name(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("应用名称不能为空")
        return value.strip() if value is not None else None


class ApplicationPublishRequest(ApiSchema):
    expected_config_version: int = Field(ge=1)
    expected_deployment_version: int = Field(ge=0)


class ApplicationReleaseCreateRequest(ApiSchema):
    expected_config_version: int = Field(ge=1)
    description: str = Field(default="", max_length=500)


class ApplicationDeployRequest(ApiSchema):
    channel_type: Literal["api", "wecom"] = "api"
    release_id: str = Field(min_length=1, max_length=128)
    expected_deployment_version: int = Field(ge=0)


class ApplicationRestoreRequest(ApiSchema):
    expected_config_version: int = Field(ge=1)


class ApiChannelCreateRequest(ApiSchema):
    type: Literal["api"]


class WecomChannelCreateRequest(ApiSchema):
    type: Literal["wecom"]
    bot_id: str = Field(min_length=1, max_length=256)
    secret: str = Field(repr=False, min_length=1, max_length=512)
    default_response: str = Field(
        default="暂时无法回答，请稍后再试。", min_length=1, max_length=1000
    )
    expires_at: datetime | None = None

    @field_validator("bot_id", "default_response")
    @classmethod
    def trim_required_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("字段不能为空")
        return normalized

    @field_validator("secret")
    @classmethod
    def nonblank_secret(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("secret 不能为空")
        return value

    @field_validator("expires_at")
    @classmethod
    def timezone_required(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("到期时间必须包含时区")
        return value


class WecomChannelUpdateRequest(ApiSchema):
    expected_config_revision: int = Field(ge=1)
    bot_id: str | None = Field(default=None, min_length=1, max_length=256)
    secret: str | None = Field(default=None, repr=False, min_length=1, max_length=512)
    default_response: str | None = Field(default=None, min_length=1, max_length=1000)
    expires_at: datetime | None = None

    @field_validator("bot_id", "default_response")
    @classmethod
    def trim_required_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("字段不能为空")
        return normalized

    @field_validator("secret")
    @classmethod
    def nonblank_secret(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("secret 不能为空")
        return value

    @field_validator("expires_at")
    @classmethod
    def timezone_required(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("到期时间必须包含时区")
        return value


PublicationChannelCreateRequest = Annotated[
    ApiChannelCreateRequest | WecomChannelCreateRequest,
    Field(discriminator="type"),
]


class WecomStatusRequest(ApiSchema):
    revision: int = Field(ge=1)
    status: Literal["offline", "online", "error"]
    error_code: (
        Literal["AUTH_FAILED", "NETWORK_ERROR", "CONNECTION_CONFLICT"] | None
    ) = None

    @model_validator(mode="after")
    def validate_error_code(self) -> WecomStatusRequest:
        if self.status == "error" and self.error_code is None:
            raise ValueError("error 状态必须提供 error_code")
        if self.status != "error" and self.error_code is not None:
            raise ValueError("非 error 状态不能提供 error_code")
        return self


class PublicationUpdateRequest(ApiSchema):
    enabled: bool
    expected_deployment_version: int = Field(ge=0)


class ApplicationReleaseResponse(ApiSchema):
    id: str
    application_id: str
    sequence: int
    draft_config_version: int
    config_schema_version: int
    config_hash: str
    description: str = ""
    config: PublishedConfig
    created_at: datetime


class ApplicationReleaseListResponse(ApiSchema):
    items: list[ApplicationReleaseResponse]
    total: int


class WecomChannelState(ApiSchema):
    id: str
    bot_id: str
    has_secret: bool
    default_response: str
    enabled: bool
    active_release: ApplicationReleaseResponse | None
    deployment_version: int
    config_revision: int
    status: Literal["offline", "online", "error"]
    error_code: Literal["AUTH_FAILED", "NETWORK_ERROR", "CONNECTION_CONFLICT"] | None
    expires_at: datetime | None
    has_unpublished_changes: bool
    dependencies_ready: bool
    dependency_error: str | None
    last_deployed_at: datetime | None
    last_reported_at: datetime | None


class ApplicationDeploymentEventResponse(ApiSchema):
    id: str
    application_id: str
    channel_type: Literal["api", "wecom"]
    action: Literal["deploy", "enable", "disable"]
    previous_release_id: str | None
    release_id: str | None
    previous_sequence: int | None
    sequence: int | None
    enabled: bool
    deployment_version: int
    created_at: datetime


class ApplicationDeploymentListResponse(ApiSchema):
    items: list[ApplicationDeploymentEventResponse]
    total: int


class PublicationResponse(ApiSchema):
    application_id: str
    api_channel_created: bool
    enabled: bool
    active_release: ApplicationReleaseResponse | None
    deployment_version: int
    last_deployed_at: datetime | None = None
    config_version: int
    has_unpublished_changes: bool
    draft_ready: bool
    draft_error: str | None = None
    dependencies_ready: bool
    dependency_error: str | None = None
    wecom_channel: WecomChannelState | None = None


class ApiClientCreateRequest(ApiSchema):
    name: str = Field(min_length=1, max_length=128)

    @field_validator("name")
    @classmethod
    def nonblank_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("客户端名称不能为空")
        return value.strip()


class ApiClientUpdateRequest(ApiSchema):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    enabled: bool | None = None

    @field_validator("name")
    @classmethod
    def nonblank_name(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("客户端名称不能为空")
        return value.strip() if value is not None else None


class ApiClientResponse(ApiSchema):
    id: str
    application_id: str
    name: str
    enabled: bool
    created_at: datetime
    updated_at: datetime


class ApiKeyCreateRequest(ApiSchema):
    name: str = Field(min_length=1, max_length=128)
    api_key: str = Field(repr=False, min_length=67, max_length=201)
    expires_at: datetime | None = None

    @field_validator("api_key")
    @classmethod
    def valid_key(cls, value: str) -> str:
        if (
            re.fullmatch(r"nl_app_[A-Za-z0-9_-]{16,64}\.[A-Za-z0-9_-]{43,128}", value)
            is None
        ):
            raise ValueError("API Key 格式无效")
        return value

    @field_validator("expires_at")
    @classmethod
    def timezone_required(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("到期时间必须包含时区")
        return value


class ApiKeyResponse(ApiSchema):
    key_id: str
    client_id: str
    name: str
    expires_at: datetime | None
    revoked_at: datetime | None
    created_at: datetime
