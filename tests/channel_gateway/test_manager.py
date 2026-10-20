import asyncio
import inspect
from collections import deque
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from time import monotonic
from typing import Any

import pytest

from app.channel_gateway import manager as manager_module
from app.channel_gateway.config import GatewaySettings
from app.channel_gateway.manager import (
    BUSY_TEXT,
    DEFAULT_RESPONSE,
    INCOMPLETE_HINT,
    THINKING_TEXT,
    ChannelManager,
)
from app.channel_gateway.metrics import Metrics
from app.channel_gateway.ownership import (
    AcquireInput,
    LocalOwnershipManager,
    Lease,
)
from app.channel_gateway.protocol import ChannelConfig, GatewayError

TOKENS = {
    "NIANLUN_INTERNAL_TOKEN": "i" * 32,
    "NIANLUN_GATEWAY_CONTROL_TOKEN": "c" * 32,
}


def make_settings(**overrides: Any) -> GatewaySettings:
    base = GatewaySettings.from_env(
        {
            **TOKENS,
            "GATEWAY_SNAPSHOT_INTERVAL_SECONDS": "3600",
            "GATEWAY_STATUS_HEARTBEAT_SECONDS": "3600",
            "GATEWAY_CONNECT_TIMEOUT_SECONDS": "2",
            "GATEWAY_FIRST_EVENT_TIMEOUT_SECONDS": "1",
            "GATEWAY_WORKFLOW_TIMEOUT_SECONDS": "2",
            "GATEWAY_STREAM_INTERVAL_MS": "1",
            "GATEWAY_STREAM_KEEPALIVE_SECONDS": "30",
            "GATEWAY_DEDUP_TTL_SECONDS": "5",
        }
    )
    return replace(base, **overrides)


def make_channel(
    channel_id: str = "ch1",
    bot_id: str = "bot1",
    revision: int = 1,
    secret: str = "secret-1",
    default_response: str = "服务暂时不可用，请稍后再试",
    expires_at: datetime | None = None,
    enabled: bool = True,
) -> ChannelConfig:
    return ChannelConfig.model_validate(
        {
            "channel_id": channel_id,
            "application_id": "app-1",
            "revision": revision,
            "enabled": enabled,
            "connection": {"bot_id": bot_id, "secret": secret},
            "runtime": {
                "default_response": default_response,
                "expires_at": expires_at,
            },
        }
    )


def make_frame(
    msgid: str = "msg-1",
    content: str = "你好",
    req_id: str = "req-1",
    userid: str = "user-1",
    chattype: str = "single",
    chatid: str = "chat-1",
    bot_id: str = "bot1",
    msgtype: str = "text",
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "aibotid": bot_id,
        "msgid": msgid,
        "chattype": chattype,
        "from": {"userid": userid},
        "msgtype": msgtype,
    }
    if chattype == "group":
        body["chatid"] = chatid
    if msgtype == "text":
        body["text"] = {"content": content}
    return {"cmd": "aibot_msg_callback", "headers": {"req_id": req_id}, "body": body}


class FakeConfig:
    def __init__(self) -> None:
        self.channels: list[ChannelConfig] = []
        self.error: Exception | None = None
        self.loads = 0

    async def load(self, signal: Any = None) -> list[ChannelConfig]:
        self.loads += 1
        if self.error is not None:
            raise self.error
        return list(self.channels)


class FakeStatus:
    def __init__(self) -> None:
        self.publishes: list[tuple[str, int, str, str | None]] = []
        self.error: Exception | None = None

    async def publish(
        self, channel_id: str, revision: int, status: str, error_code: str | None
    ) -> None:
        if self.error is not None:
            raise self.error
        self.publishes.append((channel_id, revision, status, error_code))


class FakeWorkflow:
    def __init__(self) -> None:
        self.scripts: deque[list[Any]] = deque()
        self.runs: list[dict[str, Any]] = []
        self.closed = 0

    def add(self, *items: Any) -> None:
        self.scripts.append(list(items))

    async def run(self, **kwargs: Any):
        self.runs.append(kwargs)
        items = (
            self.scripts.popleft()
            if self.scripts
            else [
                {"type": "done", "content": "默认答案", "run_id": "run-1"},
            ]
        )
        try:
            for item in items:
                if item == "WAIT":
                    await asyncio.Event().wait()
                elif item == "RETURN":
                    return
                elif isinstance(item, BaseException):
                    raise item
                elif callable(item):
                    result = item()
                    if inspect.isawaitable(result):
                        await result
                elif isinstance(item, dict):
                    yield item
                else:
                    raise AssertionError(f"unsupported script item {item!r}")
        finally:
            self.closed += 1

    def add_done(self, content: str = "答案") -> None:
        self.add({"type": "done", "content": content, "run_id": "run-1"})


class FakeOwnership(LocalOwnershipManager):
    def __init__(self) -> None:
        super().__init__("test-gateway")
        self.acquire_calls: list[AcquireInput] = []
        self.renew_calls = 0
        self.renew_result = True
        self.release_calls: list[Lease] = []
        self.publish_calls: list[tuple[str, str, str | None]] = []
        self.block_calls: list[str] = []
        self.events: list[tuple[Any, ...]] = []
        self.closed = False

    def acquire(self, input: AcquireInput) -> Lease | None:
        self.acquire_calls.append(input)
        return super().acquire(input)

    def renew(self, lease: Lease) -> bool:
        self.renew_calls += 1
        if not self.renew_result:
            return False
        return super().renew(lease)

    def release(self, lease: Lease) -> None:
        self.release_calls.append(lease)
        self.events.append(("release", lease.channel_id, lease.revision))
        super().release(lease)

    def publish_status(self, lease, status, error_code):
        state = super().publish_status(lease, status, error_code)
        self.publish_calls.append((lease.channel_id, status, error_code))
        self.events.append(("publish", lease.channel_id, status, error_code))
        return state

    def block(self, lease: Lease) -> bool:
        self.block_calls.append(lease.channel_id)
        return super().block(lease)

    def close(self) -> None:
        self.closed = True
        super().close()


