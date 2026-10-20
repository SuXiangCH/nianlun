"""Ownership protocol and the single-process implementation.

The local manager mirrors the Redis scripts' fencing semantics so the gateway
can run without Redis as long as only one process owns a bot.
"""

from dataclasses import dataclass
from threading import Lock
from time import monotonic
from typing import Protocol
from uuid import uuid4

from app.channel_gateway.protocol import (
    ConnectionErrorCode,
    GatewayError,
    StatusName,
    StatusState,
)

TERMINAL_STATUSES = frozenset(
    {"CHANNEL_EXPIRED", "CONNECTION_CONFLICT", "GATEWAY_STOPPED"}
)


@dataclass(frozen=True)
class AcquireInput:
    bot_id: str
    channel_id: str
    revision: int


@dataclass(frozen=True)
class RecoveryInput(AcquireInput):
    request_id: str


@dataclass
class Lease:
    bot_id: str
    channel_id: str
    revision: int
    recovery_version: int
    token: str
    generation: int
    safe_until: float


@dataclass(frozen=True)
class _Binding:
    channel_id: str
    revision: int


@dataclass(frozen=True)
class _StatusRecord:
    value: StatusState
    token: str
    generation: int
    recovery_version: int


class OwnershipManager(Protocol):
    def acquire(self, input: AcquireInput) -> Lease | None: ...

    def renew(self, lease: Lease) -> bool: ...

    def release(self, lease: Lease) -> None: ...

    def block(self, lease: Lease) -> bool: ...

    def recover(self, input: RecoveryInput) -> bool: ...

    def has_recovered(self, lease: Lease) -> bool: ...

    def publish_status(
        self, lease: Lease, status: StatusName, error_code: ConnectionErrorCode | None
    ) -> StatusState | None: ...

    def read_status(self, input: AcquireInput) -> StatusState | None: ...

    def is_valid(self, lease: Lease) -> bool: ...

    def close(self) -> None: ...


