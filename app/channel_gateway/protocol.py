"""Gateway-internal contracts shared by the control server, clients and adapters."""

from datetime import datetime
from typing import Annotated, Any, Literal, Union

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    field_validator,
    model_validator,
)

DEFAULT_MAX_MESSAGE_BYTES = 64 * 1024
MAX_CONFIGURABLE_MESSAGE_BYTES = 256 * 1024
MAX_REPLY_BYTES = 20 * 1024
MAX_ANSWER_BYTES = 1024 * 1024
# 单字节控制字符最多转义成六字节，另给事件包头预留空间。
MAX_WORKFLOW_EVENT_BYTES = MAX_ANSWER_BYTES * 6 + 16384
MAX_SNAPSHOT_BYTES = 1024 * 1024
MAX_RUN_REQUEST_BYTES = 512 * 1024

StatusName = Literal["offline", "online", "error"]
PlatformErrorCode = Literal["AUTH_FAILED", "NETWORK_ERROR", "CONNECTION_CONFLICT"]
TerminalErrorCode = Literal["CHANNEL_EXPIRED", "CONNECTION_CONFLICT", "GATEWAY_STOPPED"]
ConnectionErrorCode = PlatformErrorCode | TerminalErrorCode

PLATFORM_ERROR_CODES = frozenset(
    {"AUTH_FAILED", "NETWORK_ERROR", "CONNECTION_CONFLICT"}
)
TERMINAL_ERROR_CODES = frozenset(
    {"CHANNEL_EXPIRED", "CONNECTION_CONFLICT", "GATEWAY_STOPPED"}
)

SNAPSHOT_UNAVAILABLE = "CONFIG_UNAVAILABLE"
LEASE_UNAVAILABLE = "LEASE_UNAVAILABLE"
CONNECT_TIMEOUT = "CONNECT_TIMEOUT"
WORKFLOW_TIMEOUT = "WORKFLOW_TIMEOUT"
STREAM_INTERRUPTED = "STREAM_INTERRUPTED"


class GatewayError(Exception):
    """Carries protocol-safe codes across the gateway; messages never include payloads."""

    def __init__(
        self,
        code: str,
        message: str | None = None,
        status_code: int = 502,
        retryable: bool = False,
        reply_rejected: bool = False,
        provider_errcode: int | None = None,
    ) -> None:
        self.code = code
        self.message = message or code
        self.status_code = status_code
        self.retryable = retryable
        self.reply_rejected = reply_rejected
        self.provider_errcode = provider_errcode
        super().__init__(self.message)


class ConnectionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    bot_id: str = Field(min_length=1, max_length=256)
    secret: str = Field(min_length=1, max_length=1024)


class RuntimeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    default_response: str = Field(max_length=1000)
    expires_at: datetime | None = None

    @field_validator("expires_at")
    @classmethod
    def _timezone_required(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("expires_at must include a timezone")
        return value


class ChannelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    channel_id: str = Field(min_length=1, max_length=256)
    application_id: str = Field(min_length=1, max_length=256)
    revision: int = Field(ge=1)
    enabled: bool
    connection: ConnectionConfig
    runtime: RuntimeConfig


class Snapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    channels: list[ChannelConfig]

    @model_validator(mode="after")
    def _unique_identifiers(self) -> "Snapshot":
        channel_ids: set[str] = set()
        bot_ids: set[str] = set()
        for channel in self.channels:
            if channel.channel_id in channel_ids:
                raise ValueError("duplicate channel_id")
            if channel.connection.bot_id in bot_ids:
                raise ValueError("duplicate bot_id")
            channel_ids.add(channel.channel_id)
            bot_ids.add(channel.connection.bot_id)
        return self


class StatusState(BaseModel):
    """Ownership-scoped state; camelCase aliases match the Redis wire format."""

    model_config = ConfigDict(
        extra="forbid", frozen=True, populate_by_name=True, serialize_by_alias=True
    )

    channel_id: str = Field(alias="channelId", min_length=1)
    revision: int = Field(ge=1)
    status: StatusName
    error_code: ConnectionErrorCode | None = Field(default=None, alias="errorCode")

    @model_validator(mode="after")
    def _check_error_code(self) -> "StatusState":
        if self.status == "online" and self.error_code is not None:
            raise ValueError("online status cannot carry an error code")
        if self.status == "error" and self.error_code not in PLATFORM_ERROR_CODES:
            raise ValueError("error status requires a platform error code")
        if self.status == "offline" and self.error_code not in (
            None,
            "CHANNEL_EXPIRED",
            "GATEWAY_STOPPED",
        ):
            raise ValueError("offline status carries an invalid error code")
        return self


class RunStartEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["start"]
    run_id: str = Field(min_length=1)
    conversation_id: str = Field(min_length=1)
    release_id: str = Field(min_length=1)


class RunChunkEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["chunk"]
    content: str


class RunDoneEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["done"]
    content: str
    run_id: str = Field(min_length=1)


class RunErrorEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["error"]
    code: int
    message: str


class RunDuplicateEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["duplicate"]


RunEvent = Annotated[
    Union[
        RunStartEvent,
        RunChunkEvent,
        RunDoneEvent,
        RunErrorEvent,
        RunDuplicateEvent,
    ],
    Field(discriminator="type"),
]
RUN_EVENT_ADAPTER: TypeAdapter[RunEvent] = TypeAdapter(RunEvent)


def parse_run_event(value: Any) -> RunEvent:
    return RUN_EVENT_ADAPTER.validate_python(value)