class FakeAdapter:
    def __init__(
        self, bot_id: str, secret: str, callbacks: Any, factory: "FakeAdapterFactory"
    ) -> None:
        self.bot_id = bot_id
        self.secret = secret
        self.callbacks = callbacks
        self.factory = factory
        self.connected = False
        self.closed = False
        self.reply_error: Exception | None = None
        self.replies: list[dict[str, Any]] = []
        factory.instances.append(self)

    async def connect(self) -> None:
        if self.factory.connect_error is not None:
            raise self.factory.connect_error
        if self.factory.connect_blocker is not None:
            await self.factory.connect_blocker.wait()
        self.connected = True
        if self.factory.auto_ready:
            self.callbacks.on_ready()

    async def reply(
        self, *, req_id: str, stream_id: str, content: str, finish: bool
    ) -> None:
        if self.closed:
            raise GatewayError("WECOM_NOT_CONNECTED")
        if self.reply_error is not None:
            raise self.reply_error
        self.replies.append(
            {
                "req_id": req_id,
                "stream_id": stream_id,
                "content": content,
                "finish": finish,
            }
        )

    def close(self) -> None:
        self.closed = True


class FakeAdapterFactory:
    def __init__(self, auto_ready: bool = True) -> None:
        self.auto_ready = auto_ready
        self.connect_error: Exception | None = None
        self.connect_blocker: asyncio.Event | None = None
        self.instances: list[FakeAdapter] = []

    def __call__(self, **kwargs: Any) -> FakeAdapter:
        return FakeAdapter(factory=self, **kwargs)


class Harness:
    def __init__(self, **settings_overrides: Any) -> None:
        self.settings = make_settings(**settings_overrides)
        self.config = FakeConfig()
        self.workflow = FakeWorkflow()
        self.status = FakeStatus()
        self.ownership = FakeOwnership()
        self.factory = FakeAdapterFactory()
        self.manager = ChannelManager(
            self.settings,
            self.config,
            self.workflow,
            self.status,
            self.ownership,
            self.factory,
            metrics=Metrics(),
        )


async def wait_until(predicate, timeout: float = 3.0) -> None:
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("condition not met before timeout")


async def online_entry(harness: Harness, channel_id: str = "ch1", revision: int = 1):
    await wait_until(
        lambda: (
            channel_id in harness.manager._entries
            and harness.manager._entries[channel_id].online
            and harness.manager._entries[channel_id].channel.revision == revision
            and (channel_id, revision, "online", None) in harness.status.publishes
        )
    )
    return harness.manager._entries[channel_id]


async def send_frame(harness: Harness, frame: dict[str, Any]) -> FakeAdapter:
    adapter = harness.factory.instances[-1]
    adapter.callbacks.on_message(frame)
    await asyncio.sleep(0)
    return adapter


async def drain(harness: Harness, timeout: float = 3.0) -> None:
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        tasks = [task for task in harness.manager._message_tasks if not task.done()]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
            continue
        await asyncio.sleep(0)
        if not harness.manager._message_tasks:
            return
    raise AssertionError("message tasks did not settle")


def test_snapshot_connects_and_stop_reports_offline():
    async def scenario() -> None:
        harness = Harness()
        try:
            harness.config.channels = [make_channel()]
            await harness.manager.refresh()
            entry = await online_entry(harness)
            adapter = harness.factory.instances[0]

            assert adapter.bot_id == "bot1"
            assert adapter.secret == "secret-1"
            assert adapter.connected
            assert harness.ownership.acquire_calls == [
                AcquireInput(bot_id="bot1", channel_id="ch1", revision=1)
            ]
            assert harness.manager.status() == {
                "ready": True,
                "channels": 1,
                "online": 1,
                "active_tasks": 0,
            }
            assert harness.manager._metrics.snapshot()["connections"] == 1
            assert entry.lease.safe_until == float("inf")

            await harness.manager.stop()

            assert adapter.closed
            assert harness.manager.status()["online"] == 0
            assert harness.status.publishes[-1] == ("ch1", 1, "offline", None)
            assert harness.ownership.events[-2:] == [
                ("publish", "ch1", "offline", "GATEWAY_STOPPED"),
                ("release", "ch1", 1),
            ]
            assert harness.ownership.closed
        finally:
            await harness.manager.stop()

    asyncio.run(scenario())


def test_start_loads_initial_snapshot_and_stop_clears_ready():
    async def scenario() -> None:
        harness = Harness()
        try:
            harness.config.channels = [make_channel()]
            await harness.manager.start()
            await online_entry(harness)
            assert harness.manager.status()["ready"] is True
        finally:
            await harness.manager.stop()
        assert harness.manager.status()["ready"] is False

    asyncio.run(scenario())


