"""Redis-backed ownership fencing shared by multiple gateway instances."""

import json
from asyncio import gather
from hashlib import sha256
from time import monotonic
from typing import Any
from uuid import uuid4

from app.channel_gateway.ownership import AcquireInput, Lease, RecoveryInput
from app.channel_gateway.protocol import (
    GatewayError,
    StatusName,
    StatusState,
)
from app.channel_gateway.redis_scripts import (
    ACQUIRE_SCRIPT,
    BLOCK_SCRIPT,
    PUBLISH_STATUS_SCRIPT,
    READ_STATUS_SCRIPT,
    RECOVER_SCRIPT,
    RELEASE_SCRIPT,
    RENEW_SCRIPT,
)


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _decode_payload(value: Any) -> str | None:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, str):
        return value
    return None


def _parse_state(payload: Any) -> StatusState | None:
    text = _decode_payload(payload)
    if text is None:
        return None
    try:
        decoded = json.loads(text)
    except json.JSONDecodeError:
        return None
    try:
        return StatusState.model_validate(decoded)
    except ValueError:
        return None


class RedisOwnershipManager:
    """Atomic ownership, conflict fences and cross-instance status in Redis."""

    def __init__(
        self,
        redis: Any,
        *,
        key_prefix: str,
        ttl_ms: int,
        safety_ms: int,
        instance_id: str,
    ) -> None:
        if safety_ms <= 0 or ttl_ms <= safety_ms:
            raise ValueError("INVALID_LEASE_CONFIGURATION")
        if not key_prefix.strip() or not instance_id.strip():
            raise ValueError("INVALID_LEASE_CONFIGURATION")
        self._redis = redis
        self._prefix = key_prefix
        self._ttl_ms = ttl_ms
        self._safety_ms = safety_ms
        self._instance_id = instance_id
        self._active: dict[str, Lease] = {}
        self._closed = False

    def _keys(self, bot_id: str) -> dict[str, str]:
        digest = sha256(bot_id.encode("utf-8")).hexdigest()
        base = f"{self._prefix}:{{{digest}}}"
        return {
            "owner": f"{base}:owner",
            "generation": f"{base}:generation",
            "conflict": f"{base}:conflict",
            "binding": f"{base}:binding",
            "recovery_version": f"{base}:recovery-version",
            "recovery_requests": f"{base}:recovery-requests",
            "status": f"{base}:status",
        }

    async def _eval(self, script: str, keys: list[str], args: list[Any]) -> Any:
        return await self._redis.eval(script, len(keys), *keys, *args)

    def _remaining_ms(self, started: float) -> float:
        return (monotonic() - started) * 1000

    async def publish_status(
        self, lease: Lease, status: StatusName, error_code: str | None
    ) -> StatusState | None:
        if not self.is_valid(lease):
            return None
        if status == "online":
            error_code = None
        elif status == "offline" and error_code not in (
            "CHANNEL_EXPIRED",
            "GATEWAY_STOPPED",
        ):
            error_code = None
        value = StatusState.model_validate(
            {
                "channel_id": lease.channel_id,
                "revision": lease.revision,
                "status": status,
                "error_code": error_code,
            }
        )
        started = monotonic()
        keys = self._keys(lease.bot_id)
        result = await self._eval(
            PUBLISH_STATUS_SCRIPT,
            [
                keys["owner"],
                keys["binding"],
                keys["generation"],
                keys["recovery_version"],
                keys["status"],
            ],
            [
                lease.token,
                lease.channel_id,
                lease.revision,
                lease.generation,
                lease.recovery_version,
                self._safety_ms,
                value.status,
                value.error_code or "",
            ],
        )
        # A late Redis response must not turn an expired safety window into a success.
        if not isinstance(result, list) or len(result) != 2:
            return None
        ttl = _as_int(result[1])
        if ttl is None:
            return None
        if not self.is_valid(lease):
            return None
        if self._remaining_ms(started) >= ttl - self._safety_ms:
            return None
        return _parse_state(result[0])

    async def read_status(self, input: AcquireInput) -> StatusState | None:
        if self._closed:
            return None
        started = monotonic()
        keys = self._keys(input.bot_id)
        result = await self._eval(
            READ_STATUS_SCRIPT,
            [
                keys["owner"],
                keys["binding"],
                keys["generation"],
                keys["recovery_version"],
                keys["status"],
            ],
            [input.channel_id, input.revision, self._safety_ms],
        )
        if self._closed or not isinstance(result, list) or len(result) != 2:
            return None
        ttl = _as_int(result[1])
        if ttl is None:
            return None
        if ttl != -1 and self._remaining_ms(started) >= ttl - self._safety_ms:
            return None
        return _parse_state(result[0])

    async def acquire(self, input: AcquireInput) -> Lease | None:
        if self._closed or input.bot_id in self._active:
            return None
        started = monotonic()
        keys = self._keys(input.bot_id)
        token = f"{self._instance_id}:{uuid4()}:{input.channel_id}"
        result = await self._eval(
            ACQUIRE_SCRIPT,
            [
                keys["owner"],
                keys["generation"],
                keys["conflict"],
                keys["binding"],
                keys["recovery_version"],
            ],
            [token, self._ttl_ms, input.channel_id, input.revision],
        )
        code = _as_int(result)
        if code == -1:
            raise GatewayError("CHANNEL_CONNECTION_CONFLICT")
        if code == -2:
            raise GatewayError("CHANNEL_BINDING_STALE")
        if code == -3:
            raise GatewayError("CHANNEL_REVISION_STALE")
        if code == -4:
            raise GatewayError("CHANNEL_EXPIRED")
        if not isinstance(result, list) or len(result) != 2:
            return None
        generation = _as_int(result[0])
        version = _as_int(result[1])
        if generation is None or generation <= 0 or version is None:
            return None
        lease = Lease(
            bot_id=input.bot_id,
            channel_id=input.channel_id,
            revision=input.revision,
            recovery_version=version,
            token=f"{token}:{generation}",
            generation=generation,
            safe_until=started + (self._ttl_ms - self._safety_ms) / 1000,
        )
        self._active[input.bot_id] = lease
        if self._closed or not self.is_valid(lease):
            await self.release(lease)
            return None
        return lease

    async def recover(self, input: RecoveryInput) -> bool:
        if self._closed:
            return False
        keys = self._keys(input.bot_id)
        result = await self._eval(
            RECOVER_SCRIPT,
            [
                keys["binding"],
                keys["conflict"],
                keys["recovery_version"],
                keys["recovery_requests"],
                keys["owner"],
            ],
            [input.channel_id, input.request_id, input.revision],
        )
        code = _as_int(result)
        if code == -1:
            raise GatewayError("CHANNEL_BINDING_STALE")
        if code == -3:
            raise GatewayError("CHANNEL_REVISION_STALE")
        if code == -4:
            raise GatewayError("CHANNEL_EXPIRED")
        if code == -2:
            raise GatewayError("CHANNEL_RECOVERY_PENDING", retryable=True)
        return code == 1

    async def has_recovered(self, lease: Lease) -> bool:
        keys = self._keys(lease.bot_id)
        version = _as_int(await self._redis.get(keys["recovery_version"]))
        return (version or 0) > lease.recovery_version

    async def block(self, lease: Lease) -> bool:
        keys = self._keys(lease.bot_id)
        result = await self._eval(
            BLOCK_SCRIPT,
            [
                keys["owner"],
                keys["conflict"],
                keys["binding"],
                keys["recovery_version"],
            ],
            [
                lease.token,
                lease.channel_id,
                lease.revision,
                lease.recovery_version,
            ],
        )
        return _as_int(result) == 1

    async def renew(self, lease: Lease) -> bool:
        if not self.is_valid(lease):
            return False
        started = monotonic()
        old_safe_until = lease.safe_until
        keys = self._keys(lease.bot_id)
        result = await self._eval(
            RENEW_SCRIPT,
            [keys["owner"], keys["binding"], keys["recovery_version"]],
            [
                lease.token,
                self._ttl_ms,
                lease.channel_id,
                lease.revision,
                lease.recovery_version,
            ],
        )
        # A late response, a release or an exhausted safety window must not revive ownership.
        if (
            _as_int(result) != 1
            or not self.is_valid(lease)
            or monotonic() >= old_safe_until
        ):
            lease.safe_until = 0.0
            return False
        lease.safe_until = started + (self._ttl_ms - self._safety_ms) / 1000
        return self.is_valid(lease)

    async def release(self, lease: Lease) -> None:
        current = self._active.get(lease.bot_id)
        if current is not None and current.token == lease.token:
            self._active.pop(lease.bot_id, None)
        lease.safe_until = 0.0
        keys = self._keys(lease.bot_id)
        await self._eval(RELEASE_SCRIPT, [keys["owner"]], [lease.token])

    def is_valid(self, lease: Lease) -> bool:
        current = self._active.get(lease.bot_id)
        return (
            not self._closed
            and current is not None
            and current.token == lease.token
            and monotonic() < lease.safe_until
        )

    async def healthy(self) -> bool:
        if self._closed:
            return False
        try:
            return bool(await self._redis.ping())
        except Exception:
            return False

    async def close(self) -> None:
        self._closed = True
        leases = list(self._active.values())
        await gather(*(self.release(lease) for lease in leases), return_exceptions=True)
