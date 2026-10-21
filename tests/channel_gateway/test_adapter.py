import asyncio
import json

import pytest

from app.channel_gateway.protocol import GatewayError
from app.channel_gateway.wecom_adapter import WecomAdapter


class Callbacks:
    def __init__(self) -> None:
        self.ready = 0
        self.closed = 0
        self.errors: list[str] = []
        self.messages: list[dict] = []

    def on_ready(self) -> None:
        self.ready += 1

    def on_close(self) -> None:
        self.closed += 1

    def on_error(self, kind: str) -> None:
        self.errors.append(kind)

    def on_message(self, frame: dict) -> None:
        self.messages.append(frame)


class FakeClient:
    instances: list["FakeClient"] = []

    def __init__(self, **options) -> None:
        self.options = options
        self.handlers: dict[str, object] = {}
        self.connected = 0
        self.disconnected = 0
        self.replies: list[tuple[dict, str, str, bool]] = []
        self.reply_error: BaseException | None = None
        self.is_connected = True
        FakeClient.instances.append(self)

    def on(self, event: str, handler) -> None:
        self.handlers[event] = handler

    async def connect(self) -> None:
        self.connected += 1

    def disconnect(self) -> None:
        self.disconnected += 1
        self.is_connected = False

    async def reply_stream(self, frame, stream_id, content, finish=False):
        self.replies.append((frame, stream_id, content, finish))
        if self.reply_error is not None:
            raise self.reply_error
        return {"errcode": 0}

    def emit(self, event: str, *args) -> None:
        handler = self.handlers.get(event)
        assert handler is not None, event
        handler(*args)


@pytest.fixture(autouse=True)
def reset_instances():
    FakeClient.instances.clear()


def make_adapter(callbacks: Callbacks | None = None) -> tuple[WecomAdapter, Callbacks]:
    callbacks = callbacks or Callbacks()
    adapter = WecomAdapter(
        bot_id="bot-1", secret="s" * 40, callbacks=callbacks, sdk_factory=FakeClient
    )
    return adapter, callbacks


def run(scenario) -> None:
    asyncio.run(scenario())


def test_connect_creates_sdk_client_with_silent_options():
    async def scenario():
        adapter, _ = make_adapter()
        await adapter.connect()
        assert len(FakeClient.instances) == 1
        options = FakeClient.instances[0].options
        assert options["bot_id"] == "bot-1"
        assert options["secret"] == "s" * 40
        assert options["max_reconnect_attempts"] == 0
        logger = options["logger"]
        for name in ("debug", "info", "warn", "warning", "error"):
            getattr(logger, name)("must not print")

    run(scenario)


def test_authenticated_emits_ready():
    async def scenario():
        adapter, callbacks = make_adapter()
        await adapter.connect()
        FakeClient.instances[0].emit("authenticated")
        assert callbacks.ready == 1

    run(scenario)


def test_authentication_failure_emits_auth_error():
    async def scenario():
        adapter, callbacks = make_adapter()
        await adapter.connect()
        FakeClient.instances[0].emit(
            "error", Exception("Authentication failed: invalid secret (code: 40001)")
        )
        assert callbacks.errors == ["auth"]

    run(scenario)


def test_other_sdk_errors_emit_network_error():
    async def scenario():
        adapter, callbacks = make_adapter()
        await adapter.connect()
        FakeClient.instances[0].emit("error", OSError("connection reset"))
        assert callbacks.errors == ["network"]

    run(scenario)


def test_disconnected_emits_close():
    async def scenario():
        adapter, callbacks = make_adapter()
        await adapter.connect()
        FakeClient.instances[0].emit("disconnected", "closed")
        assert callbacks.closed == 1

    run(scenario)


def test_disconnected_event_emits_replaced():
    async def scenario():
        adapter, callbacks = make_adapter()
        await adapter.connect()
        FakeClient.instances[0].emit("event.disconnected_event", {"body": {}})
        assert callbacks.errors == ["replaced"]

    run(scenario)


def test_message_event_forwards_raw_frame():
    async def scenario():
        adapter, callbacks = make_adapter()
        await adapter.connect()
        frame = {"cmd": "aibot_msg_callback", "body": {"msgtype": "text"}}
        FakeClient.instances[0].emit("message", frame)
        assert callbacks.messages == [frame]

    run(scenario)


def test_late_callbacks_after_disconnect_are_ignored():
    async def scenario():
        adapter, callbacks = make_adapter()
        await adapter.connect()
        client = FakeClient.instances[0]
        await adapter.disconnect()
        client.emit("authenticated")
        client.emit("message", {"cmd": "aibot_msg_callback"})
        client.emit("disconnected", "closed")
        client.emit("event.disconnected_event", {})
        client.emit("error", Exception("Authentication failed"))
        assert callbacks.ready == 0
        assert callbacks.closed == 0
        assert callbacks.errors == []
        assert callbacks.messages == []
        assert client.disconnected == 1

    run(scenario)