def test_snapshot_revision_change_replaces_connection():
    async def scenario() -> None:
        harness = Harness()
        try:
            harness.config.channels = [make_channel(revision=1)]
            await harness.manager.refresh()
            first = await online_entry(harness, revision=1)
            first_adapter = harness.factory.instances[0]

            harness.config.channels = [make_channel(revision=2)]
            await harness.manager.refresh()
            second = await online_entry(harness, revision=2)

            assert first is not second
            assert first_adapter.closed
            assert first.lease.token != second.lease.token
            assert len(harness.ownership.release_calls) == 1
            assert harness.ownership.release_calls[0].revision == 1
            assert harness.ownership.acquire_calls[-1].revision == 2
            assert harness.status.publishes[-1] == ("ch1", 2, "online", None)
        finally:
            await harness.manager.stop()

    asyncio.run(scenario())


def test_snapshot_removal_disconnects_and_releases():
    async def scenario() -> None:
        harness = Harness()
        try:
            harness.config.channels = [make_channel()]
            await harness.manager.refresh()
            await online_entry(harness)
            adapter = harness.factory.instances[0]

            harness.config.channels = []
            await harness.manager.refresh()

            assert harness.manager._entries == {}
            assert adapter.closed
            assert len(harness.ownership.release_calls) == 1
        finally:
            await harness.manager.stop()

    asyncio.run(scenario())


def test_snapshot_bot_change_rebinds_connection():
    async def scenario() -> None:
        harness = Harness()
        try:
            harness.config.channels = [make_channel(bot_id="bot1", revision=5)]
            await harness.manager.refresh()
            first = await online_entry(harness, revision=5)
            first_adapter = harness.factory.instances[0]

            harness.config.channels = [
                make_channel(bot_id="bot2", revision=1, secret="secret-2")
            ]
            await harness.manager.refresh()
            second = await online_entry(harness, revision=1)

            assert first is not second
            assert first_adapter.closed
            assert harness.factory.instances[-1].bot_id == "bot2"
            assert harness.factory.instances[-1].secret == "secret-2"
            assert harness.ownership.acquire_calls[-1] == AcquireInput(
                bot_id="bot2", channel_id="ch1", revision=1
            )
            assert harness.manager._desired["ch1"].connection.bot_id == "bot2"
        finally:
            await harness.manager.stop()

    asyncio.run(scenario())


def test_expired_fence_is_scoped_to_bot_identity():
    async def scenario() -> None:
        harness = Harness()
        try:
            other = harness.ownership.acquire(
                AcquireInput(bot_id="bot1", channel_id="ch1", revision=3)
            )
            assert other is not None
            harness.ownership.publish_status(other, "offline", "CHANNEL_EXPIRED")
            harness.ownership.release(other)

            harness.config.channels = [make_channel(bot_id="bot1", revision=3)]
            await harness.manager.refresh()
            assert harness.manager._entries == {}
            assert harness.manager._expired_revisions == {"ch1": ("bot1", 3)}

            harness.config.channels = [
                make_channel(bot_id="bot2", revision=1, secret="secret-2")
            ]
            await harness.manager.refresh()
            await online_entry(harness, revision=1)
            assert harness.manager._expired_revisions == {}
        finally:
            await harness.manager.stop()

    asyncio.run(scenario())


def test_expired_channel_at_startup_is_skipped():
    async def scenario() -> None:
        harness = Harness()
        try:
            harness.config.channels = [
                make_channel(
                    expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)
                )
            ]
            await harness.manager.refresh()

            assert harness.manager._entries == {}
            assert harness.ownership.acquire_calls == []
            assert harness.factory.instances == []
            assert harness.manager.status() == {
                "ready": True,
                "channels": 1,
                "online": 0,
                "active_tasks": 0,
            }
        finally:
            await harness.manager.stop()

    asyncio.run(scenario())


def test_expiry_timer_publishes_expired_before_release_and_does_not_reconnect():
    async def scenario() -> None:
        harness = Harness()
        try:
            expires_at = datetime.now(timezone.utc) + timedelta(milliseconds=80)
            harness.config.channels = [make_channel(expires_at=expires_at)]
            await harness.manager.refresh()
            await online_entry(harness)
            adapter = harness.factory.instances[0]

            await wait_until(lambda: harness.manager._entries == {})

            assert adapter.closed
            assert harness.ownership.events[-2:] == [
                ("publish", "ch1", "offline", "CHANNEL_EXPIRED"),
                ("release", "ch1", 1),
            ]
            assert harness.status.publishes[-1] == ("ch1", 1, "offline", None)
            acquire_count = len(harness.ownership.acquire_calls)

            await harness.manager.refresh()
            assert harness.manager._entries == {}
            assert len(harness.ownership.acquire_calls) == acquire_count
        finally:
            await harness.manager.stop()

    asyncio.run(scenario())


def test_invalid_snapshot_keeps_existing_connection():
    async def scenario() -> None:
        harness = Harness()
        try:
            harness.config.channels = [make_channel(revision=2)]
            await harness.manager.refresh()
            entry = await online_entry(harness, revision=2)
            adapter = harness.factory.instances[0]

            harness.config.channels = [make_channel(revision=1)]
            await harness.manager.refresh()

            assert harness.manager._entries["ch1"] is entry
            assert entry.online
            assert not adapter.closed
            assert harness.manager._desired["ch1"].revision == 2

            harness.config.channels = [make_channel(revision=2, secret="rotated")]
            await harness.manager.refresh()

            assert harness.manager._entries["ch1"] is entry
            assert harness.manager._desired["ch1"].connection.secret == "secret-1"
        finally:
            await harness.manager.stop()

    asyncio.run(scenario())


def test_snapshot_failure_keeps_existing_connection():
    async def scenario() -> None:
        harness = Harness()
        try:
            harness.config.channels = [make_channel()]
            await harness.manager.refresh()
            entry = await online_entry(harness)
            adapter = harness.factory.instances[0]

            harness.config.error = GatewayError("CONFIG_UNAVAILABLE")
            await harness.manager.refresh()

            assert harness.manager._entries["ch1"] is entry
            assert entry.online
            assert not adapter.closed
        finally:
            await harness.manager.stop()

    asyncio.run(scenario())