class LocalOwnershipManager:
    """In-memory fencing for a single gateway process; tests use the same boundary."""

    def __init__(self, instance_id: str) -> None:
        if not instance_id.strip():
            raise ValueError("instance_id must not be empty")
        self._instance_id = instance_id
        self._lock = Lock()
        self._closed = False
        self._generation = 0
        self._owners: dict[str, Lease] = {}
        self._generations: dict[str, int] = {}
        self._bindings: dict[str, _Binding] = {}
        self._conflicts: dict[str, _Binding] = {}
        self._expired_revisions: dict[str, int] = {}
        self._recovery_versions: dict[str, int] = {}
        self._recovery_requests: dict[str, set[str]] = {}
        self._statuses: dict[str, _StatusRecord] = {}

    def _valid_unlocked(self, lease: Lease) -> bool:
        owner = self._owners.get(lease.bot_id)
        binding = self._bindings.get(lease.bot_id)
        return (
            not self._closed
            and owner is not None
            and owner.token == lease.token
            and self._generations.get(lease.bot_id) == lease.generation
            and lease.safe_until > monotonic()
            and lease.revision > self._expired_revisions.get(lease.bot_id, 0)
            and binding is not None
            and binding.channel_id == lease.channel_id
            and binding.revision == lease.revision
        )

    def acquire(self, input: AcquireInput) -> Lease | None:
        with self._lock:
            if self._closed:
                return None
            binding = self._bindings.get(input.bot_id)
            if binding is not None:
                if (
                    input.revision <= binding.revision
                    and binding.channel_id != input.channel_id
                ):
                    raise GatewayError("CHANNEL_BINDING_STALE")
                if input.revision < binding.revision:
                    raise GatewayError("CHANNEL_REVISION_STALE")
            if input.revision <= self._expired_revisions.get(input.bot_id, 0):
                raise GatewayError("CHANNEL_EXPIRED")
            if binding is None or input.revision > binding.revision:
                self._bindings[input.bot_id] = _Binding(
                    input.channel_id, input.revision
                )
                self._conflicts.pop(input.bot_id, None)
            if input.bot_id in self._owners:
                return None
            conflict = self._conflicts.get(input.bot_id)
            if conflict is not None and input.revision <= conflict.revision:
                raise GatewayError("CHANNEL_CONNECTION_CONFLICT")
            self._generation += 1
            lease = Lease(
                bot_id=input.bot_id,
                channel_id=input.channel_id,
                revision=input.revision,
                recovery_version=self._recovery_versions.get(input.bot_id, 0),
                token=f"{self._instance_id}:{uuid4()}",
                generation=self._generation,
                safe_until=float("inf"),
            )
            self._owners[input.bot_id] = lease
            self._generations[input.bot_id] = lease.generation
            self._conflicts.pop(input.bot_id, None)
            return lease

    def block(self, lease: Lease) -> bool:
        with self._lock:
            if (
                not self._valid_unlocked(lease)
                or self._recovery_versions.get(lease.bot_id, 0)
                != lease.recovery_version
            ):
                return False
            self._conflicts[lease.bot_id] = _Binding(lease.channel_id, lease.revision)
            return True

    def recover(self, input: RecoveryInput) -> bool:
        with self._lock:
            if self._closed:
                return False
            binding = self._bindings.get(input.bot_id)
            if binding is None or binding.channel_id != input.channel_id:
                raise GatewayError("CHANNEL_BINDING_STALE")
            if binding.revision != input.revision:
                raise GatewayError("CHANNEL_REVISION_STALE")
            if input.revision <= self._expired_revisions.get(input.bot_id, 0):
                raise GatewayError("CHANNEL_EXPIRED")
            requests = self._recovery_requests.setdefault(input.bot_id, set())
            if input.request_id in requests:
                return False
            owner = self._owners.get(input.bot_id)
            if (
                input.bot_id not in self._conflicts
                and owner is not None
                and self._valid_unlocked(owner)
            ):
                raise GatewayError("CHANNEL_RECOVERY_PENDING", retryable=True)
            requests.add(input.request_id)
            self._conflicts.pop(input.bot_id, None)
            self._recovery_versions[input.bot_id] = (
                self._recovery_versions.get(input.bot_id, 0) + 1
            )
            return True

    def has_recovered(self, lease: Lease) -> bool:
        with self._lock:
            return self._recovery_versions.get(lease.bot_id, 0) > lease.recovery_version

    def renew(self, lease: Lease) -> bool:
        with self._lock:
            if self._recovery_versions.get(lease.bot_id, 0) != lease.recovery_version:
                lease.safe_until = 0.0
                return False
            return self._valid_unlocked(lease)

    def release(self, lease: Lease) -> None:
        with self._lock:
            owner = self._owners.get(lease.bot_id)
            if owner is not None and owner.token == lease.token:
                self._owners.pop(lease.bot_id, None)
            lease.safe_until = 0.0

    def publish_status(
        self, lease: Lease, status: StatusName, error_code: ConnectionErrorCode | None
    ) -> StatusState | None:
        with self._lock:
            if (
                not self._valid_unlocked(lease)
                or self._recovery_versions.get(lease.bot_id, 0)
                != lease.recovery_version
            ):
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
            if status != "online" and error_code == "CHANNEL_EXPIRED":
                self._expired_revisions[lease.bot_id] = lease.revision
            self._statuses[lease.bot_id] = _StatusRecord(
                value=value,
                token=lease.token,
                generation=lease.generation,
                recovery_version=lease.recovery_version,
            )
            return value

    def read_status(self, input: AcquireInput) -> StatusState | None:
        with self._lock:
            if self._closed:
                return None
            record = self._statuses.get(input.bot_id)
            binding = self._bindings.get(input.bot_id)
            if (
                record is None
                or record.value.channel_id != input.channel_id
                or record.value.revision != input.revision
                or binding is None
                or binding.channel_id != input.channel_id
                or binding.revision != input.revision
            ):
                return None
            if record.generation != self._generations.get(
                input.bot_id
            ) or record.recovery_version != self._recovery_versions.get(
                input.bot_id, 0
            ):
                return None
            owner = self._owners.get(input.bot_id)
            if (
                owner is not None
                and owner.token == record.token
                and self._valid_unlocked(owner)
            ):
                return record.value
            if (
                record.value.status != "online"
                and record.value.error_code in TERMINAL_STATUSES
            ):
                return record.value
            return None

    def is_valid(self, lease: Lease) -> bool:
        with self._lock:
            return self._valid_unlocked(lease)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            for lease in self._owners.values():
                lease.safe_until = 0.0
            self._owners.clear()