def test_connect_creates_a_new_client_and_ignores_the_old_one():
    async def scenario():
        adapter, callbacks = make_adapter()
        await adapter.connect()
        first = FakeClient.instances[0]
        await adapter.disconnect()
        await adapter.connect()
        assert len(FakeClient.instances) == 2
        second = FakeClient.instances[1]
        assert second is not first
        first.emit("authenticated")
        assert callbacks.ready == 0
        second.emit("authenticated")
        assert callbacks.ready == 1

    run(scenario)


def test_reply_forwards_req_id_stream_and_finish():
    async def scenario():
        adapter, _ = make_adapter()
        await adapter.connect()
        await adapter.reply(
            req_id="req-1", stream_id="stream-1", content="hi", finish=True
        )
        assert FakeClient.instances[0].replies == [
            ({"headers": {"req_id": "req-1"}}, "stream-1", "hi", True)
        ]

    run(scenario)


@pytest.mark.parametrize(
    ("errcode", "code"),
    [
        (846604, "WECOM_CALLBACK_EXPIRED"),
        (846605, "WECOM_INVALID_CALLBACK"),
        (846606, "WECOM_DUPLICATE_REPLY"),
        (846607, "WECOM_RATE_LIMITED"),
        (846608, "WECOM_STREAM_EXPIRED"),
        (846609, "WECOM_NOT_SUBSCRIBED"),
        (12345, "WECOM_REPLY_REJECTED"),
    ],
)
def test_reply_ack_errcodes_are_mapped(errcode: int, code: str):
    async def scenario():
        adapter, _ = make_adapter()
        await adapter.connect()
        FakeClient.instances[0].reply_error = RuntimeError(
            f"Reply ack error: errcode={errcode}, errmsg=private"
        )
        with pytest.raises(GatewayError) as error:
            await adapter.reply(req_id="r", stream_id="s", content="x", finish=False)
        assert error.value.code == code
        assert error.value.reply_rejected is True
        assert error.value.provider_errcode == errcode
        assert "private" not in str(error.value)

    run(scenario)


@pytest.mark.parametrize(
    "failure",
    [
        TimeoutError("Reply ack timeout (5s)"),
        RuntimeError("WebSocket not connected, unable to send data"),
        RuntimeError("Reply ack error: errcode=0, errmsg=ok"),
        RuntimeError("Reply ack error: errcode=oops"),
        RuntimeError("Reply queue for reqId r exceeds max size"),
    ],
)
def test_unknown_reply_outcomes_are_not_provider_rejections(
    failure: BaseException,
):
    async def scenario():
        adapter, _ = make_adapter()
        await adapter.connect()
        FakeClient.instances[0].reply_error = failure
        with pytest.raises(GatewayError) as error:
            await adapter.reply(req_id="r", stream_id="s", content="x", finish=True)
        assert error.value.code == "WECOM_ACK_UNKNOWN"
        assert error.value.reply_rejected is False
        assert error.value.provider_errcode is None

    run(scenario)


def test_reply_before_connect_is_rejected():
    async def scenario():
        adapter, _ = make_adapter()
        with pytest.raises(GatewayError) as error:
            await adapter.reply(req_id="r", stream_id="s", content="x", finish=False)
        assert error.value.code == "WECOM_NOT_CONNECTED"

    run(scenario)


class SdkTransport:
    """Only replace the external socket; keep SDK auth and receive tasks real."""

    def __init__(self) -> None:
        self.open = True
        self.frames: asyncio.Queue[str | None] = asyncio.Queue()
        self.auth_req_id = ""
        self.receive_finished = asyncio.Event()

    async def send(self, raw: str) -> None:
        frame = json.loads(raw)
        if frame["cmd"] == "aibot_subscribe":
            self.auth_req_id = frame["headers"]["req_id"]

    def authenticate(self) -> None:
        self.frames.put_nowait(
            json.dumps({"headers": {"req_id": self.auth_req_id}, "errcode": 0})
        )

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        frame = await self.frames.get()
        if frame is None:
            self.receive_finished.set()
            raise StopAsyncIteration
        return frame

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.open = False
        self.frames.put_nowait(None)


async def wait_for_callback(predicate) -> None:
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0.005)


@pytest.mark.parametrize("close_code", [1000, 1001])
def test_real_sdk_clean_close_notifies_once_and_leaves_no_tasks(
    monkeypatch, close_code
):
    async def scenario():
        import aibot.ws

        baseline = asyncio.all_tasks()
        transport = SdkTransport()

        async def connect(*args, **kwargs):
            return transport

        monkeypatch.setattr(aibot.ws.websockets, "connect", connect)
        callbacks = Callbacks()
        adapter = WecomAdapter(bot_id="bot", secret="test-secret", callbacks=callbacks)
        try:
            await adapter.connect()
            # connect() returns before authentication; this is not a failure.
            await asyncio.sleep(0.02)
            assert callbacks.closed == 0
            assert callbacks.errors == []
            assert callbacks.ready == 0
            transport.authenticate()
            await wait_for_callback(lambda: callbacks.ready == 1)
            await transport.close(code=close_code)
            await transport.receive_finished.wait()
            await wait_for_callback(lambda: callbacks.closed == 1)
            await asyncio.sleep(0.02)
            assert callbacks.closed == 1
            assert callbacks.errors == []
        finally:
            await adapter.disconnect()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
        assert asyncio.all_tasks() == baseline

    run(scenario)