def test_ownership_conflict_blocks_same_revision_and_recovers_on_new_revision():
    async def scenario() -> None:
        harness = Harness()
        try:
            other = harness.ownership.acquire(
                AcquireInput(bot_id="bot1", channel_id="ch1", revision=1)
            )
            assert other is not None
            assert harness.ownership.block(other)
            harness.ownership.release(other)

            harness.config.channels = [make_channel(revision=1)]
            await harness.manager.refresh()
            assert harness.manager._entries == {}
            assert harness.manager._blocked == {"ch1": 1}
            assert harness.factory.instances == []

            await harness.manager.refresh()
            assert harness.manager._entries == {}
            assert harness.factory.instances == []

            harness.config.channels = [make_channel(revision=2)]
            await harness.manager.refresh()
            await online_entry(harness, revision=2)
            assert "ch1" not in harness.manager._blocked
        finally:
            await harness.manager.stop()

    asyncio.run(scenario())


def test_expired_revision_from_ownership_is_not_acquired_again():
    async def scenario() -> None:
        harness = Harness()
        try:
            other = harness.ownership.acquire(
                AcquireInput(bot_id="bot1", channel_id="ch1", revision=1)
            )
            assert other is not None
            harness.ownership.publish_status(other, "offline", "CHANNEL_EXPIRED")
            harness.ownership.release(other)

            harness.config.channels = [make_channel(revision=1)]
            await harness.manager.refresh()
            assert harness.manager._entries == {}
            assert harness.manager._expired_revisions == {"ch1": ("bot1", 1)}
            acquire_count = len(harness.ownership.acquire_calls)

            await harness.manager.refresh()
            assert len(harness.ownership.acquire_calls) == acquire_count
            assert harness.factory.instances == []

            harness.config.channels = [make_channel(revision=2)]
            await harness.manager.refresh()
            await online_entry(harness, revision=2)
        finally:
            await harness.manager.stop()

    asyncio.run(scenario())


def test_network_error_reports_and_reconnects(monkeypatch):
    monkeypatch.setattr(manager_module, "RECONNECT_BASE_SECONDS", 0.01)
    monkeypatch.setattr(manager_module, "RECONNECT_MAX_SECONDS", 0.02)

    async def scenario() -> None:
        harness = Harness()
        try:
            harness.config.channels = [make_channel()]
            await harness.manager.refresh()
            entry = await online_entry(harness)
            first_adapter = harness.factory.instances[0]

            first_adapter.callbacks.on_close()

            await wait_until(
                lambda: (
                    len(harness.factory.instances) == 2
                    and entry.online
                    and first_adapter.closed
                )
            )
            assert ("ch1", 1, "error", "NETWORK_ERROR") in harness.status.publishes
            assert harness.manager._metrics.snapshot()["reconnects"] == 1
            assert harness.ownership.renew_calls == 1
            assert harness.ownership.release_calls == []
        finally:
            await harness.manager.stop()

    asyncio.run(scenario())


def test_network_error_cancels_a_hung_connect_attempt(monkeypatch):
    monkeypatch.setattr(manager_module, "RECONNECT_BASE_SECONDS", 0.01)

    async def scenario() -> None:
        harness = Harness()
        harness.factory.connect_blocker = asyncio.Event()
        try:
            harness.config.channels = [make_channel()]
            await harness.manager.refresh()
            entry = harness.manager._entries["ch1"]
            attempt = entry.connect_task
            assert attempt is not None and not attempt.done()

            harness.factory.instances[0].callbacks.on_error("network")

            await wait_until(lambda: attempt.done())
            assert attempt.cancelled()
        finally:
            await harness.manager.stop()

    asyncio.run(scenario())


def test_auth_failure_reports_and_retries_with_same_lease(monkeypatch):
    monkeypatch.setattr(manager_module, "AUTH_RETRY_SECONDS", 0.01)

    async def scenario() -> None:
        harness = Harness()
        try:
            harness.config.channels = [make_channel()]
            await harness.manager.refresh()
            entry = await online_entry(harness)
            token = entry.lease.token
            first_adapter = harness.factory.instances[0]

            first_adapter.callbacks.on_error("auth")

            await wait_until(
                lambda: len(harness.factory.instances) == 2 and entry.online
            )
            assert ("ch1", 1, "error", "AUTH_FAILED") in harness.status.publishes
            assert entry.lease.token == token
            assert harness.ownership.renew_calls == 1
        finally:
            await harness.manager.stop()

    asyncio.run(scenario())


def test_renew_failure_removes_entry():
    async def scenario() -> None:
        harness = Harness()
        try:
            harness.config.channels = [make_channel()]
            await harness.manager.refresh()
            entry = await online_entry(harness)
            adapter = harness.factory.instances[0]

            harness.ownership.renew_result = False
            await harness.manager._renew_entry(entry)

            assert harness.manager._entries == {}
            assert adapter.closed
            assert len(harness.ownership.release_calls) == 1
            assert (
                "ch1",
                "offline",
                "LEASE_UNAVAILABLE",
            ) not in harness.ownership.publish_calls
        finally:
            await harness.manager.stop()

    asyncio.run(scenario())


