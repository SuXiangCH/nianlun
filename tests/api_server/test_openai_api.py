import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient

from app.api_server.apis.v1.router import v1_router
from app.api_server.common.errors import (
    ApiError,
    api_error_handler,
    http_exception_handler,
    validation_error_handler,
)
from app.api_server.middleware import RequestTrackingMiddleware
from tests.api_server.test_public_runtime import runtime as runtime
from tests.api_server.test_application_publishing import publishing as publishing


@pytest.fixture
def client(runtime, monkeypatch):
    def build_runtime(config, checkpointer, record):
        def events(messages, **kwargs):
            runtime.calls.append(messages)
            yield {"type": "message", "data": {"delta": "Hello "}}
            yield {"type": "message", "data": {"delta": "world"}}
            yield {
                "type": "done",
                "data": {
                    "answer": "Hello world",
                    "usage": {
                        "input_tokens": 10,
                        "output_tokens": 2,
                        "total_tokens": 12,
                    },
                },
            }

        return SimpleNamespace(iter_public_events=events)

    monkeypatch.setattr(
        runtime.publication.applications, "release_runtime", build_runtime
    )
    app = FastAPI()
    app.state.services = SimpleNamespace(invocations=runtime.service)
    app.add_exception_handler(ApiError, api_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(HTTPException, http_exception_handler)
    app.add_middleware(RequestTrackingMiddleware)
    app.include_router(v1_router)
    with TestClient(app) as client:
        client.headers["Authorization"] = f"Bearer {runtime.token}"
        yield client


def body(runtime, **options):
    return {
        "model": runtime.app_id,
        "messages": [{"role": "user", "content": "hello"}],
        **options,
    }


def test_completion_works_with_openai_sdk_and_persists_run(client, runtime):
    from openai import OpenAI

    sdk = OpenAI(
        api_key=runtime.token, base_url="http://testserver/v1", http_client=client
    )
    response = sdk.chat.completions.create(
        model=runtime.app_id, messages=[{"role": "user", "content": "hello"}]
    )
    assert response.object == "chat.completion"
    assert response.model == runtime.app_id
    assert response.choices[0].message.content == "Hello world"
    assert response.choices[0].finish_reason == "stop"
    assert response.usage.prompt_tokens == 10
    assert response.usage.completion_tokens == 2
    assert runtime.service.list(runtime.app_id)["items"][0]["status"] == "completed"


def test_models_are_scoped_to_authenticated_key(client, runtime):
    response = client.get("/v1/models")
    assert response.status_code == 200
    assert response.json()["object"] == "list"
    assert [item["id"] for item in response.json()["data"]] == [runtime.app_id]
    assert client.get(f"/v1/models/{runtime.app_id}").json()["object"] == "model"
    assert client.get("/v1/models/another-agent").status_code == 404
    wrong = client.post(
        "/v1/chat/completions", json=body(runtime, model="another-agent")
    )
    assert wrong.status_code == 404
    assert not runtime.calls


def test_messages_are_preserved_and_calls_do_not_share_history(client, runtime):
    messages = [
        {"role": "system", "content": "Reply briefly"},
        {"role": "user", "content": "My name is Lin"},
        {"role": "assistant", "content": "Hello Lin"},
        {"role": "user", "content": "What is my name?"},
    ]
    first = client.post("/v1/chat/completions", json=body(runtime, messages=messages))
    second = client.post("/v1/chat/completions", json=body(runtime))
    assert first.status_code == second.status_code == 200
    assert runtime.calls == [messages, body(runtime)["messages"]]
    runs = runtime.service.list(runtime.app_id)["items"]
    assert len({run["conversation_id"] for run in runs}) == 2


def test_standard_stream_and_sdk_usage(client, runtime):
    from openai import OpenAI

    sdk = OpenAI(
        api_key=runtime.token, base_url="http://testserver/v1", http_client=client
    )
    stream = sdk.chat.completions.create(
        model=runtime.app_id,
        messages=[{"role": "user", "content": "hello"}],
        stream=True,
        stream_options={"include_usage": True},
    )
    chunks = list(stream)
    assert (
        "".join(
            chunk.choices[0].delta.content or "" for chunk in chunks if chunk.choices
        )
        == "Hello world"
    )
    assert len({chunk.id for chunk in chunks}) == 1
    assert all(chunk.object == "chat.completion.chunk" for chunk in chunks)
    assert chunks[-2].choices[0].finish_reason == "stop"
    assert chunks[-1].choices == []
    assert chunks[-1].usage.total_tokens == 12


def test_stream_replay_emits_answer_once_and_done(client, runtime):
    headers = {"Idempotency-Key": "openai-retry"}
    request = body(runtime, stream=True)
    first = client.post("/v1/chat/completions", json=request, headers=headers)
    second = client.post("/v1/chat/completions", json=request, headers=headers)
    assert first.status_code == second.status_code == 200
    for response in [first, second]:
        assert "event:" not in response.text
        assert response.text.endswith("data: [DONE]\n\n")
        chunks = [
            json.loads(line[6:])
            for line in response.text.splitlines()
            if line.startswith("data: {")
        ]
        assert all("finish_reason" in item["choices"][0] for item in chunks)
        assert (
            "".join(item["choices"][0]["delta"].get("content", "") for item in chunks)
            == "Hello world"
        )
    assert len(runtime.calls) == 1
    changed = client.post(
        "/v1/chat/completions",
        json=body(
            runtime, stream=True, messages=[{"role": "user", "content": "changed"}]
        ),
        headers=headers,
    )
    assert changed.status_code == 409
    assert "error" in changed.json()


@pytest.mark.parametrize(
    "option",
    [
        {"tools": []},
        {"temperature": 0.5},
        {"n": 2},
        {"messages": []},
        {"messages": [{"role": "assistant", "content": "hello"}]},
        {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": "private-input"}}
                    ],
                }
            ]
        },
    ],
)
def test_unsupported_inputs_return_standard_errors_without_echoing_input(
    client, runtime, option
):
    response = client.post("/v1/chat/completions", json=body(runtime, **option))
    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_request_error"
    assert "private-input" not in response.text
    assert response.headers["X-Request-Id"]
    assert not runtime.calls


