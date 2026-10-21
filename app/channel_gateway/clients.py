"""HTTP clients for the platform snapshot, status and NDJSON run contracts."""

import codecs
import json
from asyncio import Event
from typing import Any, AsyncIterator
from urllib.parse import quote

import httpx

from app.channel_gateway.protocol import (
    MAX_ANSWER_BYTES,
    MAX_RUN_REQUEST_BYTES,
    MAX_SNAPSHOT_BYTES,
    MAX_WORKFLOW_EVENT_BYTES,
    ChannelConfig,
    GatewayError,
    Snapshot,
    parse_run_event,
)

_AUTH_ERROR_CODES = {
    401: "PLATFORM_UNAUTHORIZED",
    403: "CHANNEL_UNAVAILABLE",
    404: "CHANNEL_NOT_FOUND",
    409: "CHANNEL_CONFLICT",
    429: "CHANNEL_BUSY",
    504: "WORKFLOW_TIMEOUT",
}


def _url(base_url: str, path: str) -> str:
    return base_url.rstrip("/") + path


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _http_error(status_code: int) -> GatewayError:
    return GatewayError(
        _AUTH_ERROR_CODES.get(status_code, f"HTTP_{status_code}"),
        f"平台返回 HTTP {status_code}",
        status_code=status_code,
    )


async def _read_bounded(response: httpx.Response, limit: int) -> bytes:
    chunks: list[bytes] = []
    size = 0
    async for chunk in response.aiter_bytes():
        size += len(chunk)
        if size > limit:
            raise GatewayError("RESPONSE_TOO_LARGE", status_code=502)
        chunks.append(chunk)
    return b"".join(chunks)


class ConfigClient:
    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        base_url: str,
        token: str,
        max_bytes: int = MAX_SNAPSHOT_BYTES,
    ) -> None:
        self._client = client
        self._base_url = base_url
        self._token = token
        self._max_bytes = max_bytes

    async def load(self, signal: Event | None = None) -> list[ChannelConfig]:
        if signal is not None and signal.is_set():
            raise GatewayError("CONFIG_UNAVAILABLE", status_code=502)
        headers = {**_headers(self._token), "Accept": "application/json"}
        try:
            async with self._client.stream(
                "GET",
                _url(self._base_url, "/internal/v1/wecom/snapshot"),
                headers=headers,
            ) as response:
                if response.status_code != 200:
                    raise GatewayError("CONFIG_UNAVAILABLE", status_code=502)
                body = await _read_bounded(response, self._max_bytes)
        except GatewayError as error:
            raise GatewayError("CONFIG_UNAVAILABLE", status_code=502) from error
        except httpx.HTTPError as error:
            raise GatewayError("CONFIG_UNAVAILABLE", status_code=502) from error
        if signal is not None and signal.is_set():
            raise GatewayError("CONFIG_UNAVAILABLE", status_code=502)
        try:
            snapshot = Snapshot.model_validate(json.loads(body))
        except (json.JSONDecodeError, ValueError) as error:
            raise GatewayError("CONFIG_UNAVAILABLE", status_code=502) from error
        return snapshot.channels


class StatusClient:
    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        base_url: str,
        token: str,
    ) -> None:
        self._client = client
        self._base_url = base_url
        self._token = token

    async def publish(
        self,
        channel_id: str,
        revision: int,
        status: str,
        error_code: str | None,
    ) -> None:
        path = f"/internal/v1/wecom/channels/{quote(channel_id, safe='')}/status"
        body = {
            "revision": revision,
            "status": status,
            "error_code": error_code,
        }
        try:
            response = await self._client.post(
                _url(self._base_url, path),
                json=body,
                headers={**_headers(self._token), "Content-Type": "application/json"},
            )
        except httpx.HTTPError as error:
            raise GatewayError("STATUS_UNAVAILABLE", status_code=502) from error
        if not 200 <= response.status_code < 300:
            raise GatewayError("STATUS_UNAVAILABLE", status_code=response.status_code)