def test_connection_conflict_blocks_and_stops_auto_retry():
    async def scenario() -> None:
        harness = Harness()
        try:
            harness.config.channels = [make_channel()]
            await harness.manager.refresh()
            await online_entry(harness)
            adapter = harness.factory.instances[0]

            adapter.callbacks.on_error("replaced")

            await wait_until(lambda: harness.manager._entries == {})

            assert harness.ownership.block_calls == ["ch1"]
            assert (
                "ch1",
                1,
                "error",
                "CONNECTION_CONFLICT",
            ) in harness.status.publishes
            assert harness.manager._blocked == {"ch1": 1}
            state = harness.ownership.read_status(
                AcquireInput(bot_id="bot1", channel_id="ch1", revision=1)
            )
            assert state is not None
            assert state.status == "error"
            assert state.error_code == "CONNECTION_CONFLICT"

            await harness.manager.refresh()
            assert harness.manager._entries == {}
            assert len(harness.factory.instances) == 1

            harness.config.channels = [make_channel(revision=2)]
            await harness.manager.refresh()
            await online_entry(harness, revision=2)
        finally:
            await harness.manager.stop()

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["exception", "rejected"])
def test_conflict_persistence_retries_after_a_transient_failure(failure):
    class OnceFailingOwnership(FakeOwnership):
        block_attempts = 0

        def block(self, lease: Lease) -> bool:
            self.block_attempts += 1
            if self.block_attempts == 1:
                if failure == "exception":
                    raise ConnectionError("temporary ownership outage")
                return False
            return super().block(lease)

    async def scenario() -> None:
        harness = Harness()
        ownership = OnceFailingOwnership()
        harness.manager._ownership = ownership
        harness.config.channels = [make_channel()]
        try:
            await harness.manager.refresh()
            original = await online_entry(harness)
            original.adapter.callbacks.on_error("replaced")
            await original.conflict_task
            assert ownership.block_attempts == 1

            await harness.manager.refresh()
            await original.conflict_task

            assert ownership.block_attempts == 2
            assert harness.manager._entries == {}
            assert not ownership.is_valid(original.lease)
            result = await harness.manager.ensure_connected("ch1", 1, "recover-1")
            assert result["status"] == "online"
            assert len(harness.factory.instances) == 2
        finally:
            await harness.manager.stop()

    asyncio.run(scenario())


def test_duplicate_event_is_dropped_after_completion():
    async def scenario() -> None:
        harness = Harness()
        try:
            harness.config.channels = [make_channel()]
            await harness.manager.refresh()
            await online_entry(harness)
            harness.workflow.add_done("答案")
            adapter = harness.factory.instances[0]
            frame = make_frame()

            await send_frame(harness, frame)
            await drain(harness)
            await send_frame(harness, frame)
            await drain(harness)

            assert len(harness.workflow.runs) == 1
            assert len(adapter.replies) == 1
            assert harness.manager._metrics.snapshot()["messages_duplicate"] == 1
        finally:
            await harness.manager.stop()

    asyncio.run(scenario())


def test_in_flight_duplicate_is_replayed_as_pending():
    async def scenario() -> None:
        harness = Harness()
        try:
            harness.config.channels = [make_channel()]
            await harness.manager.refresh()
            await online_entry(harness)
            gate = asyncio.Event()

            async def wait_gate() -> None:
                await gate.wait()

            harness.workflow.add(
                wait_gate, {"type": "done", "content": "一", "run_id": "r1"}
            )
            harness.workflow.add_done("二")
            adapter = harness.factory.instances[0]

            await send_frame(harness, make_frame(msgid="msg-1", req_id="req-1"))
            await wait_until(lambda: len(harness.workflow.runs) == 1)
            await send_frame(harness, make_frame(msgid="msg-1", req_id="req-2"))
            await asyncio.sleep(0.01)
            assert len(harness.workflow.runs) == 1

            gate.set()
            await drain(harness)

            assert len(harness.workflow.runs) == 2
            assert [reply["content"] for reply in adapter.replies] == ["一", "二"]
            assert [reply["finish"] for reply in adapter.replies] == [True, True]
        finally:
            await harness.manager.stop()

    asyncio.run(scenario())


def test_workflow_duplicate_204_sends_no_reply_on_first_attempt():
    async def scenario() -> None:
        harness = Harness()
        try:
            harness.config.channels = [make_channel()]
            await harness.manager.refresh()
            await online_entry(harness)
            harness.workflow.add({"type": "duplicate"})
            adapter = harness.factory.instances[0]

            await send_frame(harness, make_frame())
            await drain(harness)

            assert adapter.replies == []
            assert harness.manager._metrics.snapshot()["replies_ok"] == 0
        finally:
            await harness.manager.stop()

    asyncio.run(scenario())


@pytest.mark.parametrize("chattype", ["single", "group"])
def test_busy_reply_for_same_conversation(chattype):
    async def scenario() -> None:
        harness = Harness()
        try:
            harness.config.channels = [make_channel()]
            await harness.manager.refresh()
            await online_entry(harness)
            gate = asyncio.Event()

            async def wait_gate() -> None:
                await gate.wait()

            harness.workflow.add(
                wait_gate, {"type": "done", "content": "答案", "run_id": "r1"}
            )
            adapter = harness.factory.instances[0]

            await send_frame(
                harness,
                make_frame(msgid="msg-1", req_id="req-1", chattype=chattype),
            )
            await wait_until(lambda: len(harness.workflow.runs) == 1)
            await send_frame(
                harness,
                make_frame(
                    msgid="msg-2",
                    req_id="req-2",
                    userid="user-2" if chattype == "group" else "user-1",
                    chattype=chattype,
                ),
            )
            await wait_until(
                lambda: any(reply["content"] == BUSY_TEXT for reply in adapter.replies)
            )

            gate.set()
            await drain(harness)

            busy = [reply for reply in adapter.replies if reply["content"] == BUSY_TEXT]
            assert len(busy) == 1
            assert busy[0]["finish"] is True
            assert len(harness.workflow.runs) == 1
        finally:
            await harness.manager.stop()

    asyncio.run(scenario())