def test_text_parts_are_normalized(client, runtime):
    response = client.post(
        "/v1/chat/completions",
        json=body(
            runtime,
            messages=[{"role": "user", "content": [{"type": "text", "text": "hello"}]}],
        ),
    )
    assert response.status_code == 200
    assert runtime.calls[0] == [{"role": "user", "content": "hello"}]


def test_missing_or_revoked_credentials_are_rejected_before_stream(client, runtime):
    client.headers.pop("Authorization")
    response = client.post("/v1/chat/completions", json=body(runtime, stream=True))
    assert response.status_code == 401
    assert response.json()["error"]["type"] == "authentication_error"
    assert response.headers["content-type"].startswith("application/json")
    runtime.publication.revoke_key(
        runtime.app_id, runtime.principal.client_id, runtime.principal.key_id
    )
    response = client.get(
        "/v1/models", headers={"Authorization": f"Bearer {runtime.token}"}
    )
    assert response.status_code == 401
    assert not runtime.calls


def test_stream_failure_has_no_success_terminator(client, runtime, monkeypatch):
    def fail(messages, **kwargs):
        yield {"type": "message", "data": {"delta": "partial"}}
        raise RuntimeError("private-model-secret")

    monkeypatch.setattr(
        runtime.publication.applications,
        "release_runtime",
        lambda *args: SimpleNamespace(iter_public_events=fail),
    )
    response = client.post("/v1/chat/completions", json=body(runtime, stream=True))
    assert response.status_code == 200
    assert '"error"' in response.text
    assert "private-model-secret" not in response.text
    assert "[DONE]" not in response.text
    assert '"finish_reason": "stop"' not in response.text


def test_api_disable_and_quota_use_standard_errors(client, runtime):
    from dataclasses import replace

    runtime.service.settings = replace(runtime.settings, application_api_enabled=False)
    response = client.get("/v1/models")
    assert response.status_code == 404 and "error" in response.json()
    runtime.service.settings = replace(
        runtime.settings, application_api_requests_per_minute=1
    )
    assert client.post("/v1/chat/completions", json=body(runtime)).status_code == 200
    response = client.post("/v1/chat/completions", json=body(runtime, stream=True))
    assert response.status_code == 429
    assert response.headers["Retry-After"] == "1"
    assert response.json()["error"]["type"] == "rate_limit_error"