class WorkflowClient:
    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        base_url: str,
        token: str,
        max_answer_bytes: int = MAX_ANSWER_BYTES,
        max_event_bytes: int = MAX_WORKFLOW_EVENT_BYTES,
    ) -> None:
        self._client = client
        self._base_url = base_url
        self._token = token
        self._max_answer_bytes = max_answer_bytes
        self._max_event_bytes = max_event_bytes

    async def run(
        self,
        *,
        channel_id: str,
        revision: int,
        user_key: str,
        text: str,
        idempotency_key: str,
        signal: Event,
    ) -> AsyncIterator[dict[str, Any]]:
        body = {"revision": revision, "message": {"user_key": user_key, "text": text}}
        serialized = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        if len(serialized) > MAX_RUN_REQUEST_BYTES:
            raise GatewayError("WORKFLOW_REQUEST_TOO_LARGE", status_code=413)
        path = f"/internal/v1/wecom/channels/{quote(channel_id, safe='')}/runs"
        headers = {
            **_headers(self._token),
            "Content-Type": "application/json",
            "Accept": "application/x-ndjson",
            "Idempotency-Key": idempotency_key,
        }
        if signal.is_set():
            raise GatewayError("WORKFLOW_CANCELLED")

        response_started = False
        try:
            async with self._client.stream(
                "POST",
                _url(self._base_url, path),
                content=serialized,
                headers=headers,
            ) as response:
                response_started = True
                if response.status_code == 204:
                    yield {"type": "duplicate"}
                    return
                if not 200 <= response.status_code < 300:
                    try:
                        await self._drain_error(response)
                    except httpx.HTTPError:
                        pass
                    raise _http_error(response.status_code)
                content_type = (
                    response.headers.get("content-type", "")
                    .split(";")[0]
                    .strip()
                    .lower()
                )
                if content_type != "application/x-ndjson":
                    raise GatewayError("INVALID_STREAM_CONTENT_TYPE")
                async for event in self._events(response, signal):
                    yield event
        except GatewayError:
            raise
        except httpx.HTTPError as error:
            if response_started:
                raise GatewayError("STREAM_INTERRUPTED", retryable=True) from error
            raise GatewayError("WORKFLOW_UNAVAILABLE") from error
        except UnicodeDecodeError as error:
            raise GatewayError("INVALID_STREAM_UTF8") from error

    async def _drain_error(self, response: httpx.Response) -> None:
        size = 0
        async for chunk in response.aiter_bytes():
            size += len(chunk)
            if size > 65536:
                break

    async def _events(
        self, response: httpx.Response, signal: Event
    ) -> AsyncIterator[dict[str, Any]]:
        decoder = codecs.getincrementaldecoder("utf-8")()
        buffer = ""
        started = False
        chunk_bytes = 0

        def parse(line: str) -> dict[str, Any]:
            nonlocal started, chunk_bytes
            if len(line.encode("utf-8")) > self._max_event_bytes:
                raise GatewayError("STREAM_LINE_TOO_LARGE")
            try:
                value = json.loads(line)
            except json.JSONDecodeError as error:
                raise GatewayError("INVALID_STREAM_JSON") from error
            try:
                event = parse_run_event(value)
            except ValueError as error:
                raise GatewayError("INVALID_STREAM_EVENT") from error
            if event.type == "start":
                if started:
                    raise GatewayError("INVALID_STREAM_ORDER")
                started = True
            elif event.type == "chunk":
                if not started:
                    raise GatewayError("INVALID_STREAM_ORDER")
                chunk_bytes += len(event.content.encode("utf-8"))
                if chunk_bytes > self._max_answer_bytes:
                    raise GatewayError("CHANNEL_ANSWER_TOO_LARGE")
            elif event.type == "done":
                if len(event.content.encode("utf-8")) > self._max_answer_bytes:
                    raise GatewayError("CHANNEL_ANSWER_TOO_LARGE")
            return event.model_dump()

        async for chunk in response.aiter_bytes():
            if signal.is_set():
                raise GatewayError("WORKFLOW_CANCELLED")
            buffer += decoder.decode(chunk, final=False)
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                line = line.strip()
                if not line:
                    continue
                event = parse(line)
                yield event
                if event["type"] in ("done", "error"):
                    return
            if len(buffer.encode("utf-8")) > self._max_event_bytes:
                raise GatewayError("STREAM_LINE_TOO_LARGE")

        buffer += decoder.decode(b"", final=True)
        line = buffer.strip()
        if line:
            event = parse(line)
            yield event
            if event["type"] in ("done", "error"):
                return
        raise GatewayError("STREAM_INTERRUPTED", retryable=True)