@pytest.mark.parametrize("chattype", ["single", "group"])
def test_same_conversation_on_different_channels_runs_concurrently(chattype):
    async def scenario() -> None:
        harness = Harness()
        gate = asyncio.Event()
        try:
            harness.config.channels = [
                make_channel(),
                make_channel(channel_id="ch2", bot_id="bot2"),
            ]
            await harness.manager.refresh()
            first = await online_entry(harness)
            second = await online_entry(harness, channel_id="ch2")

            async def wait_gate() -> None:
                await gate.wait()

            for _ in range(2):
                harness.workflow.add(
                    wait_gate, {"type": "done", "content": "答案", "run_id": "r1"}
                )
            first.adapter.callbacks.on_message(make_frame(chattype=chattype))
            await wait_until(lambda: len(harness.workflow.runs) == 1)
            second.adapter.callbacks.on_message(
                make_frame(chattype=chattype, bot_id="bot2")
            )
            await wait_until(
                lambda: len(harness.workflow.runs) == 2 or bool(second.adapter.replies)
            )

            assert len(harness.workflow.runs) == 2
            assert harness.manager.status()["active_tasks"] == 2
            assert {run["channel_id"] for run in harness.workflow.runs} == {
                "ch1",
                "ch2",
            }
            assert len({run["user_key"] for run in harness.workflow.runs}) == 2
            assert not any(
                reply["content"] == BUSY_TEXT for reply in second.adapter.replies
            )

            gate.set()
            await drain(harness)
        finally:
            gate.set()
            await harness.manager.stop()

    asyncio.run(scenario())


def test_global_concurrency_limit_replies_busy():
    async def scenario() -> None:
        harness = Harness(max_concurrent=1)
        try:
            harness.config.channels = [make_channel()]
            await harness.manager.refresh()
            await online_entry(harness)
            gate = asyncio.Event()

            async def wait_gate() -> None:
                await gate.wait()

            harness.workflow.add(
                wait_gate, {"type": "done", "content": "答案", "run_id": "r1"}
            )
            adapter = harness.factory.instances[0]

            await send_frame(
                harness,
                make_frame(msgid="msg-1", req_id="req-1", userid="user-1"),
            )
            await wait_until(lambda: len(harness.workflow.runs) == 1)
            await send_frame(
                harness,
                make_frame(msgid="msg-2", req_id="req-2", userid="user-2"),
            )
            await wait_until(
                lambda: any(reply["content"] == BUSY_TEXT for reply in adapter.replies)
            )

            gate.set()
            await drain(harness)

            assert len(harness.workflow.runs) == 1
            assert harness.manager.status()["active_tasks"] == 0
        finally:
            await harness.manager.stop()

    asyncio.run(scenario())


def test_stream_start_chunk_done_and_truncation():
    async def scenario() -> None:
        harness = Harness(stream_interval_ms=0, reply_max_bytes=12)
        try:
            harness.config.channels = [make_channel()]
            await harness.manager.refresh()
            await online_entry(harness)
            harness.workflow.add(
                {
                    "type": "start",
                    "run_id": "r1",
                    "conversation_id": "c1",
                    "release_id": "rel1",
                },
                {"type": "chunk", "content": "你好世界你好世界"},
                {"type": "done", "content": "你好世界你好世界", "run_id": "r1"},
            )
            adapter = harness.factory.instances[0]

            await send_frame(harness, make_frame())
            await drain(harness)

            assert [reply["content"] for reply in adapter.replies] == [
                "正在思考",
                "你好世界",
            ]
            assert [reply["finish"] for reply in adapter.replies] == [False, True]
            assert all(
                len(reply["content"].encode("utf-8")) <= 12 for reply in adapter.replies
            )
        finally:
            await harness.manager.stop()

    asyncio.run(scenario())


def test_keepalive_resends_current_content():
    async def scenario() -> None:
        harness = Harness(stream_keepalive_seconds=0.03, stream_interval_ms=100000)
        try:
            harness.config.channels = [make_channel()]
            await harness.manager.refresh()
            await online_entry(harness)
            gate = asyncio.Event()

            async def wait_gate() -> None:
                await gate.wait()

            harness.workflow.add(
                {
                    "type": "start",
                    "run_id": "r1",
                    "conversation_id": "c1",
                    "release_id": "rel1",
                },
                {"type": "chunk", "content": "内容"},
                wait_gate,
                {"type": "done", "content": "内容", "run_id": "r1"},
            )
            adapter = harness.factory.instances[0]

            await send_frame(harness, make_frame())
            await wait_until(
                lambda: any(
                    reply["content"] == "内容" and reply["finish"] is False
                    for reply in adapter.replies
                )
            )

            gate.set()
            await drain(harness)
            assert adapter.replies[-1] == {
                "req_id": "req-1",
                "stream_id": adapter.replies[0]["stream_id"],
                "content": "内容",
                "finish": True,
            }
        finally:
            await harness.manager.stop()

    asyncio.run(scenario())