@pytest.mark.parametrize("disconnect", [False, True])
def test_stream_deltas_reach_transport_before_completion(
    client, runtime, monkeypatch, disconnect
):
    import asyncio
    import threading

    release, finished = threading.Event(), threading.Event()

    def events(messages, **kwargs):
        yield {"type": "message", "data": {"delta": "live-token"}}
        wait_for = kwargs["cancellation_event"] if disconnect else release
        if not wait_for.wait(3):
            raise RuntimeError("transport did not receive token")
        finished.set()
        yield {"type": "done", "data": {"answer": "live-token"}}

    monkeypatch.setattr(
        runtime.publication.applications,
        "release_runtime",
        lambda *args: SimpleNamespace(iter_public_events=events),
    )

    async def exercise():
        received = False
        chunks = []
        disconnected = asyncio.Event()

        async def receive():
            nonlocal received
            if not received:
                received = True
                return {
                    "type": "http.request",
                    "body": json.dumps(body(runtime, stream=True)).encode(),
                    "more_body": False,
                }
            await disconnected.wait()
            return {"type": "http.disconnect"}

        async def send(event):
            if event["type"] == "http.response.body":
                chunk = event.get("body", b"")
                chunks.append(chunk)
                if b"live-token" in chunk:
                    assert not finished.is_set()
                    if disconnect:
                        disconnected.set()
                    else:
                        release.set()

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3" if disconnect else "2.4"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/v1/chat/completions",
            "raw_path": b"/v1/chat/completions",
            "query_string": b"",
            "headers": [
                (b"content-type", b"application/json"),
                (b"authorization", f"Bearer {runtime.token}".encode()),
            ],
            "server": ("testserver", 80),
            "client": ("127.0.0.1", 1234),
            "root_path": "",
        }
        try:
            await asyncio.wait_for(client.app(scope, receive, send), 4)
        finally:
            release.set()
        if disconnect:
            assert b"[DONE]" not in b"".join(chunks)
            assert await asyncio.to_thread(finished.wait, 2)
        else:
            assert b"".join(chunks).endswith(b"data: [DONE]\n\n")
        assert finished.is_set()

    asyncio.run(exercise())


def test_stream_backpressure_is_bounded_and_cancellation_unblocks_worker(
    runtime, monkeypatch
):
    import threading
    from app.api_server.apis.v1.runtime_schemas import PublicChatRequest

    full = threading.Event()

    def events(messages, **kwargs):
        for index in range(1000):
            if index == 256:
                full.set()
            yield {"type": "message", "data": {"delta": "text"}}
        yield {"type": "done", "data": {"answer": "text" * 1000}}

    monkeypatch.setattr(
        runtime.publication.applications,
        "release_runtime",
        lambda *args: SimpleNamespace(iter_public_events=events),
    )
    prepared = runtime.service.prepare(
        runtime.principal,
        PublicChatRequest(message="hello", user_id="openai", response_mode="streaming"),
        "backpressure",
        input_messages=body(runtime)["messages"],
        stream_output=True,
    )
    try:
        assert full.wait(2)
        assert prepared.deltas.qsize() == prepared.deltas.maxsize == 256
    finally:
        runtime.service.cancel(prepared)
    with pytest.raises(ApiError):
        prepared.future.result(timeout=2)
    assert runtime.service.list(runtime.app_id)["items"][0]["status"] == "cancelled"


def test_external_history_over_budget_fails_before_model_execution(client, runtime):
    response = client.post(
        "/v1/chat/completions",
        json=body(
            runtime,
            messages=[
                {"role": "system", "content": "历史" * 16000},
                {"role": "user", "content": "历史" * 16000},
            ],
        ),
    )
    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_request_error"
    assert not runtime.calls


def test_over_capacity_history_at_runtime_returns_400(client, runtime, monkeypatch):
    from nianlun.agent.middleware import HistoryOverCapacityError

    def events(messages, **kwargs):
        # LangGraph wraps middleware failures; the cause chain carries the type.
        raise RuntimeError("graph execution failed") from HistoryOverCapacityError(
            "private-detail: complete history exceeds model context capacity"
        )
        yield  # pragma: no cover

    monkeypatch.setattr(
        runtime.publication.applications,
        "release_runtime",
        lambda *args: SimpleNamespace(iter_public_events=events),
    )
    response = client.post("/v1/chat/completions", json=body(runtime))
    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_request_error"
    assert "private-detail" not in response.text
    run = runtime.service.list(runtime.app_id)["items"][0]
    assert run["status"] == "failed"
    assert run["error_status"] == 400


def test_stream_timeout_cancels_execution_without_done(client, runtime, monkeypatch):
    from dataclasses import replace
    import threading

    exited, release = threading.Event(), threading.Event()
    runtime.service.settings = replace(
        runtime.settings, application_api_timeout_seconds=0.03
    )

    def events(messages, cancellation_event, **kwargs):
        cancellation_event.wait(2)
        release.wait(2)
        exited.set()
        yield {"type": "done", "data": {"answer": "late"}}

    monkeypatch.setattr(
        runtime.publication.applications,
        "release_runtime",
        lambda *args: SimpleNamespace(iter_public_events=events),
    )
    response = client.post("/v1/chat/completions", json=body(runtime, stream=True))
    try:
        assert response.status_code == 200
        assert '"code": "504"' in response.text
        assert "[DONE]" not in response.text
        assert (
            runtime.service.list(runtime.app_id)["items"][0]["error_type"]
            == "timed_out"
        )
    finally:
        release.set()
    assert exited.wait(2)
    assert runtime.service.list(runtime.app_id)["items"][0]["status"] == "timed_out"
