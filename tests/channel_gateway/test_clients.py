import asyncio
import json
from typing import Any, AsyncIterator

import httpx
import pytest

from app.channel_gateway.clients import ConfigClient, StatusClient, WorkflowClient
from app.channel_gateway.protocol import (
    MAX_RUN_REQUEST_BYTES,
    GatewayError,
)

BASE_URL = "https://platform.test"
TOKEN = "t" * 32
CHANNEL_ID = "channel-1"
USER_KEY = "wc_" + "a" * 32
IDEMPOTENCY_KEY = "wcm_event-1"


def event_dict(start: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    start = start or {
        "type": "start",
        "run_id": "run-1",
        "conversation_id": "conversation-1",
        "release_id": "release-1",
    }
    return [
        start,
        {"type": "chunk", "content": "你"},
        {"type": "chunk", "content": "好"},
        {"type": "done", "content": "你好", "run_id": "run-1"},
    ]


def ndjson(events: list[dict[str, Any]], *, trailing_newline: bool = True) -> bytes:
    body = b"".join(
        json.dumps(event, ensure_ascii=False).encode("utf-8") + b"\n"
        for event in events
    )
    return body if trailing_newline else body.rstrip(b"\n")


class ChunkedStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            yield chunk


def run_workflow(
    handler: Any,
    *,
    options: dict[str, Any] | None = None,
    signal_set: bool = False,
) -> list[dict[str, Any]]:
    async def scenario() -> list[dict[str, Any]]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            workflow = WorkflowClient(
                http,
                base_url=BASE_URL,
                token=TOKEN,
                **(options or {}),
            )
            signal = asyncio.Event()
            if signal_set:
                signal.set()
            return [
                event
                async for event in workflow.run(
                    channel_id=CHANNEL_ID,
                    revision=3,
                    user_key=USER_KEY,
                    text="你好",
                    idempotency_key=IDEMPOTENCY_KEY,
                    signal=signal,
                )
            ]

    return asyncio.run(scenario())


def test_run_sends_the_contract_and_streams_events():
    seen: dict[str, Any] = {}
    expected = event_dict()

    def handler(request: httpx.Request) -> httpx.Response:
        seen["request"] = request
        return httpx.Response(
            200,
            headers={"content-type": "application/x-ndjson"},
            content=ndjson(expected),
        )

    events = run_workflow(handler)

    assert events == expected
    request = seen["request"]
    assert request.method == "POST"
    assert (
        str(request.url) == f"{BASE_URL}/internal/v1/wecom/channels/{CHANNEL_ID}/runs"
    )
    assert request.headers["authorization"] == f"Bearer {TOKEN}"
    assert request.headers["accept"] == "application/x-ndjson"
    assert request.headers["content-type"] == "application/json"
    assert request.headers["idempotency-key"] == IDEMPOTENCY_KEY
    assert json.loads(request.content) == {
        "revision": 3,
        "message": {"user_key": USER_KEY, "text": "你好"},
    }


def test_run_returns_a_duplicate_marker_for_204():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(204)

    assert run_workflow(handler) == [{"type": "duplicate"}]


def test_run_handles_split_utf8_blank_lines_and_a_trailing_line():
    expected = event_dict()
    payload = ndjson(expected, trailing_newline=False).replace(b"\n", b"\n\n")
    split = payload.index("你".encode("utf-8")) + 1

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/x-ndjson"},
            stream=ChunkedStream([payload[:split], payload[split:]]),
        )

    events = run_workflow(handler)
    assert events == expected


def test_run_yields_an_error_event_and_stops():
    start = {
        "type": "start",
        "run_id": "run-1",
        "conversation_id": "conversation-1",
        "release_id": "release-1",
    }
    failure = {"type": "error", "code": 502, "message": "模型调用失败"}

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/x-ndjson"},
            content=ndjson([start, failure, {"type": "chunk", "content": "ignored"}]),
        )

    events = run_workflow(handler)
    assert events == [start, failure]


def test_run_rejects_a_line_over_the_configured_limit():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/x-ndjson"},
            content=ndjson(event_dict()),
        )

    with pytest.raises(GatewayError) as error:
        run_workflow(handler, options={"max_event_bytes": 32})
    assert error.value.code == "STREAM_LINE_TOO_LARGE"


def test_run_rejects_a_non_ndjson_content_type():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=ndjson(event_dict()),
        )

    with pytest.raises(GatewayError) as error:
        run_workflow(handler)
    assert error.value.code == "INVALID_STREAM_CONTENT_TYPE"


@pytest.mark.parametrize(
    ("status_code", "code"),
    [
        (401, "PLATFORM_UNAUTHORIZED"),
        (403, "CHANNEL_UNAVAILABLE"),
        (404, "CHANNEL_NOT_FOUND"),
        (409, "CHANNEL_CONFLICT"),
        (429, "CHANNEL_BUSY"),
        (504, "WORKFLOW_TIMEOUT"),
        (500, "HTTP_500"),
    ],
)
def test_run_maps_non_2xx_responses(status_code: int, code: str):
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code,
            headers={"content-type": "application/json"},
            json={"code": status_code, "message": "失败", "data": None},
        )

    with pytest.raises(GatewayError) as error:
        run_workflow(handler)
    assert error.value.code == code
    assert error.value.status_code == status_code


def test_run_rejects_an_oversized_request_body_before_sending():
    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("request must not be sent")

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            workflow = WorkflowClient(http, base_url=BASE_URL, token=TOKEN)
            async for _ in workflow.run(
                channel_id=CHANNEL_ID,
                revision=3,
                user_key=USER_KEY,
                text="a" * (MAX_RUN_REQUEST_BYTES + 1),
                idempotency_key=IDEMPOTENCY_KEY,
                signal=asyncio.Event(),
            ):
                pass

    with pytest.raises(GatewayError) as error:
        asyncio.run(scenario())
    assert error.value.code == "WORKFLOW_REQUEST_TOO_LARGE"