def test_first_event_timeout_sends_default_response():
    async def scenario() -> None:
        harness = Harness(first_event_timeout_seconds=0.05, workflow_timeout_seconds=1)
        try:
            harness.config.channels = [make_channel()]
            await harness.manager.refresh()
            await online_entry(harness)
            harness.workflow.add("WAIT")
            adapter = harness.factory.instances[0]

            await send_frame(harness, make_frame())
            await drain(harness)

            assert len(adapter.replies) == 1
            assert adapter.replies[0]["content"] == DEFAULT_RESPONSE
            assert adapter.replies[0]["finish"] is True
            assert harness.manager._metrics.snapshot()["workflow_errors"] == 1
            await wait_until(lambda: harness.workflow.closed == 1)
        finally:
            await harness.manager.stop()

    asyncio.run(scenario())


def test_channel_removal_cancels_in_flight_run_promptly():
    async def scenario() -> None:
        harness = Harness(first_event_timeout_seconds=5, workflow_timeout_seconds=10)
        try:
            harness.config.channels = [make_channel()]
            await harness.manager.refresh()
            await online_entry(harness)
            harness.workflow.add("WAIT")
            adapter = harness.factory.instances[0]

            await send_frame(harness, make_frame())
            await wait_until(lambda: len(harness.workflow.runs) == 1)

            harness.config.channels = []
            started = monotonic()
            await harness.manager.refresh()
            await drain(harness)
            elapsed = monotonic() - started

            assert elapsed < 1.0, f"in-flight run was not cancelled ({elapsed:.2f}s)"
            assert adapter.replies == []
            assert harness.workflow.closed == 1
        finally:
            await harness.manager.stop()

    asyncio.run(scenario())


def test_stream_interrupted_retries_with_same_idempotency_key(monkeypatch):
    monkeypatch.setattr(manager_module, "MESSAGE_RETRY_DELAY_SECONDS", 0)

    async def scenario() -> None:
        harness = Harness(stream_interval_ms=0)
        try:
            harness.config.channels = [make_channel()]
            await harness.manager.refresh()
            await online_entry(harness)
            harness.workflow.add(
                {
                    "type": "start",
                    "run_id": "r1",
                    "conversation_id": "c1",
                    "release_id": "rel1",
                },
                {"type": "chunk", "content": "部分"},
                "RETURN",
            )
            harness.workflow.add(
                {
                    "type": "start",
                    "run_id": "r2",
                    "conversation_id": "c1",
                    "release_id": "rel1",
                },
                {"type": "done", "content": "完整", "run_id": "r2"},
            )
            adapter = harness.factory.instances[0]

            await send_frame(harness, make_frame())
            await drain(harness)

            assert len(harness.workflow.runs) == 2
            assert (
                harness.workflow.runs[0]["idempotency_key"]
                == harness.workflow.runs[1]["idempotency_key"]
            )
            assert len({reply["stream_id"] for reply in adapter.replies}) == 1
            assert adapter.replies[-1]["content"] == "完整"
            assert adapter.replies[-1]["finish"] is True
        finally:
            await harness.manager.stop()

    asyncio.run(scenario())


def test_duplicate_on_retry_sends_fallback_terminal(monkeypatch):
    monkeypatch.setattr(manager_module, "MESSAGE_RETRY_DELAY_SECONDS", 0)

    async def scenario() -> None:
        harness = Harness()
        try:
            harness.config.channels = [make_channel()]
            await harness.manager.refresh()
            await online_entry(harness)
            harness.workflow.add(
                {
                    "type": "start",
                    "run_id": "r1",
                    "conversation_id": "c1",
                    "release_id": "rel1",
                },
                "RETURN",
            )
            harness.workflow.add({"type": "duplicate"})
            adapter = harness.factory.instances[0]

            await send_frame(harness, make_frame())
            await drain(harness)

            assert len(harness.workflow.runs) == 2
            assert [reply["content"] for reply in adapter.replies] == [
                THINKING_TEXT,
                DEFAULT_RESPONSE,
            ]
            assert adapter.replies[-1]["finish"] is True
        finally:
            await harness.manager.stop()

    asyncio.run(scenario())


def test_repeated_409_ends_with_busy_reply():
    async def scenario() -> None:
        harness = Harness()
        try:
            harness.config.channels = [make_channel()]
            await harness.manager.refresh()
            await online_entry(harness)
            harness.workflow.add(GatewayError("CHANNEL_BUSY", status_code=409))
            harness.workflow.add(GatewayError("CHANNEL_BUSY", status_code=409))
            adapter = harness.factory.instances[0]

            await send_frame(harness, make_frame())
            await drain(harness)

            assert len(harness.workflow.runs) == 2
            assert adapter.replies[-1]["content"] == BUSY_TEXT
            assert adapter.replies[-1]["finish"] is True
        finally:
            await harness.manager.stop()

    asyncio.run(scenario())


def test_404_removes_channel_without_reply():
    async def scenario() -> None:
        harness = Harness()
        try:
            harness.config.channels = [make_channel()]
            await harness.manager.refresh()
            await online_entry(harness)
            adapter = harness.factory.instances[0]
            harness.workflow.add(GatewayError("CHANNEL_NOT_FOUND", status_code=404))
            harness.config.channels = []

            await send_frame(harness, make_frame())
            await drain(harness)
            await wait_until(lambda: harness.manager._entries == {})

            assert adapter.closed
            assert adapter.replies == []
        finally:
            await harness.manager.stop()

    asyncio.run(scenario())


def test_403_requests_refresh_and_sends_default_response():
    async def scenario() -> None:
        harness = Harness()
        try:
            harness.config.channels = [make_channel()]
            await harness.manager.refresh()
            await online_entry(harness)
            harness.workflow.add(GatewayError("CHANNEL_UNAVAILABLE", status_code=403))
            adapter = harness.factory.instances[0]

            await send_frame(harness, make_frame())
            await drain(harness)

            assert adapter.replies[-1]["content"] == DEFAULT_RESPONSE
            assert adapter.replies[-1]["finish"] is True
            assert harness.manager._entries["ch1"].online
        finally:
            await harness.manager.stop()

    asyncio.run(scenario())


