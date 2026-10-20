"""Channel reconciliation, connection lifecycle and message scheduling."""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from time import monotonic
from typing import Any, Awaitable, Callable, TypeVar
from uuid import uuid4

from app.channel_gateway.config import GatewaySettings
from app.channel_gateway.message import (
    NOTICE_INVALID_MESSAGE,
    NOTICE_REPLIES,
    NOTICE_UNSUPPORTED_MESSAGE,
    InvalidFrame,
    MessageNotice,
    NormalizedMessage,
    normalize_message,
    truncate_reply,
)
from app.channel_gateway.metrics import Metrics
from app.channel_gateway.ownership import (
    AcquireInput,
    Lease,
    OwnershipManager,
    RecoveryInput,
)
from app.channel_gateway.protocol import (
    ChannelConfig,
    ConnectionErrorCode,
    GatewayError,
    Snapshot,
    StatusName,
    StatusState,
)

logger = logging.getLogger(__name__)

AUTH_RETRY_SECONDS = 60.0
RECONNECT_BASE_SECONDS = 1.0
RECONNECT_MAX_SECONDS = 30.0
MESSAGE_RETRY_DELAY_SECONDS = 0.5
OWNERSHIP_CONCURRENCY = 16
SHUTDOWN_TASK_TIMEOUT_SECONDS = 5.0

THINKING_TEXT = "正在思考中..."
BUSY_TEXT = "上一条消息还在处理中，请稍后再试"
INCOMPLETE_HINT = "（回答未完成，请稍后重试）"
DEFAULT_RESPONSE = "服务暂时不可用，请稍后再试"
AUTH_FAILURE = "AUTH_FAILED"
NETWORK_ERROR = "NETWORK_ERROR"
CONNECTION_CONFLICT = "CONNECTION_CONFLICT"
CHANNEL_EXPIRED = "CHANNEL_EXPIRED"
GATEWAY_STOPPED = "GATEWAY_STOPPED"
CONNECTING = "CONNECTING"
LEASE_UNAVAILABLE = "LEASE_UNAVAILABLE"
CONFIG_UNAVAILABLE = "CONFIG_UNAVAILABLE"
CONNECT_TIMEOUT = "CONNECT_TIMEOUT"
WORKFLOW_CANCELLED = "WORKFLOW_CANCELLED"
STREAM_INTERRUPTED = "STREAM_INTERRUPTED"
WORKFLOW_TIMEOUT = "WORKFLOW_TIMEOUT"
ANSWER_TOO_LARGE = "CHANNEL_ANSWER_TOO_LARGE"
WORKFLOW_REQUEST_TOO_LARGE = "WORKFLOW_REQUEST_TOO_LARGE"

# Status writes for these internal codes describe an operation failure rather
# than the channel's runtime state, so they must not overwrite platform status.
_UNREPORTED_CODES = frozenset({CONFIG_UNAVAILABLE, LEASE_UNAVAILABLE, CONNECT_TIMEOUT})
_PLATFORM_ERROR_CODES = frozenset({AUTH_FAILURE, NETWORK_ERROR, CONNECTION_CONFLICT})

T = TypeVar("T")


async def _call(value: T | Awaitable[T]) -> T:
    if inspect.isawaitable(value):
        return await value
    return value


def _platform_write(
    status: str, error_code: str | None
) -> tuple[StatusName, ConnectionErrorCode | None] | None:
    """Map internal state to the ownership protocol's narrower status types."""
    if status == "online":
        return "online", None
    if status == "offline":
        if error_code in ("CHANNEL_EXPIRED", "GATEWAY_STOPPED"):
            return "offline", error_code
        return "offline", None
    if status == "error" and error_code in (
        "AUTH_FAILED",
        "NETWORK_ERROR",
        "CONNECTION_CONFLICT",
    ):
        return "error", error_code
    return None


def _fence_blocks(fence: tuple[str, int] | None, channel: ChannelConfig) -> bool:
    """Revision fences only apply while the channel keeps the same bot identity."""
    return (
        fence is not None
        and fence[0] == channel.connection.bot_id
        and channel.revision <= fence[1]
    )


@dataclass
class _Entry:
    channel: ChannelConfig
    lease: Lease
    adapter: Any | None = None
    epoch: int = 0
    online: bool = False
    attempts: int = 0
    auth_failure: bool = False
    conflicted: bool = False
    error_code: str | None = None
    retry_task: asyncio.Task | None = None
    renew_task: asyncio.Task | None = None
    connect_task: asyncio.Task | None = None
    connect_timer: asyncio.Task | None = None
    safety_task: asyncio.Task | None = None
    expiry_task: asyncio.Task | None = None
    status_task: asyncio.Task | None = None
    conflict_task: asyncio.Task | None = None
    remove_task: asyncio.Task | None = None
    controllers: set[asyncio.Event] = field(default_factory=set)

    @property
    def channel_id(self) -> str:
        return self.channel.channel_id


@dataclass
class _InFlight:
    context: str
    pending: tuple["_Entry", int, dict[str, Any]] | None = None


@dataclass
class _Completed:
    revision: int
    expires_at: float
    kind: str
    notice_code: str | None = None
    settled_context: str | None = None


@dataclass(eq=False)
class _SnapshotWaiter:
    after_cycle: int
    future: asyncio.Future[bool]