def test_run_reports_a_stream_that_ends_without_a_terminal_event():
    start = {
        "type": "start",
        "run_id": "run-1",
        "conversation_id": "conversation-1",
        "release_id": "release-1",
    }

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/x-ndjson"},
            content=ndjson([start, {"type": "chunk", "content": "partial"}]),
        )

    with pytest.raises(GatewayError) as error:
        run_workflow(handler)
    assert error.value.code == "STREAM_INTERRUPTED"
    assert error.value.retryable is True


@pytest.mark.parametrize(
    "events",
    [
        [
            {"type": "chunk", "content": "early"},
            {
                "type": "start",
                "run_id": "run-1",
                "conversation_id": "conversation-1",
                "release_id": "release-1",
            },
        ],
        [
            {
                "type": "start",
                "run_id": "run-1",
                "conversation_id": "conversation-1",
                "release_id": "release-1",
            },
            {
                "type": "start",
                "run_id": "run-2",
                "conversation_id": "conversation-1",
                "release_id": "release-1",
            },
        ],
    ],
)
def test_run_enforces_stream_order(events: list[dict[str, Any]]):
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/x-ndjson"},
            content=ndjson(events),
        )

    with pytest.raises(GatewayError) as error:
        run_workflow(handler)
    assert error.value.code == "INVALID_STREAM_ORDER"


def test_run_stops_immediately_when_the_signal_is_set():
    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("request must not be sent")

    with pytest.raises(GatewayError) as error:
        run_workflow(handler, signal_set=True)
    assert error.value.code == "WORKFLOW_CANCELLED"


def snapshot_payload() -> dict[str, Any]:
    return {
        "channels": [
            {
                "channel_id": CHANNEL_ID,
                "application_id": "app-1",
                "revision": 3,
                "enabled": True,
                "connection": {"bot_id": "bot-1", "secret": "secret"},
                "runtime": {
                    "default_response": "默认回复",
                    "expires_at": "2026-11-01T00:00:00+00:00",
                },
            }
        ]
    }


def test_config_client_loads_a_strict_snapshot():
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["request"] = request
        return httpx.Response(200, json=snapshot_payload())

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            return await ConfigClient(http, base_url=BASE_URL, token=TOKEN).load()

    channels = asyncio.run(scenario())
    assert len(channels) == 1
    assert channels[0].channel_id == CHANNEL_ID
    assert channels[0].connection.bot_id == "bot-1"
    assert channels[0].runtime.default_response == "默认回复"
    assert channels[0].runtime.expires_at is not None
    assert channels[0].runtime.expires_at.tzinfo is not None
    request = seen["request"]
    assert str(request.url) == f"{BASE_URL}/internal/v1/wecom/snapshot"
    assert request.headers["authorization"] == f"Bearer {TOKEN}"


@pytest.mark.parametrize(
    "payload",
    [
        b"not-json",
        json.dumps(
            {
                "channels": [
                    {
                        **snapshot_payload()["channels"][0],
                        "channel_id": "other",
                    }
                ]
                * 2
            }
        ).encode(),
        json.dumps(
            {
                "channels": [
                    snapshot_payload()["channels"][0],
                    {
                        **snapshot_payload()["channels"][0],
                        "channel_id": "other",
                    },
                ]
            }
        ).encode(),
    ],
)
def test_config_client_rejects_invalid_snapshots(payload: bytes):
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=payload)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            await ConfigClient(http, base_url=BASE_URL, token=TOKEN).load()

    with pytest.raises(GatewayError) as error:
        asyncio.run(scenario())
    assert error.value.code == "CONFIG_UNAVAILABLE"


def test_config_client_maps_transport_failures_and_oversized_bodies():
    def failing(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    def oversized(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 128)

    async def scenario(handler: Any, max_bytes: int):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            await ConfigClient(
                http, base_url=BASE_URL, token=TOKEN, max_bytes=max_bytes
            ).load()

    with pytest.raises(GatewayError) as error:
        asyncio.run(scenario(failing, 1024))
    assert error.value.code == "CONFIG_UNAVAILABLE"
    with pytest.raises(GatewayError) as error:
        asyncio.run(scenario(oversized, 32))
    assert error.value.code == "CONFIG_UNAVAILABLE"


def test_status_client_publishes_and_maps_failures():
    seen: dict[str, Any] = {}

    def accepted(request: httpx.Request) -> httpx.Response:
        seen["request"] = request
        return httpx.Response(204)

    def rejected(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    def failing(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    async def publish(handler: Any):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            await StatusClient(http, base_url=BASE_URL, token=TOKEN).publish(
                CHANNEL_ID, 3, "online", None
            )

    asyncio.run(publish(accepted))
    request = seen["request"]
    assert (
        str(request.url) == f"{BASE_URL}/internal/v1/wecom/channels/{CHANNEL_ID}/status"
    )
    assert request.headers["authorization"] == f"Bearer {TOKEN}"
    assert json.loads(request.content) == {
        "revision": 3,
        "status": "online",
        "error_code": None,
    }

    with pytest.raises(GatewayError) as error:
        asyncio.run(publish(rejected))
    assert error.value.code == "STATUS_UNAVAILABLE"
    assert error.value.status_code == 500
    with pytest.raises(GatewayError) as error:
        asyncio.run(publish(failing))
    assert error.value.code == "STATUS_UNAVAILABLE"
