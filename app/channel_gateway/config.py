"""Environment-driven settings for the standalone gateway process."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping
from urllib.parse import urlsplit

from app.channel_gateway.protocol import MAX_CONFIGURABLE_MESSAGE_BYTES

_LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})


class GatewayConfigurationError(Exception):
    """Reports only offending field names; values may contain credentials."""

    def __init__(self, fields: list[str]) -> None:
        self.fields = fields
        super().__init__("INVALID_GATEWAY_CONFIGURATION")


class _Invalid(Exception):
    def __init__(self, field: str) -> None:
        self.field = field
        super().__init__(field)


def _string(
    env: Mapping[str, str], name: str, default: str | None = None
) -> str | None:
    value = env.get(name)
    if value is None or value == "":
        return default
    return value


def _token(env: Mapping[str, str], name: str) -> str:
    value = _string(env, name)
    # The platform rejects tokens with surrounding whitespace, so accept only the
    # exact form that will actually authenticate.
    if value is None or len(value) < 32 or value != value.strip():
        raise _Invalid(name)
    return value


def _integer(
    env: Mapping[str, str], name: str, default: int, maximum: int | None = None
) -> int:
    raw = _string(env, name)
    if raw is None:
        value = default
    else:
        try:
            value = int(raw)
        except ValueError:
            raise _Invalid(name) from None
    if value < 1 or (maximum is not None and value > maximum):
        raise _Invalid(name)
    return value


def _float(env: Mapping[str, str], name: str, default: float) -> float:
    raw = _string(env, name)
    if raw is None:
        value = default
    else:
        try:
            value = float(raw)
        except ValueError:
            raise _Invalid(name) from None
    if not math.isfinite(value) or value <= 0:
        raise _Invalid(name)
    return value


def _http_url(env: Mapping[str, str], name: str, default: str) -> str:
    value = _string(env, name, default)
    assert value is not None
    parsed = urlsplit(value)
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise _Invalid(name)
    return value


def _redis_url(env: Mapping[str, str]) -> str | None:
    value = _string(env, "REDIS_URL")
    if value is None:
        return None
    parsed = urlsplit(value)
    if parsed.scheme not in ("redis", "rediss") or not parsed.hostname:
        raise _Invalid("REDIS_URL")
    return value


@dataclass(frozen=True)
class GatewaySettings:
    host: str
    port: int
    log_level: str
    api_url: str
    internal_token: str
    control_token: str
    ownership_backend: str
    redis_url: str | None
    redis_key_prefix: str
    lease_ttl_ms: int
    lease_safety_ms: int
    snapshot_interval_seconds: float
    status_heartbeat_seconds: float
    connect_timeout_seconds: float
    first_event_timeout_seconds: float
    workflow_timeout_seconds: float
    max_concurrent: int
    max_message_bytes: int
    max_answer_bytes: int
    reply_max_bytes: int
    stream_interval_ms: int
    stream_keepalive_seconds: float
    dedup_max_entries: int
    dedup_ttl_seconds: float

    @property
    def lease_renew_seconds(self) -> float:
        safe_window_ms = self.lease_ttl_ms - self.lease_safety_ms
        return min(10.0, self.lease_ttl_ms / 3000, safe_window_ms / 2000)

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "GatewaySettings":
        try:
            internal_token = _token(env, "NIANLUN_INTERNAL_TOKEN")
            control_token = _token(env, "NIANLUN_GATEWAY_CONTROL_TOKEN")
            host = _string(env, "GATEWAY_HOST", "127.0.0.1")
            assert host is not None
            if not host.strip():
                raise _Invalid("GATEWAY_HOST")
            port = _integer(env, "GATEWAY_PORT", 3100, 65535)
            log_level = (_string(env, "GATEWAY_LOG_LEVEL", "INFO") or "").upper()
            if log_level not in _LOG_LEVELS:
                raise _Invalid("GATEWAY_LOG_LEVEL")
            api_url = _http_url(env, "NIANLUN_API_URL", "http://127.0.0.1:8000")
            backend = _string(env, "GATEWAY_OWNERSHIP_BACKEND", "local")
            if backend not in ("local", "redis"):
                raise _Invalid("GATEWAY_OWNERSHIP_BACKEND")
            redis_url = _redis_url(env)
            if backend == "redis" and redis_url is None:
                raise _Invalid("REDIS_URL")
            prefix = _string(env, "GATEWAY_REDIS_KEY_PREFIX", "nianlun:channel-gateway")
            assert prefix is not None
            if (
                (
                    "GATEWAY_REDIS_KEY_PREFIX" in env
                    and not env["GATEWAY_REDIS_KEY_PREFIX"].strip()
                )
                or "{" in prefix
                or "}" in prefix
            ):
                raise _Invalid("GATEWAY_REDIS_KEY_PREFIX")
            lease_ttl_ms = _integer(env, "GATEWAY_LEASE_TTL_MS", 30000)
            lease_safety_ms = _integer(env, "GATEWAY_LEASE_SAFETY_MS", 5000)
            if lease_ttl_ms <= lease_safety_ms:
                raise _Invalid("GATEWAY_LEASE_TTL_MS")
            snapshot_interval = _float(env, "GATEWAY_SNAPSHOT_INTERVAL_SECONDS", 30.0)
            heartbeat = _float(env, "GATEWAY_STATUS_HEARTBEAT_SECONDS", 30.0)
            connect_timeout = _float(env, "GATEWAY_CONNECT_TIMEOUT_SECONDS", 10.0)
            first_event_timeout = _float(
                env, "GATEWAY_FIRST_EVENT_TIMEOUT_SECONDS", 30.0
            )
            workflow_timeout = _float(env, "GATEWAY_WORKFLOW_TIMEOUT_SECONDS", 150.0)
            if first_event_timeout >= workflow_timeout:
                raise _Invalid("GATEWAY_FIRST_EVENT_TIMEOUT_SECONDS")
            max_concurrent = _integer(env, "GATEWAY_MAX_CONCURRENT", 32)
            max_message_bytes = _integer(
                env,
                "GATEWAY_MAX_MESSAGE_BYTES",
                64 * 1024,
                MAX_CONFIGURABLE_MESSAGE_BYTES,
            )
            max_answer_bytes = _integer(env, "GATEWAY_MAX_ANSWER_BYTES", 1024 * 1024)
            reply_max_bytes = _integer(env, "GATEWAY_REPLY_MAX_BYTES", 20 * 1024)
            stream_interval_ms = _integer(env, "GATEWAY_STREAM_INTERVAL_MS", 300)
            keepalive = _float(env, "GATEWAY_STREAM_KEEPALIVE_SECONDS", 15.0)
            dedup_max_entries = _integer(env, "GATEWAY_DEDUP_MAX_ENTRIES", 10000)
            dedup_ttl = _float(env, "GATEWAY_DEDUP_TTL_SECONDS", 300.0)
        except _Invalid as error:
            raise GatewayConfigurationError([error.field]) from None
        return cls(
            host=host,
            port=port,
            log_level=log_level,
            api_url=api_url,
            internal_token=internal_token,
            control_token=control_token,
            ownership_backend=backend,
            redis_url=redis_url,
            redis_key_prefix=prefix,
            lease_ttl_ms=lease_ttl_ms,
            lease_safety_ms=lease_safety_ms,
            snapshot_interval_seconds=snapshot_interval,
            status_heartbeat_seconds=heartbeat,
            connect_timeout_seconds=connect_timeout,
            first_event_timeout_seconds=first_event_timeout,
            workflow_timeout_seconds=workflow_timeout,
            max_concurrent=max_concurrent,
            max_message_bytes=max_message_bytes,
            max_answer_bytes=max_answer_bytes,
            reply_max_bytes=reply_max_bytes,
            stream_interval_ms=stream_interval_ms,
            stream_keepalive_seconds=keepalive,
            dedup_max_entries=dedup_max_entries,
            dedup_ttl_seconds=dedup_ttl,
        )
