"""Adapter around the official WeCom bot SDK.

The manager owns reconnection, so the SDK reconnect loop is disabled and every
connection attempt gets a fresh client instance.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any, Callable, Protocol

from app.channel_gateway.protocol import GatewayError

_PROVIDER_ERRCODES = {
    846604: "WECOM_CALLBACK_EXPIRED",
    846605: "WECOM_INVALID_CALLBACK",
    846606: "WECOM_DUPLICATE_REPLY",
    846607: "WECOM_RATE_LIMITED",
    846608: "WECOM_STREAM_EXPIRED",
    846609: "WECOM_NOT_SUBSCRIBED",
}
_ACK_ERROR = re.compile(r"^Reply ack error: errcode=(-?\d+)(?:,|$)")
LIVENESS_INTERVAL_SECONDS = 1.0


class AdapterCallbacks(Protocol):
    def on_ready(self) -> None: ...

    def on_close(self) -> None: ...

    def on_error(self, kind: str) -> None: ...

    def on_message(self, frame: dict[str, Any]) -> None: ...


class _SilentLogger:
    """The SDK's default logger prints raw frames and credentials."""

    def debug(self, *args: object, **kwargs: object) -> None:
        pass

    def info(self, *args: object, **kwargs: object) -> None:
        pass

    def warn(self, *args: object, **kwargs: object) -> None:
        pass

    def warning(self, *args: object, **kwargs: object) -> None:
        pass

    def error(self, *args: object, **kwargs: object) -> None:
        pass


def _default_sdk_factory(**options: Any) -> Any:
    from aibot import WSClient, WSClientOptions

    return WSClient(WSClientOptions(**options))


def _ack_errcode(error: BaseException) -> int | None:
    match = _ACK_ERROR.match(str(error))
    if match is None:
        return None
    return int(match.group(1))


class WecomAdapter:
    def __init__(
        self,
        *,
        bot_id: str,
        secret: str,
        callbacks: AdapterCallbacks,
        sdk_factory: Callable[..., Any] | None = None,
    ) -> None:
        self._bot_id = bot_id
        self._secret = secret
        self._callbacks = callbacks
        self._sdk_factory = sdk_factory or _default_sdk_factory
        self._client: Any | None = None
        self._closed = True
        self._generation = 0
        self._authenticated = False
        self._close_reported = False
        self._monitor_tasks: set[asyncio.Task[None]] = set()

    def _guard(self, client: Any, callback: Callable[..., None], *bound: Any):
        generation = self._generation

        def handler(*args: Any) -> None:
            if (
                self._closed
                or self._close_reported
                or self._client is not client
                or self._generation != generation
            ):
                return
            try:
                callback(*bound, *args)
            except Exception:
                # A manager callback error must not tear down the SDK receive loop.
                pass

        return handler

    def _bind(self, client: Any) -> None:
        client.on("connected", self._guard(client, lambda: None))
        client.on(
            "authenticated",
            self._guard(client, lambda *_: self._on_authenticated(client)),
        )
        client.on("message", self._guard(client, self._callbacks.on_message))
        client.on("disconnected", self._guard(client, lambda *_: self._notify_close()))
        client.on(
            "event.disconnected_event",
            self._guard(client, lambda *_: self._callbacks.on_error("replaced")),
        )
        client.on("error", self._guard(client, self._handle_error))

    def _on_authenticated(self, client: Any) -> None:
        if self._authenticated:
            return
        self._authenticated = True
        task = asyncio.create_task(self._monitor_connection(client, self._generation))
        self._monitor_tasks.add(task)
        task.add_done_callback(self._monitor_tasks.discard)
        self._callbacks.on_ready()

    async def _monitor_connection(self, client: Any, generation: int) -> None:
        # The SDK silently exits its receive loop on a normal WebSocket close.
        # Poll its public state only after auth; connect() itself returns earlier.
        notify_close = self._guard(client, self._notify_close)
        while True:
            await asyncio.sleep(LIVENESS_INTERVAL_SECONDS)
            if (
                self._closed
                or self._generation != generation
                or self._client is not client
            ):
                return
            if not client.is_connected:
                notify_close()
                return

    def _notify_close(self) -> None:
        if self._close_reported:
            return
        self._close_reported = True
        for task in self._monitor_tasks:
            task.cancel()
        self._callbacks.on_close()

    def _handle_error(self, error: BaseException) -> None:
        kind = "auth" if str(error).startswith("Authentication failed") else "network"
        self._callbacks.on_error(kind)

    async def connect(self) -> None:
        await self.disconnect()
        self._generation += 1
        self._closed = False
        self._authenticated = False
        self._close_reported = False
        client = self._sdk_factory(
            bot_id=self._bot_id,
            secret=self._secret,
            max_reconnect_attempts=0,
            logger=_SilentLogger(),
        )
        self._client = client
        self._bind(client)
        try:
            await client.connect()
        except Exception as error:
            # Never surface SDK exception text: connection errors can include URLs.
            raise GatewayError("WECOM_CONNECT_FAILED") from error

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._generation += 1
        for task in self._monitor_tasks:
            task.cancel()
        client = self._client
        self._client = None
        if client is not None:
            try:
                client.disconnect()
            except Exception:
                pass

    async def disconnect(self) -> None:
        self.close()
        tasks = [
            task for task in self._monitor_tasks if task is not asyncio.current_task()
        ]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def reply(
        self, *, req_id: str, stream_id: str, content: str, finish: bool
    ) -> None:
        client = self._client
        if self._closed or client is None:
            raise GatewayError("WECOM_NOT_CONNECTED")
        try:
            await client.reply_stream(
                {"headers": {"req_id": req_id}}, stream_id, content, finish
            )
        except Exception as error:
            errcode = _ack_errcode(error)
            # Zero is an ACK success; anything else parsed from the ack is a
            # provider rejection. Timeouts and malformed acks are unknown.
            if errcode is not None and errcode != 0:
                raise GatewayError(
                    _PROVIDER_ERRCODES.get(errcode, "WECOM_REPLY_REJECTED"),
                    reply_rejected=True,
                    provider_errcode=errcode,
                ) from None
            raise GatewayError("WECOM_ACK_UNKNOWN") from None