class _Reply:
    """Per-message reply state; owns the single stream id used for retries."""

    def __init__(
        self,
        entry: _Entry,
        epoch: int,
        req_id: str,
        started_at: float,
        *,
        settings: GatewaySettings,
        metrics: Metrics,
        is_current: Callable[[_Entry], bool],
    ) -> None:
        self._settings = settings
        self._metrics = metrics
        self._is_current = is_current
        self.entry = entry
        self.epoch = epoch
        self.req_id = req_id
        self.stream_id = str(uuid4())
        self.started_at = started_at
        self.deadline = started_at + settings.workflow_timeout_seconds
        self.first_deadline = started_at + settings.first_event_timeout_seconds
        self.cancel = asyncio.Event()
        self.content = ""
        self.sent_any_content = False
        self.terminal_attempted = False
        self.reply_rejected = False
        self.reply_failed = False
        self.sent_final = False
        self.last_sent_at = 0.0

    def valid(self, terminal: bool = False) -> bool:
        return (
            not self.cancel.is_set()
            and (terminal or monotonic() < self.deadline)
            and self.entry.online
            and self.entry.epoch == self.epoch
            and self._is_current(self.entry)
        )

    async def send(self, content: str, finish: bool) -> None:
        if not self.valid(terminal=finish):
            raise GatewayError(WORKFLOW_CANCELLED)
        adapter = self.entry.adapter
        if adapter is None:
            raise GatewayError(WORKFLOW_CANCELLED)
        if finish:
            self.terminal_attempted = True
        self._metrics.inc("replies_active")
        try:
            await adapter.reply(
                req_id=self.req_id,
                stream_id=self.stream_id,
                content=truncate_reply(content, self._settings.reply_max_bytes),
                finish=finish,
            )
        except GatewayError as error:
            self.reply_failed = True
            if error.reply_rejected:
                self.reply_rejected = True
            self._metrics.inc("replies_failed")
            raise
        except Exception:
            self.reply_failed = True
            self._metrics.inc("replies_failed")
            raise GatewayError("WECOM_ACK_UNKNOWN") from None
        else:
            self._metrics.inc("replies_ok")
            self.last_sent_at = monotonic()
        finally:
            self._metrics.dec("replies_active")

    async def send_terminal(self, content: str) -> None:
        await self.send(content, True)
        self.sent_final = True