def test_real_sdk_clean_close_drives_manager_reconnect(monkeypatch):
    async def scenario():
        import aibot.ws

        from app.channel_gateway import manager as manager_module
        from app.channel_gateway.manager import ChannelManager
        from tests.channel_gateway.test_manager import (
            FakeConfig,
            FakeOwnership,
            FakeStatus,
            FakeWorkflow,
            make_channel,
            make_settings,
        )

        baseline = asyncio.all_tasks()
        transports: list[SdkTransport] = []

        async def connect(*args, **kwargs):
            transport = SdkTransport()
            transports.append(transport)
            return transport

        monkeypatch.setattr(aibot.ws.websockets, "connect", connect)
        monkeypatch.setattr(manager_module, "RECONNECT_BASE_SECONDS", 0.01)
        config, status = FakeConfig(), FakeStatus()
        config.channels = [make_channel()]
        manager = ChannelManager(
            make_settings(),
            config,
            FakeWorkflow(),
            status,
            FakeOwnership(),
            WecomAdapter,
        )
        try:
            await manager.refresh()
            await wait_for_callback(lambda: len(transports) == 1)
            transports[0].authenticate()
            await wait_for_callback(lambda: manager.status()["online"] == 1)
            await transports[0].close()
            await wait_for_callback(lambda: len(transports) == 2)
            assert manager.status()["online"] == 0
            assert ("ch1", 1, "error", "NETWORK_ERROR") in status.publishes
            transports[1].authenticate()
            await wait_for_callback(lambda: manager.status()["online"] == 1)
        finally:
            await manager.stop()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
        assert asyncio.all_tasks() == baseline

    run(scenario)


def test_liveness_waits_for_authentication(monkeypatch):
    async def scenario():
        from app.channel_gateway import wecom_adapter

        monkeypatch.setattr(wecom_adapter, "LIVENESS_INTERVAL_SECONDS", 0.005)
        adapter, callbacks = make_adapter()
        try:
            await adapter.connect()
            client = FakeClient.instances[-1]
            client.is_connected = False
            await asyncio.sleep(0.03)
            assert callbacks.closed == 0
            assert callbacks.errors == []
            client.is_connected = True
            client.emit("authenticated")
            await asyncio.sleep(0.03)
            assert callbacks.ready == 1
            assert callbacks.closed == 0
        finally:
            await adapter.disconnect()

    run(scenario)


def test_disconnected_and_liveness_report_close_only_once(monkeypatch):
    async def scenario():
        from app.channel_gateway import wecom_adapter

        monkeypatch.setattr(wecom_adapter, "LIVENESS_INTERVAL_SECONDS", 0.005)
        baseline = asyncio.all_tasks()
        adapter, callbacks = make_adapter()
        try:
            await adapter.connect()
            client = FakeClient.instances[-1]
            client.emit("authenticated")
            client.is_connected = False
            client.emit("disconnected", "closed")
            client.emit("disconnected", "closed")
            await asyncio.sleep(0.03)
            assert callbacks.closed == 1
        finally:
            await adapter.disconnect()
        assert asyncio.all_tasks() == baseline

    run(scenario)


def test_close_and_reconnect_fence_old_liveness_and_join_monitors(monkeypatch):
    async def scenario():
        from app.channel_gateway import wecom_adapter

        monkeypatch.setattr(wecom_adapter, "LIVENESS_INTERVAL_SECONDS", 0.005)
        baseline = asyncio.all_tasks()
        adapter, callbacks = make_adapter()
        try:
            await adapter.connect()
            first = FakeClient.instances[-1]
            first.emit("authenticated")
            await asyncio.sleep(0)
            adapter.close()
            first.emit("disconnected", "closed")
            first.emit("authenticated")
            assert callbacks.closed == 0
            assert callbacks.ready == 1
            await adapter.connect()
            second = FakeClient.instances[-1]
            second.emit("authenticated")
            await asyncio.sleep(0.03)
            assert callbacks.ready == 2
            assert callbacks.closed == 0
            second.is_connected = False
            await wait_for_callback(lambda: callbacks.closed == 1)
            await adapter.connect()
            third = FakeClient.instances[-1]
            third.emit("authenticated")
            await asyncio.sleep(0.03)
            assert callbacks.ready == 3
            assert callbacks.closed == 1
        finally:
            await adapter.disconnect()
        assert asyncio.all_tasks() == baseline

    run(scenario)