def test_workflow_timeout_appends_incomplete_hint():
    async def scenario() -> None:
        harness = Harness(
            first_event_timeout_seconds=0.05,
            workflow_timeout_seconds=0.1,
            stream_interval_ms=0,
        )
        try:
            harness.config.channels = [make_channel()]
            await harness.manager.refresh()
            await online_entry(harness)
            harness.workflow.add(
                {
                    "type": "start",
                    "run_id": "r1",
                    "conversation_id": "c1",
                    "release_id": "rel1",
                },
                {"type": "chunk", "content": "部分"},
                "WAIT",
            )
            adapter = harness.factory.instances[0]

            await send_frame(harness, make_frame())
            await drain(harness)

            assert adapter.replies[-1]["content"] == "部分\n\n" + INCOMPLETE_HINT
            assert adapter.replies[-1]["finish"] is True
        finally:
            await harness.manager.stop()

    asyncio.run(scenario())


def test_answer_too_large_sends_default_response():
    async def scenario() -> None:
        harness = Harness(max_answer_bytes=4, reply_max_bytes=100)
        try:
            harness.config.channels = [make_channel()]
            await harness.manager.refresh()
            await online_entry(harness)
            harness.workflow.add(
                {
                    "type": "start",
                    "run_id": "r1",
                    "conversation_id": "c1",
                    "release_id": "rel1",
                },
                {"type": "chunk", "content": "你好世界"},
            )
            adapter = harness.factory.instances[0]

            await send_frame(harness, make_frame())
            await drain(harness)

            assert adapter.replies[-1]["content"] == DEFAULT_RESPONSE
            assert adapter.replies[-1]["finish"] is True
            assert harness.manager._metrics.snapshot()["workflow_errors"] == 1
        finally:
            await harness.manager.stop()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("status", "error_code"),
    [("online", None), ("error", "AUTH_FAILED"), ("error", "NETWORK_ERROR")],
)
def test_ensure_connected_preserves_shared_owner(status, error_code):
    async def scenario() -> None:
        harness = Harness()
        try:
            owner = harness.ownership.acquire(
                AcquireInput(bot_id="bot1", channel_id="ch1", revision=1)
            )
            assert owner is not None
            harness.ownership.publish_status(owner, status, error_code)
            harness.config.channels = [make_channel()]

            result = await harness.manager.ensure_connected("ch1", 1, "req-1")

            assert result == {
                "channel_id": "ch1",
                "revision": 1,
                "status": status,
                "error_code": error_code,
            }
            assert harness.manager._entries == {}
            assert harness.ownership.renew(owner)
            assert harness.factory.instances == []
        finally:
            await harness.manager.stop()

    asyncio.run(scenario())


def test_ensure_connected_recovers_connection_conflict_at_same_revision():
    async def scenario() -> None:
        harness = Harness()
        try:
            harness.config.channels = [make_channel()]
            await harness.manager.refresh()
            original = await online_entry(harness)
            original.adapter.callbacks.on_error("replaced")
            await wait_until(lambda: harness.manager._entries == {})
            state = harness.ownership.read_status(
                AcquireInput(bot_id="bot1", channel_id="ch1", revision=1)
            )
            assert state is not None
            assert state.error_code == "CONNECTION_CONFLICT"

            result = await harness.manager.ensure_connected("ch1", 1, "recover-1")

            assert result == {
                "channel_id": "ch1",
                "revision": 1,
                "status": "online",
                "error_code": None,
            }
            recovered = await online_entry(harness)
            assert recovered.lease.generation > original.lease.generation
            assert not harness.ownership.is_valid(original.lease)
            assert "ch1" not in harness.manager._blocked
        finally:
            await harness.manager.stop()

    asyncio.run(scenario())


def test_ensure_connected_waits_for_snapshot_and_connects():
    async def scenario() -> None:
        harness = Harness()
        try:
            harness.config.channels = [make_channel()]

            result = await harness.manager.ensure_connected("ch1", 1, "req-1")

            assert result["status"] == "online"
            await online_entry(harness)
        finally:
            await harness.manager.stop()

    asyncio.run(scenario())


def test_ensure_connected_rejects_missing_stale_expired_and_stopped():
    async def scenario() -> None:
        harness = Harness()
        with pytest.raises(GatewayError) as missing:
            await harness.manager.ensure_connected("ch1", 1, "req-1")
        assert missing.value.code == "CHANNEL_NOT_FOUND"
        assert missing.value.status_code == 404

        harness.config.channels = [
            make_channel(expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
        ]
        await harness.manager.refresh()

        with pytest.raises(GatewayError) as stale:
            await harness.manager.ensure_connected("ch1", 2, "req-1")
        assert stale.value.code == "CHANNEL_REVISION_MISMATCH"
        assert stale.value.status_code == 409

        with pytest.raises(GatewayError) as expired:
            await harness.manager.ensure_connected("ch1", 1, "req-1")
        assert expired.value.code == "CHANNEL_EXPIRED"
        assert expired.value.status_code == 409

        await harness.manager.stop()

        with pytest.raises(GatewayError) as stopped:
            await harness.manager.ensure_connected("ch1", 1, "req-1")
        assert stopped.value.code == "GATEWAY_STOPPED"
        assert stopped.value.status_code == 503

    asyncio.run(scenario())