class ChannelManager:
    def __init__(
        self,
        settings: GatewaySettings,
        config: Any,
        workflow: Any,
        status: Any,
        ownership: OwnershipManager,
        adapter_factory: Callable[..., Any],
        *,
        metrics: Metrics | None = None,
    ) -> None:
        self._settings = settings
        self._config = config
        self._workflow = workflow
        self._status = status
        self._ownership = ownership
        self._adapter_factory = adapter_factory
        self._metrics = metrics or Metrics()

        self._entries: dict[str, _Entry] = {}
        self._desired: dict[str, ChannelConfig] = {}
        self._completed: OrderedDict[str, _Completed] = OrderedDict()
        self._in_flight: dict[str, _InFlight] = {}
        self._conversations: set[str] = set()
        self._expired_revisions: dict[str, tuple[str, int]] = {}
        self._blocked: dict[str, int] = {}
        self._snapshot_waiters: set[_SnapshotWaiter] = set()
        self._message_tasks: set[asyncio.Task] = set()
        self._loops: list[asyncio.Task] = []
        self._refresh_task: asyncio.Task | None = None

        self._started = False
        self._stopped = False
        self._ready = False
        self._dirty = False
        self._snapshot_cycle = 0
        self._active_tasks = 0

    # -- lifecycle -----------------------------------------------------

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._loops = [
            asyncio.create_task(self._snapshot_loop()),
            asyncio.create_task(self._heartbeat_loop()),
        ]
        await self.refresh()

    async def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        self._ready = False
        for task in self._loops:
            task.cancel()
        self._loops.clear()
        for entry in self._entries.values():
            for controller in list(entry.controllers):
                controller.set()
        conflict_tasks = [
            entry.conflict_task
            for entry in self._entries.values()
            if entry.conflict_task is not None
        ]
        if conflict_tasks:
            await asyncio.gather(*conflict_tasks, return_exceptions=True)
        for entry in list(self._entries.values()):
            await self._remove_entry(entry, GATEWAY_STOPPED, GATEWAY_STOPPED)
        if self._refresh_task is not None and not self._refresh_task.done():
            self._refresh_task.cancel()
            await asyncio.gather(self._refresh_task, return_exceptions=True)
        self._refresh_task = None
        if self._message_tasks:
            _, pending = await asyncio.wait(
                list(self._message_tasks), timeout=SHUTDOWN_TASK_TIMEOUT_SECONDS
            )
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        try:
            await _call(self._ownership.close())
        except Exception:
            logger.warning("gateway_ownership_close_failed")

    def status(self) -> dict[str, Any]:
        online = sum(
            1
            for entry in self._entries.values()
            if entry.online and self._is_current(entry)
        )
        return {
            "ready": self._ready and not self._stopped,
            "channels": len(self._desired),
            "online": online,
            "active_tasks": self._active_tasks,
        }

    async def readiness(self) -> bool:
        """Ready means a snapshot loaded and the ownership backend is reachable."""
        if not self._ready or self._stopped:
            return False
        health = getattr(self._ownership, "healthy", None)
        if health is None:
            return True
        try:
            return bool(await _call(health()))
        except Exception:
            return False

    def metrics_snapshot(self) -> dict[str, int]:
        return self._metrics.snapshot()

    def request_refresh(self) -> bool:
        if self._stopped:
            return False
        self._dirty = True
        if self._refresh_task is None:
            self._refresh_task = asyncio.create_task(self._refresh_loop())
        return True

    async def refresh(self) -> None:
        if not self.request_refresh():
            return
        task = self._refresh_task
        if task is not None:
            await asyncio.shield(task)

    async def ensure_connected(
        self, channel_id: str, revision: int, request_id: str
    ) -> dict[str, Any]:
        if self._stopped:
            raise GatewayError(GATEWAY_STOPPED, status_code=503)
        budget = self._settings.connect_timeout_seconds
        deadline = monotonic() + budget
        if not self._ready:
            if not await self._wait_for_next_snapshot(budget):
                raise GatewayError(CONFIG_UNAVAILABLE, status_code=503)
            deadline = monotonic() + budget
        channel = self._desired.get(channel_id)
        if channel is None:
            raise GatewayError("CHANNEL_NOT_FOUND", status_code=404)
        if channel.revision != revision:
            raise GatewayError("CHANNEL_REVISION_MISMATCH", status_code=409)
        if self._is_expired(channel):
            raise GatewayError(CHANNEL_EXPIRED, status_code=409)

        entry = self._entries.get(channel_id)
        if entry is not None and self._is_current(entry) and entry.online:
            return self._connect_result(channel_id, revision, "online", None)

        await self._recover_ownership(channel, request_id)
        target = AcquireInput(
            bot_id=channel.connection.bot_id,
            channel_id=channel.channel_id,
            revision=channel.revision,
        )

        while monotonic() < deadline:
            entry = self._entries.get(channel_id)
            result = self._local_result(channel_id, revision, entry)
            if result is not None:
                return result
            try:
                state = await _call(self._ownership.read_status(target))
            except Exception:
                state = None
            entry = self._entries.get(channel_id)
            result = self._local_result(channel_id, revision, entry)
            if result is not None:
                return result
            if state is not None:
                if state.status == "error":
                    return self._connect_result(
                        channel_id, revision, "error", state.error_code
                    )
                if state.status == "online" and entry is None:
                    return self._connect_result(channel_id, revision, "online", None)
            await asyncio.sleep(0.025)
        entry = self._entries.get(channel_id)
        if entry is not None and entry.error_code in _PLATFORM_ERROR_CODES:
            return self._connect_result(channel_id, revision, "error", entry.error_code)
        raise GatewayError(CONNECT_TIMEOUT, status_code=503)

    def _local_result(
        self, channel_id: str, revision: int, entry: _Entry | None
    ) -> dict[str, Any] | None:
        if entry is not None and self._is_current(entry) and entry.online:
            return self._connect_result(channel_id, revision, "online", None)
        if entry is not None and entry.conflicted:
            raise GatewayError(CONNECTION_CONFLICT, status_code=409)
        if (
            entry is not None
            and entry.remove_task is None
            and entry.error_code == AUTH_FAILURE
        ):
            return self._connect_result(channel_id, revision, "error", AUTH_FAILURE)
        return None

    @staticmethod
    def _connect_result(
        channel_id: str, revision: int, status: str, error_code: str | None
    ) -> dict[str, Any]:
        return {
            "channel_id": channel_id,
            "revision": revision,
            "status": status,
            "error_code": error_code,
        }

    async def _recover_ownership(self, channel: ChannelConfig, request_id: str) -> None:
        input = AcquireInput(
            bot_id=channel.connection.bot_id,
            channel_id=channel.channel_id,
            revision=channel.revision,
        )
        try:
            state = await _call(self._ownership.read_status(input))
        except Exception:
            state = None
        if state is not None and (
            state.status == "online"
            or (state.status == "error" and state.error_code != CONNECTION_CONFLICT)
        ):
            return
        try:
            await _call(
                self._ownership.recover(
                    RecoveryInput(
                        bot_id=input.bot_id,
                        channel_id=input.channel_id,
                        revision=input.revision,
                        request_id=request_id,
                    )
                )
            )
        except GatewayError as error:
            if error.code == "CHANNEL_RECOVERY_PENDING":
                # 已有健康 owner；由外层轮询共享状态等待它上线。
                return
            if error.code in (
                CHANNEL_EXPIRED,
                "CHANNEL_BINDING_STALE",
                "CHANNEL_REVISION_STALE",
            ):
                raise GatewayError(error.code, status_code=409) from None
            raise GatewayError(LEASE_UNAVAILABLE, status_code=503) from None
        except Exception:
            raise GatewayError(LEASE_UNAVAILABLE, status_code=503) from None
        self.request_refresh()

    async def _wait_for_next_snapshot(self, timeout: float) -> bool:
        loop = asyncio.get_running_loop()
        waiter = _SnapshotWaiter(self._snapshot_cycle, loop.create_future())
        self._snapshot_waiters.add(waiter)
        self.request_refresh()
        try:
            return await asyncio.wait_for(waiter.future, timeout)
        except asyncio.TimeoutError:
            return False
        finally:
            self._snapshot_waiters.discard(waiter)

    # -- background loops ---------------------------------------------

    async def _snapshot_loop(self) -> None:
        try:
            while not self._stopped:
                await asyncio.sleep(self._settings.snapshot_interval_seconds)
                self.request_refresh()
        except asyncio.CancelledError:
            pass

    async def _renew_loop(self, entry: _Entry) -> None:
        next_renewal = monotonic() + self._settings.lease_renew_seconds
        try:
            while (
                not self._stopped
                and entry.remove_task is None
                and self._entries.get(entry.channel_id) is entry
            ):
                await asyncio.sleep(max(0.0, next_renewal - monotonic()))
                # Each lease has its own cadence; slow peers cannot delay renewal.
                next_renewal = monotonic() + self._settings.lease_renew_seconds
                await self._renew_entry(entry)
        except asyncio.CancelledError:
            pass

    async def _heartbeat_loop(self) -> None:
        try:
            while not self._stopped:
                await asyncio.sleep(self._settings.status_heartbeat_seconds)
                for entry in list(self._entries.values()):
                    if entry.remove_task is not None:
                        continue
                    if self._is_expired(entry.channel):
                        await self._remove_entry(
                            entry, CHANNEL_EXPIRED, CHANNEL_EXPIRED
                        )
                    elif entry.online:
                        self._record_state(entry, "online", None)
                    else:
                        code = entry.error_code or CONNECTING
                        status = "offline" if code == CONNECTING else "error"
                        self._record_state(entry, status, code)
        except asyncio.CancelledError:
            pass

    async def _renew_entry(self, entry: _Entry) -> None:
        if (
            self._stopped
            or entry.remove_task is not None
            or self._entries.get(entry.channel_id) is not entry
        ):
            return
        if self._is_expired(entry.channel):
            await self._remove_entry(entry, CHANNEL_EXPIRED, CHANNEL_EXPIRED)
            return
        try:
            renewed = await _call(self._ownership.renew(entry.lease))
        except Exception:
            renewed = False
        if (
            self._stopped
            or entry.remove_task is not None
            or self._entries.get(entry.channel_id) is not entry
        ):
            return
        if entry.conflicted:
            await self._reconcile_conflict(entry)
        elif not renewed:
            await self._remove_entry(entry, LEASE_UNAVAILABLE, LEASE_UNAVAILABLE)
        else:
            self._arm_safety(entry)

    # -- snapshot ------------------------------------------------------

    async def _refresh_loop(self) -> None:
        try:
            while self._dirty and not self._stopped:
                self._dirty = False
                self._snapshot_cycle += 1
                cycle = self._snapshot_cycle
                success = False
                try:
                    channels = await self._config.load()
                    if not self._stopped:
                        await self._apply_snapshot(list(channels))
                    success = not self._stopped
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    code = (
                        error.code
                        if isinstance(error, GatewayError)
                        else "SNAPSHOT_UNAVAILABLE"
                    )
                    logger.warning(
                        "snapshot_failed code=%s http_status=%s",
                        code,
                        getattr(error, "status_code", None),
                    )
                finally:
                    for waiter in list(self._snapshot_waiters):
                        if waiter.after_cycle < cycle and not waiter.future.done():
                            waiter.future.set_result(success)
                            self._snapshot_waiters.discard(waiter)
        finally:
            self._refresh_task = None
            if self._dirty and not self._stopped:
                self.request_refresh()

    async def _apply_snapshot(self, channels: list[ChannelConfig]) -> None:
        try:
            snapshot = Snapshot(channels=channels)
        except ValueError as error:
            raise GatewayError("INVALID_SNAPSHOT") from error
        next_channels: dict[str, ChannelConfig] = {}
        for channel in snapshot.channels:
            if not channel.enabled:
                continue
            current = self._desired.get(channel.channel_id)
            # Compare revisions within a bot identity; rebinding the channel
            # targets a different ownership binding and its associated fences.
            if current is not None and (
                channel.connection.bot_id == current.connection.bot_id
            ):
                if channel.revision < current.revision:
                    raise GatewayError("INVALID_SNAPSHOT")
                if channel.revision == current.revision and (
                    channel.connection.secret != current.connection.secret
                    or channel.runtime.expires_at != current.runtime.expires_at
                ):
                    raise GatewayError("INVALID_SNAPSHOT")
            next_channels[channel.channel_id] = channel

        for entry in list(self._entries.values()):
            await self._reconcile_conflict(entry)

        removals = []
        for entry in list(self._entries.values()):
            target = next_channels.get(entry.channel_id)
            expired = target is not None and self._is_expired(target)
            rebound = target is not None and (
                target.connection.bot_id != entry.channel.connection.bot_id
            )
            if (
                target is None
                or rebound
                or target.revision != entry.channel.revision
                or expired
            ):
                code = CHANNEL_EXPIRED if expired else LEASE_UNAVAILABLE
                if expired:
                    reason = CHANNEL_EXPIRED
                elif target is None:
                    reason = "SNAPSHOT_ABSENT"
                elif rebound:
                    reason = "BOT_CHANGED"
                else:
                    reason = "REVISION_CHANGED"
                removals.append(self._remove_entry(entry, code, reason))
        if removals:
            await asyncio.gather(*removals, return_exceptions=True)
        if self._stopped:
            return

        self._desired = next_channels
        for channel_id, fence in list(self._expired_revisions.items()):
            channel = next_channels.get(channel_id)
            if channel is None or not _fence_blocks(fence, channel):
                self._expired_revisions.pop(channel_id, None)
        for channel_id, revision in list(self._blocked.items()):
            channel = next_channels.get(channel_id)
            if channel is None or channel.revision > revision:
                self._blocked.pop(channel_id, None)
        for channel in next_channels.values():
            entry = self._entries.get(channel.channel_id)
            if entry is not None:
                entry.channel = channel
        self._ready = True

        pending = deque(
            channel
            for channel in next_channels.values()
            if channel.channel_id not in self._entries and not self._is_expired(channel)
        )

        async def worker() -> None:
            while pending and not self._stopped:
                channel = pending.popleft()
                if self._stopped or self._is_expired(channel):
                    continue
                try:
                    lease = await _call(
                        self._ownership.acquire(
                            AcquireInput(
                                bot_id=channel.connection.bot_id,
                                channel_id=channel.channel_id,
                                revision=channel.revision,
                            )
                        )
                    )
                    self._blocked.pop(channel.channel_id, None)
                except GatewayError as error:
                    if error.code == CHANNEL_EXPIRED:
                        self._record_expired(channel)
                    elif error.code == "CHANNEL_CONNECTION_CONFLICT":
                        self._blocked[channel.channel_id] = channel.revision
                        logger.warning(
                            "channel_connection_blocked channel_id=%s revision=%s",
                            channel.channel_id,
                            channel.revision,
                        )
                    else:
                        self._metrics.inc("lease_acquire_failed")
                        logger.warning(
                            "channel_lease_acquire_failed channel_id=%s code=%s",
                            channel.channel_id,
                            error.code,
                        )
                    continue
                except Exception:
                    self._metrics.inc("lease_acquire_failed")
                    logger.warning(
                        "channel_lease_acquire_failed channel_id=%s code=%s",
                        channel.channel_id,
                        LEASE_UNAVAILABLE,
                    )
                    continue
                if lease is None:
                    continue
                if (
                    self._stopped
                    or self._is_expired(channel)
                    or not self._ownership.is_valid(lease)
                ):
                    await _call(self._ownership.release(lease))
                    continue
                entry = _Entry(channel=channel, lease=lease)
                self._entries[channel.channel_id] = entry
                self._arm_safety(entry)
                self._arm_expiry(entry)
                entry.renew_task = asyncio.create_task(self._renew_loop(entry))
                self._connect(entry)

        workers = min(OWNERSHIP_CONCURRENCY, len(pending))
        if workers:
            await asyncio.gather(*(worker() for _ in range(workers)))

    # -- connection lifecycle -----------------------------------------

    def _connect(self, entry: _Entry) -> None:
        if not self._is_current(entry):
            return
        entry.epoch += 1
        epoch = entry.epoch
        entry.online = False
        self._record_state(entry, "offline", CONNECTING)

        def current() -> bool:
            return self._is_current(entry) and entry.epoch == epoch

        class Callbacks:
            def on_ready(self) -> None:
                if not current():
                    return
                self_cancel(entry, "connect_timer")
                entry.online = True
                entry.attempts = 0
                entry.auth_failure = False
                self_metrics.inc("connections")
                self_record_state(entry, "online", None)

            def on_close(self) -> None:
                if current():
                    self_reconnect(entry, "network")

            def on_error(self, kind: str) -> None:
                if current():
                    self_reconnect(entry, kind)

            def on_message(self, frame: dict[str, Any]) -> None:
                if current() and entry.online:
                    self_start_message(entry, epoch, frame)

        self_cancel = self._cancel_task
        self_metrics = self._metrics
        self_record_state = self._record_state
        self_reconnect = self._reconnect
        self_start_message = self._start_message_task

        try:
            adapter = self._adapter_factory(
                bot_id=entry.channel.connection.bot_id,
                secret=entry.channel.connection.secret,
                callbacks=Callbacks(),
            )
        except Exception:
            logger.warning(
                "channel_adapter_failed channel_id=%s code=NETWORK_ERROR",
                entry.channel_id,
            )
            self._reconnect(entry, "network")
            return
        entry.adapter = adapter

        async def do_connect() -> None:
            try:
                await adapter.connect()
            except Exception:
                if current():
                    self._reconnect(entry, "network")

        entry.connect_task = asyncio.create_task(do_connect())
        entry.connect_timer = asyncio.create_task(self._connect_watchdog(entry, epoch))

    async def _connect_watchdog(self, entry: _Entry, epoch: int) -> None:
        try:
            await asyncio.sleep(self._settings.connect_timeout_seconds)
            if entry.epoch == epoch and not entry.online and self._is_current(entry):
                self._reconnect(entry, "network")
        except asyncio.CancelledError:
            pass

    def _reconnect(self, entry: _Entry, kind: str = "network") -> None:
        if kind == "replaced":
            entry.conflicted = True
            self._blocked[entry.channel_id] = entry.channel.revision
            self._invalidate(entry)
            self._record_state(entry, "error", CONNECTION_CONFLICT)
            logger.warning(
                "channel_connection_conflict channel_id=%s revision=%s",
                entry.channel_id,
                entry.channel.revision,
            )
            self._persist_conflict(entry)
            return

        self._invalidate(entry)
        self._metrics.inc("reconnects")
        self._record_state(
            entry, "error", AUTH_FAILURE if kind == "auth" else NETWORK_ERROR
        )
        if not self._is_current(entry):
            return
        entry.auth_failure = entry.auth_failure or kind == "auth"
        if entry.auth_failure:
            delay = AUTH_RETRY_SECONDS
        else:
            delay = min(
                RECONNECT_BASE_SECONDS * 2 ** min(entry.attempts, 5),
                RECONNECT_MAX_SECONDS,
            )
        entry.attempts += 1

        async def retry() -> None:
            try:
                await asyncio.sleep(delay)
                if not self._is_current(entry):
                    await self._remove_entry(entry)
                    return
                try:
                    renewed = await _call(self._ownership.renew(entry.lease))
                except Exception:
                    renewed = False
                if not renewed:
                    await self._remove_entry(
                        entry, LEASE_UNAVAILABLE, LEASE_UNAVAILABLE
                    )
                    return
                self._arm_safety(entry)
                self._connect(entry)
            except asyncio.CancelledError:
                pass

        entry.retry_task = asyncio.create_task(retry())
        logger.info(
            "channel_reconnecting channel_id=%s revision=%s code=%s retry_in=%.1f",
            entry.channel_id,
            entry.channel.revision,
            kind,
            delay,
        )

    def _persist_conflict(self, entry: _Entry) -> asyncio.Task | None:
        if entry.conflict_task is not None and not entry.conflict_task.done():
            return entry.conflict_task

        async def work() -> None:
            try:
                blocked = await _call(self._ownership.block(entry.lease))
            except Exception:
                logger.warning(
                    "channel_conflict_persist_failed channel_id=%s code=LEASE_UNAVAILABLE",
                    entry.channel_id,
                )
                return
            if blocked:
                await self._remove_entry(
                    entry, CONNECTION_CONFLICT, CONNECTION_CONFLICT
                )
            else:
                logger.warning(
                    "channel_conflict_persist_failed channel_id=%s code=LEASE_LOST",
                    entry.channel_id,
                )

        entry.conflict_task = asyncio.create_task(work())
        return entry.conflict_task

    async def _reconcile_conflict(self, entry: _Entry) -> None:
        if not entry.conflicted or self._stopped:
            return
        if entry.remove_task is not None:
            await entry.remove_task
            return
        if self._entries.get(entry.channel_id) is not entry:
            return
        try:
            recovered = await _call(self._ownership.has_recovered(entry.lease))
        except Exception:
            recovered = False
        if (
            self._stopped
            or entry.remove_task is not None
            or self._entries.get(entry.channel_id) is not entry
        ):
            return
        if recovered:
            await self._remove_entry(entry)
        elif self._ownership.is_valid(entry.lease):
            self._persist_conflict(entry)

    def _invalidate(self, entry: _Entry) -> None:
        entry.epoch += 1
        if entry.online:
            entry.online = False
            self._metrics.dec("connections")
        self._cancel_task(entry, "retry_task")
        self._cancel_task(entry, "connect_task")
        self._cancel_task(entry, "connect_timer")
        for controller in list(entry.controllers):
            controller.set()
        entry.controllers.clear()
        adapter = entry.adapter
        entry.adapter = None
        if adapter is not None:
            try:
                adapter.close()
            except Exception:
                pass

    @staticmethod
    def _cancel_task(entry: _Entry, attribute: str) -> None:
        task = getattr(entry, attribute)
        if task is not None and not task.done():
            task.cancel()
        setattr(entry, attribute, None)

    def _is_current(self, entry: _Entry) -> bool:
        return (
            not self._stopped
            and entry.remove_task is None
            and not entry.conflicted
            and not self._is_expired(entry.channel)
            and self._entries.get(entry.channel_id) is entry
            and self._ownership.is_valid(entry.lease)
        )

    def _is_expired(self, channel: ChannelConfig) -> bool:
        if _fence_blocks(self._expired_revisions.get(channel.channel_id), channel):
            return True
        expires_at = channel.runtime.expires_at
        return expires_at is not None and expires_at <= datetime.now(timezone.utc)

    def _record_expired(self, channel: ChannelConfig) -> None:
        bot_id = channel.connection.bot_id
        fence = self._expired_revisions.get(channel.channel_id)
        revision = channel.revision
        if fence is not None and fence[0] == bot_id:
            revision = max(revision, fence[1])
        self._expired_revisions[channel.channel_id] = (bot_id, revision)

    def _arm_safety(self, entry: _Entry) -> None:
        self._cancel_task(entry, "safety_task")
        if entry.lease.safe_until == float("inf"):
            return

        async def wait() -> None:
            try:
                await asyncio.sleep(max(0.0, entry.lease.safe_until - monotonic()))
                if not entry.conflicted:
                    await self._remove_entry(entry)
            except asyncio.CancelledError:
                pass

        entry.safety_task = asyncio.create_task(wait())

    def _arm_expiry(self, entry: _Entry) -> None:
        self._cancel_task(entry, "expiry_task")
        expires_at = entry.channel.runtime.expires_at
        if expires_at is None:
            return
        remaining = (expires_at - datetime.now(timezone.utc)).total_seconds()

        async def wait() -> None:
            try:
                await asyncio.sleep(max(0.0, remaining))
                await self._remove_entry(entry, CHANNEL_EXPIRED, CHANNEL_EXPIRED)
            except asyncio.CancelledError:
                pass

        entry.expiry_task = asyncio.create_task(wait())

    async def _remove_entry(
        self,
        entry: _Entry,
        error_code: str = LEASE_UNAVAILABLE,
        reason: str | None = None,
    ) -> None:
        if error_code == CHANNEL_EXPIRED:
            self._record_expired(entry.channel)
        if entry.remove_task is not None:
            await asyncio.shield(entry.remove_task)
            return

        entry.remove_task = asyncio.create_task(
            self._remove_entry_impl(entry, error_code, reason or error_code)
        )
        # 定时器触发的移除会在实现里取消自身；shield 保证清理事务完整执行。
        await asyncio.shield(entry.remove_task)

    async def _remove_entry_impl(
        self, entry: _Entry, error_code: str, reason: str
    ) -> None:
        renewal = entry.renew_task
        self._cancel_task(entry, "renew_task")
        if renewal is not None:
            await asyncio.gather(renewal, return_exceptions=True)
        self._cancel_task(entry, "safety_task")
        self._cancel_task(entry, "expiry_task")
        self._invalidate(entry)
        logger.info(
            "channel_closed channel_id=%s revision=%s reason=%s code=%s",
            entry.channel_id,
            entry.channel.revision,
            reason,
            error_code,
        )
        status = "error" if error_code == CONNECTION_CONFLICT else "offline"
        task = self._record_state(entry, status, error_code)
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        if self._entries.get(entry.channel_id) is entry:
            self._entries.pop(entry.channel_id, None)
        try:
            await _call(self._ownership.release(entry.lease))
        except Exception:
            logger.warning(
                "channel_lease_release_failed channel_id=%s code=LEASE_UNAVAILABLE",
                entry.channel_id,
            )

    # -- status reporting ---------------------------------------------

    def _record_state(
        self, entry: _Entry, status: str, error_code: str | None
    ) -> asyncio.Task | None:
        if error_code in _UNREPORTED_CODES:
            return entry.status_task
        entry.error_code = None if status == "online" else (error_code or NETWORK_ERROR)
        epoch = entry.epoch
        previous = entry.status_task
        task = asyncio.create_task(
            self._write_state(entry, epoch, status, error_code, previous)
        )
        entry.status_task = task
        return task

    async def _write_state(
        self,
        entry: _Entry,
        epoch: int,
        status: str,
        error_code: str | None,
        previous: asyncio.Task | None,
    ) -> None:
        if previous is not None:
            try:
                await previous
            except Exception:
                pass
        if (
            self._entries.get(entry.channel_id) is not entry
            or entry.epoch != epoch
            or not self._ownership.is_valid(entry.lease)
        ):
            return
        if status == "online" and (not entry.online or not self._is_current(entry)):
            return
        write = _platform_write(status, error_code)
        if write is None:
            return
        try:
            state = await _call(self._ownership.publish_status(entry.lease, *write))
        except Exception:
            logger.warning(
                "channel_status_write_failed channel_id=%s code=LEASE_UNAVAILABLE",
                entry.channel_id,
            )
            return
        if state is None:
            return
        platform_status, platform_error = self._platform_state(state)
        try:
            await self._status.publish(
                entry.channel_id,
                entry.channel.revision,
                platform_status,
                platform_error,
            )
        except Exception:
            logger.warning(
                "status_report_failed channel_id=%s code=STATUS_UNAVAILABLE",
                entry.channel_id,
            )

    @staticmethod
    def _platform_state(state: StatusState) -> tuple[str, str | None]:
        if state.status == "online":
            return "online", None
        if state.status == "error":
            return "error", state.error_code
        return "offline", None

    # -- message scheduling -------------------------------------------

    def _start_message_task(
        self, entry: _Entry, epoch: int, frame: dict[str, Any]
    ) -> None:
        task = asyncio.create_task(self._handle_message(entry, epoch, frame))
        self._message_tasks.add(task)

        def done(completed: asyncio.Task) -> None:
            self._message_tasks.discard(completed)
            if not completed.cancelled() and completed.exception() is not None:
                logger.warning(
                    "message_task_failed channel_id=%s code=MESSAGE_TASK_FAILED",
                    entry.channel_id,
                )

        task.add_done_callback(done)

    async def _handle_message(
        self, entry: _Entry, epoch: int, frame: dict[str, Any]
    ) -> None:
        if not self._is_current(entry) or not entry.online or entry.epoch != epoch:
            return
        message: NormalizedMessage | None
        notice: MessageNotice | None
        try:
            message = normalize_message(
                entry.channel_id,
                entry.channel.connection.bot_id,
                frame,
                self._settings.max_message_bytes,
            )
            notice = None
        except InvalidFrame:
            logger.info(
                "message_rejected channel_id=%s code=INVALID_MESSAGE",
                entry.channel_id,
            )
            return
        except MessageNotice as caught:
            message = None
            notice = caught

        self._metrics.inc("messages_received")
        if message is not None:
            event_id = message.event_id
            req_id = message.req_id
            conversation_key = message.user_key
            idempotency_key = message.idempotency_key
        else:
            assert notice is not None
            event_id = notice.event_id
            req_id = notice.req_id
            conversation_key = notice.user_key
            idempotency_key = notice.idempotency_key
        if not event_id or not req_id:
            return

        now = monotonic()
        while self._completed:
            oldest = next(iter(self._completed.values()))
            if oldest.expires_at > now:
                break
            self._completed.popitem(last=False)

        context = f"{entry.lease.generation}:{epoch}:{req_id}"
        running = self._in_flight.get(event_id)
        if running is not None:
            self._metrics.inc("messages_duplicate")
            if running.context != context:
                running.pending = (entry, epoch, frame)
            return
        completed = self._completed.get(event_id)
        if completed is not None and completed.settled_context == context:
            self._metrics.inc("messages_duplicate")
            return

        busy = (
            (completed is not None and completed.kind == "busy")
            or conversation_key in self._conversations
            or self._active_tasks >= self._settings.max_concurrent
        )
        revision = (
            completed.revision if completed is not None else entry.channel.revision
        )
        kind = (
            completed.kind
            if completed is not None
            else ("busy" if busy else ("notice" if message is None else "workflow"))
        )
        notice_code = (
            completed.notice_code
            if completed is not None
            else (notice.code if notice is not None else None)
        )
        reply = _Reply(
            entry,
            epoch,
            req_id,
            now,
            settings=self._settings,
            metrics=self._metrics,
            is_current=self._is_current,
        )
        task = _InFlight(context)
        self._in_flight[event_id] = task
        # Disconnects, removals and shutdown set every registered controller, so
        # the platform stream is torn down instead of running until its timeout.
        entry.controllers.add(reply.cancel)
        if not busy:
            self._conversations.add(conversation_key)
            self._active_tasks += 1
        fallback = entry.channel.runtime.default_response or DEFAULT_RESPONSE
        try:
            if busy:
                await reply.send_terminal(BUSY_TEXT)
            elif notice_code is not None:
                if notice_code == NOTICE_UNSUPPORTED_MESSAGE:
                    self._metrics.inc("messages_unsupported")
                logger.info(
                    "message_rejected channel_id=%s code=%s",
                    entry.channel_id,
                    notice_code,
                )
                await reply.send_terminal(NOTICE_REPLIES.get(notice_code, fallback))
            else:
                assert message is not None
                await self._run_workflow(reply, message, entry, idempotency_key)
        except Exception as error:
            await self._handle_message_failure(
                reply, entry, event_id, revision, error, kind, fallback
            )
        finally:
            entry.controllers.discard(reply.cancel)
            self._in_flight.pop(event_id, None)
            if not busy:
                self._conversations.discard(conversation_key)
                self._active_tasks -= 1
            self._completed.pop(event_id, None)
            self._completed[event_id] = _Completed(
                revision=revision,
                expires_at=monotonic() + self._settings.dedup_ttl_seconds,
                kind=kind,
                notice_code=notice_code,
                settled_context=(
                    context
                    if (
                        reply.sent_final
                        or reply.terminal_attempted
                        or reply.reply_rejected
                    )
                    else None
                ),
            )
            while len(self._completed) > self._settings.dedup_max_entries:
                self._completed.popitem(last=False)
            if task.pending is not None:
                self._start_message_task(*task.pending)

    async def _run_workflow(
        self,
        reply: _Reply,
        message: NormalizedMessage,
        entry: _Entry,
        idempotency_key: str,
    ) -> None:
        revision = entry.channel.revision
        stale_retry = False
        attempt = 0
        while attempt < 3:
            try:
                await self._stream_once(
                    reply, message, entry, revision, idempotency_key, attempt
                )
                return
            except GatewayError as error:
                if error.code == "CHANNEL_NOT_FOUND":
                    await self._remove_entry(entry)
                    self.request_refresh()
                    raise
                if error.code == "CHANNEL_UNAVAILABLE":
                    self.request_refresh()
                    raise
                if error.code == CONNECTION_CONFLICT or error.status_code == 409:
                    if attempt > 0:
                        raise GatewayError("CHANNEL_RUN_FAILED") from error
                    await self.refresh()
                    if not reply.valid():
                        raise GatewayError(WORKFLOW_CANCELLED) from error
                    if not stale_retry:
                        stale_retry = True
                        revision = entry.channel.revision
                        continue
                    await reply.send_terminal(BUSY_TEXT)
                    return
                if (
                    error.retryable
                    and not reply.reply_failed
                    and not reply.terminal_attempted
                    and attempt < 2
                    and reply.valid()
                ):
                    attempt += 1
                    await asyncio.sleep(MESSAGE_RETRY_DELAY_SECONDS)
                    continue
                raise

    async def _stream_once(
        self,
        reply: _Reply,
        message: NormalizedMessage,
        entry: _Entry,
        revision: int,
        idempotency_key: str,
        attempt: int,
    ) -> None:
        iterator = self._workflow.run(
            channel_id=entry.channel_id,
            revision=revision,
            user_key=message.user_key,
            text=message.text,
            idempotency_key=idempotency_key,
            signal=reply.cancel,
        ).__aiter__()
        started = False
        keepalive: asyncio.Task | None = None
        try:
            while True:
                remaining = reply.deadline - monotonic()
                if not started:
                    remaining = min(remaining, reply.first_deadline - monotonic())
                if remaining <= 0:
                    raise GatewayError(
                        WORKFLOW_TIMEOUT if started else "FIRST_EVENT_TIMEOUT"
                    )
                try:
                    event = await self._next_event(iterator, reply.cancel, remaining)
                except StopAsyncIteration:
                    raise GatewayError(STREAM_INTERRUPTED, retryable=True) from None
                except asyncio.TimeoutError:
                    raise GatewayError(
                        WORKFLOW_TIMEOUT if started else "FIRST_EVENT_TIMEOUT"
                    ) from None
                if not reply.valid():
                    raise GatewayError(WORKFLOW_CANCELLED)
                event_type = event.get("type")
                if event_type == "duplicate":
                    if attempt > 0:
                        await reply.send_terminal(
                            entry.channel.runtime.default_response or DEFAULT_RESPONSE
                        )
                    else:
                        reply.sent_final = True
                    return
                if event_type == "start":
                    if started:
                        raise GatewayError("INVALID_STREAM_EVENT")
                    started = True
                    await reply.send(THINKING_TEXT, False)
                    keepalive = asyncio.create_task(self._keepalive(reply))
                elif event_type == "chunk":
                    reply.content += str(event.get("content", ""))
                    encoded = reply.content.encode("utf-8")
                    if len(encoded) > self._settings.max_answer_bytes:
                        raise GatewayError(ANSWER_TOO_LARGE)
                    if len(encoded) > self._settings.reply_max_bytes:
                        # 终态仍需完整读取后再截断发送，中间帧不再刷新。
                        continue
                    elapsed_ms = (monotonic() - reply.last_sent_at) * 1000
                    if elapsed_ms >= self._settings.stream_interval_ms:
                        await reply.send(reply.content, False)
                        reply.sent_any_content = bool(reply.content.strip())
                elif event_type == "done":
                    content = str(event.get("content", ""))
                    if not content.strip():
                        raise GatewayError("CHANNEL_WORKFLOW_FAILED")
                    if len(content.encode("utf-8")) > self._settings.max_answer_bytes:
                        raise GatewayError(ANSWER_TOO_LARGE)
                    reply.content = content
                    await reply.send(content, True)
                    reply.sent_final = True
                    return
                elif event_type == "error":
                    raise GatewayError("CHANNEL_WORKFLOW_FAILED")
                else:
                    raise GatewayError("INVALID_STREAM_EVENT")
        finally:
            if keepalive is not None:
                keepalive.cancel()
                await asyncio.gather(keepalive, return_exceptions=True)
            try:
                await iterator.aclose()
            except Exception:
                pass

    async def _keepalive(self, reply: _Reply) -> None:
        interval = self._settings.stream_keepalive_seconds
        try:
            while True:
                await asyncio.sleep(interval)
                if not reply.valid():
                    return
                if monotonic() - reply.last_sent_at < interval:
                    continue
                if not reply.content:
                    continue
                try:
                    await reply.send(reply.content, False)
                    reply.sent_any_content = True
                except Exception:
                    return
        except asyncio.CancelledError:
            pass

    async def _next_event(
        self,
        iterator: Any,
        cancel: asyncio.Event,
        timeout: float,
    ) -> dict[str, Any]:
        next_task = asyncio.create_task(iterator.__anext__())
        cancel_task = asyncio.create_task(cancel.wait())
        try:
            done, _ = await asyncio.wait(
                {next_task, cancel_task},
                timeout=max(0.0, timeout),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if next_task in done:
                return next_task.result()
            next_task.cancel()
            await asyncio.gather(next_task, return_exceptions=True)
            if cancel_task in done:
                raise GatewayError(WORKFLOW_CANCELLED)
            raise asyncio.TimeoutError
        finally:
            cancel_task.cancel()
            await asyncio.gather(cancel_task, return_exceptions=True)

    async def _handle_message_failure(
        self,
        reply: _Reply,
        entry: _Entry,
        event_id: str,
        revision: int,
        error: BaseException,
        kind: str,
        fallback: str,
    ) -> None:
        failure = (
            error
            if isinstance(error, GatewayError)
            else GatewayError("WORKFLOW_FAILED")
        )
        cancelled = failure.code == WORKFLOW_CANCELLED or reply.cancel.is_set()
        if kind == "workflow" and not cancelled:
            self._metrics.inc("workflow_errors")
        logger.warning(
            "message_failed channel_id=%s event_id=%s revision=%s code=%s provider_errcode=%s",
            entry.channel_id,
            event_id,
            revision,
            failure.code,
            failure.provider_errcode,
        )
        if kind != "workflow" or cancelled:
            return
        if reply.reply_rejected or reply.terminal_attempted:
            return
        if not reply.valid(terminal=True):
            return
        if failure.code == WORKFLOW_REQUEST_TOO_LARGE:
            content = NOTICE_REPLIES[NOTICE_INVALID_MESSAGE]
        elif reply.sent_any_content and reply.content.strip():
            content = reply.content + "\n\n" + INCOMPLETE_HINT
        else:
            content = fallback
        try:
            await reply.send_terminal(content)
        except Exception:
            pass
