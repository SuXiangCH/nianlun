import asyncio
import json
import threading
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api_server.apis.v1.router import v1_router
from app.api_server.common.errors import ApiError, api_error_handler
from app.api_server.apis.v1.runtime import api as native_api, openai as openai_api
from tests.api_server.test_application_publishing import publishing as publishing
from tests.api_server.test_public_runtime import runtime as runtime
from tests.api_server.test_public_answer_images import image_runtime as image_runtime


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("mismatch", [False, True])
def test_image_stream_sends_safe_prefix_before_completion_and_checks_final(
    image_runtime, monkeypatch, native, mismatch
):
    context = image_runtime
    release = threading.Event()
    heartbeat_seen = threading.Event()
    clock_value = 0

    def clock():
        nonlocal clock_value
        clock_value += 16
        return clock_value

    monkeypatch.setattr(
        native_api if native else openai_api, "time", SimpleNamespace(monotonic=clock)
    )
    original = context.publication.applications.release_runtime(None, None, None)

    def events(*args, **kwargs):
        yield {"type": "message", "data": {"delta": "Before\n\n"}}
        assert release.wait(3), "safe prefix was withheld until completion"
        yield {
            "type": "message",
            "data": {"delta": "![candidate](nianlun-image:img_01)\n\nAfter"},
        }
        for _ in range(1000):
            yield {"type": "message", "data": {"delta": "buffered\n"}}
        assert heartbeat_seen.wait(3), (
            "heartbeat stopped while image suffix was buffered"
        )
        for event in original.iter_events(*args, **kwargs):
            if event["type"] == "done":
                marker = event["data"]["answer"].split("\n\n", 1)[1]
                event["data"]["answer"] = (
                    ("Different" if mismatch else "Before")
                    + "\n\n"
                    + marker
                    + "\n\nAfter"
                )
                yield event

    monkeypatch.setattr(
        context.publication.applications,
        "release_runtime",
        lambda *args, **kwargs: SimpleNamespace(
            iter_events=events, iter_public_events=events
        ),
    )
    app = FastAPI()
    app.add_exception_handler(ApiError, api_error_handler)
    app.state.services = SimpleNamespace(invocations=context.service)
    app.include_router(v1_router)
    path = (
        f"/api/v1/runtime/apps/{context.app_id}/chat"
        if native
        else "/v1/chat/completions"
    )
    body = (
        {
            "message": "show image",
            "user_id": "user",
            "include_images": True,
            "response_mode": "streaming",
        }
        if native
        else {
            "model": context.app_id,
            "messages": [{"role": "user", "content": "show image"}],
            "stream": True,
        }
    )

    async def exercise():
        received = False
        chunks = []

        async def receive():
            nonlocal received
            if not received:
                received = True
                return {
                    "type": "http.request",
                    "body": json.dumps(body).encode(),
                    "more_body": False,
                }
            await asyncio.Event().wait()

        async def send(event):
            if event["type"] == "http.response.body":
                chunk = event.get("body", b"")
                chunks.append(chunk)
                if b": heartbeat" in chunk:
                    heartbeat_seen.set()
                if b"Before" in chunk and not release.is_set():
                    assert (
                        not context.service.repository.list(context.app_id)[0]["status"]
                        == "completed"
                    )
                    release.set()

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "root_path": "",
            "headers": [
                (b"content-type", b"application/json"),
                (b"authorization", f"Bearer {context.token}".encode()),
                (b"idempotency-key", b"stream-key"),
                (b"x-nianlun-include-images", b"true"),
            ],
            "server": ("testserver", 80),
            "client": ("127.0.0.1", 1234),
        }
        try:
            await asyncio.wait_for(app(scope, receive, send), 5)
        finally:
            release.set()
            heartbeat_seen.set()
        text = b"".join(chunks).decode()
        assert b"Before" in b"".join(chunks), "safe prefix did not reach transport"
        assert "nianlun-image:" not in text
        assert "candidate" not in text
        if mismatch:
            assert "流式答案与已提交结果不一致" in text
            assert "[DONE]" not in text and "event: done" not in text
        elif native:
            assert "event: done" in text and "Source image" in text
        else:
            deltas = [
                json.loads(line[6:])
                for line in text.splitlines()
                if line.startswith("data: {")
            ]
            content = "".join(
                choice["delta"].get("content", "")
                for item in deltas
                for choice in item["choices"]
            )
            assert content.startswith("Before\n\n![Source image](https://api.test/")
            assert content.endswith("\nAfter")
            assert content.count("Before") == 1
            assert text.endswith("data: [DONE]\n\n")

    asyncio.run(exercise())


def test_openai_blocking_response_exposes_run_location(image_runtime):
    context = image_runtime
    app = FastAPI()
    app.state.services = SimpleNamespace(invocations=context.service)
    app.include_router(v1_router)
    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {context.token}"},
            json={
                "model": context.app_id,
                "messages": [{"role": "user", "content": "hello"}],
            },
        )
        assert response.status_code == 200
        assert response.headers["Location"].startswith(
            f"/api/v1/runtime/apps/{context.app_id}/runs/"
        )


@pytest.mark.parametrize("native", [True, False])
@pytest.mark.parametrize("transport", ["url", "data_url"])
@pytest.mark.parametrize("available", [True, False])
def test_image_stream_first_and_replay_emit_saved_answer_once(
    image_runtime, monkeypatch, native, transport, available
):
    context = image_runtime
    if not available:
        original = context.publication.applications.release_runtime(None, None, None)

        def events(*args, **kwargs):
            for event in original.iter_events(*args, **kwargs):
                if event["type"] == "done":
                    event["data"]["answer"] = event["data"]["answer"].split("\n\n", 1)[
                        1
                    ]
                    event["data"]["answer_output_mode"] = "images"
                yield event

        # Preserve trusted metadata, but make the original unavailable before archive.
        from pathlib import Path

        Path(
            context.service.image_service.knowledge_base_lookup("kb")["workspace_dir"],
            "image.png",
        ).unlink()
        monkeypatch.setattr(
            context.publication.applications,
            "release_runtime",
            lambda *args, **kwargs: SimpleNamespace(
                iter_events=events, iter_public_events=events
            ),
        )
    app = FastAPI()
    app.add_exception_handler(ApiError, api_error_handler)
    app.state.services = SimpleNamespace(invocations=context.service)
    app.include_router(v1_router)
    headers = {
        "Authorization": f"Bearer {context.token}",
        "Idempotency-Key": "image-stream-replay",
        "X-Nianlun-Include-Images": "true",
        "X-Nianlun-Image-Transport": transport,
    }
    path = (
        f"/api/v1/runtime/apps/{context.app_id}/chat"
        if native
        else "/v1/chat/completions"
    )
    body = (
        {
            "message": "show image",
            "user_id": "user",
            "response_mode": "streaming",
            "include_images": True,
            "image_transport": transport,
        }
        if native
        else {
            "model": context.app_id,
            "user": "user",
            "stream": True,
            "messages": [{"role": "user", "content": "show image"}],
        }
    )
    contents = []
    locations = []
    with TestClient(app) as client:
        for _ in range(2):
            response = client.post(path, headers=headers, json=body)
            assert response.status_code == 200
            assert response.headers["cache-control"] == "private, no-store"
            locations.append(response.headers["Location"])
            if native:
                events = []
                for block in response.text.strip().split("\n\n"):
                    lines = block.splitlines()
                    if lines[0].startswith("event: "):
                        events.append((lines[0][7:], json.loads(lines[1][6:])))
                content = "".join(
                    data["delta"] for name, data in events if name == "message"
                )
                saved = next(data["answer"] for name, data in events if name == "done")
                assert content == saved
                assert events[-1][0] == "done"
            else:
                chunks = [
                    json.loads(line[6:])
                    for line in response.text.splitlines()
                    if line.startswith("data: {")
                ]
                content = "".join(
                    choice["delta"].get("content", "")
                    for item in chunks
                    for choice in item["choices"]
                )
                assert response.text.endswith("data: [DONE]\n\n")
            assert content
            assert "nianlun-image:" not in content
            if not available:
                assert "![" not in content
                assert "base64" not in content
            contents.append(content)
    assert contents[0] == contents[1]
    assert locations[0] == locations[1]
    assert len(context.calls) == 1


@pytest.mark.parametrize("agent_version", ["v0", "v1"])
@pytest.mark.parametrize("exact_budget", [False, True])
def test_native_image_stream_uses_final_text_from_real_runner(
    image_runtime, monkeypatch, agent_version, exact_budget
):
    from dataclasses import replace
    from langchain_core.messages import AIMessage, AIMessageChunk
    from nianlun.agent.lead_agent.runtime import AgentRuntime

    context = image_runtime
    answer = "Ordinary answer\n"
    if exact_budget:
        context.service.settings = replace(
            context.service.settings, answer_image_max_canonical_bytes=len(answer)
        )

    class Graph:
        def stream(self, *args, **kwargs):
            yield {
                "type": "messages",
                "data": (
                    AIMessageChunk(content=answer.rstrip()),
                    {"langgraph_node": "model" if agent_version == "v0" else "answer"},
                ),
            }
            yield {
                "type": "values",
                "data": {
                    "messages": [AIMessage(content=answer)],
                    "answer": answer,
                },
            }

    monkeypatch.setattr(
        context.publication.applications,
        "release_runtime",
        lambda *args, **kwargs: AgentRuntime(
            agent=Graph(),
            model="fake",
            effective_url="https://fake.test",
            tool_logging=False,
            kb=SimpleNamespace(has_fts=True, has_vector=False),
            agent_version=agent_version,
            tool_schema_version=5,
            image_capability=True,
        ),
    )
    app = FastAPI()
    app.state.services = SimpleNamespace(invocations=context.service)
    app.include_router(v1_router)
    with TestClient(app) as client:
        response = client.post(
            f"/api/v1/runtime/apps/{context.app_id}/chat",
            headers={
                "Authorization": f"Bearer {context.token}",
                "Idempotency-Key": "real-v0-stream",
            },
            json={
                "message": "Explain the evidence",
                "user_id": "user",
                "response_mode": "streaming",
                "include_images": True,
            },
        )
    events = [
        (lines[0][7:], json.loads(lines[1][6:]))
        for block in response.text.strip().split("\n\n")
        if (lines := block.splitlines()) and lines[0].startswith("event: ")
    ]
    assert events[-1][0] == "done", response.text
    assert (
        "".join(data["delta"] for name, data in events if name == "message") == answer
    )


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("transport", ["url", "data_url"])
def test_image_stream_finishes_when_source_images_are_inserted_between_paragraphs(
    image_runtime, monkeypatch, native, transport
):
    context = image_runtime
    original = context.publication.applications.release_runtime(None, None, None)
    answer = "Introduction\n\nFact [1]\n\nSecond paragraph\n"

    def events(*args, **kwargs):
        for part in ("Introduction\n\n", "Fact [", "1]\n\n", "Second paragraph\n"):
            yield {"type": "message", "data": {"delta": part}}
        for event in original.iter_events(*args, **kwargs):
            if event["type"] == "done":
                event["data"]["answer"] = answer
                yield event

    monkeypatch.setattr(
        context.publication.applications,
        "release_runtime",
        lambda *args, **kwargs: SimpleNamespace(
            iter_events=events, iter_public_events=events
        ),
    )
    app = FastAPI()
    app.state.services = SimpleNamespace(invocations=context.service)
    app.include_router(v1_router)
    path = (
        f"/api/v1/runtime/apps/{context.app_id}/chat"
        if native
        else "/v1/chat/completions"
    )
    body = (
        {
            "message": "Explain the evidence",
            "user_id": "user",
            "response_mode": "streaming",
            "include_images": True,
            "image_transport": transport,
        }
        if native
        else {
            "model": context.app_id,
            "user": "user",
            "messages": [{"role": "user", "content": "Explain the evidence"}],
            "stream": True,
        }
    )
    with TestClient(app) as client:
        response = client.post(
            path,
            headers={
                "Authorization": f"Bearer {context.token}",
                "Idempotency-Key": "auto-image-stream",
                "X-Nianlun-Include-Images": "true",
                "X-Nianlun-Image-Transport": transport,
            },
            json=body,
        )
    content = ""
    if native:
        events = [
            (lines[0][7:], json.loads(lines[1][6:]))
            for block in response.text.strip().split("\n\n")
            if (lines := block.splitlines()) and lines[0].startswith("event: ")
        ]
        assert events[-1][0] == "done", response.text
        content = "".join(data["delta"] for name, data in events if name == "message")
        assert content == events[-1][1]["answer"]
    else:
        assert response.text.endswith("data: [DONE]\n\n"), response.text
        for line in response.text.splitlines():
            if line.startswith("data: {"):
                for choice in json.loads(line[6:])["choices"]:
                    content += choice["delta"].get("content", "")
    assert content.count("Source image") == 1
    assert (
        content.index("Fact [1]")
        < content.index("Source image")
        < content.index("Second paragraph")
    )
    assert "nianlun-image:" not in content
    row = context.service.repository.list(context.app_id)[0]
    saved = context.service.get(context.principal, row["id"], "user").result
    assert saved is not None and content == saved.answer


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("container", ["> ", "- ", "> - 1. > "])
def test_reference_image_definitions_finish_without_exposing_uncommitted_text(
    image_runtime, monkeypatch, native, container
):
    context = image_runtime
    original = context.publication.applications.release_runtime(None, None, None)

    def events(*args, **kwargs):
        for event in original.iter_events(*args, **kwargs):
            if event["type"] == "done":
                image_id = event["data"]["retrieved_snippets"][0]["images"][0][
                    "image_id"
                ]
                parts = (
                    "Introduction\n\n",
                    container + "[pi",
                    "c]: ",
                    f"nianlun-image:{image_id}\n",
                    "\n![pic]\n",
                )
                for part in parts:
                    yield {"type": "message", "data": {"delta": part}}
                event["data"]["answer"] = "".join(parts)
                yield event

    monkeypatch.setattr(
        context.publication.applications,
        "release_runtime",
        lambda *args, **kwargs: SimpleNamespace(
            iter_events=events, iter_public_events=events
        ),
    )
    app = FastAPI()
    app.state.services = SimpleNamespace(invocations=context.service)
    app.include_router(v1_router)
    path = (
        f"/api/v1/runtime/apps/{context.app_id}/chat"
        if native
        else "/v1/chat/completions"
    )
    body = (
        {
            "message": "Show the image",
            "user_id": "user",
            "response_mode": "streaming",
            "include_images": True,
        }
        if native
        else {
            "model": context.app_id,
            "user": "user",
            "messages": [{"role": "user", "content": "Show the image"}],
            "stream": True,
        }
    )
    with TestClient(app) as client:
        response = client.post(
            path,
            headers={
                "Authorization": f"Bearer {context.token}",
                "Idempotency-Key": "reference-image-stream",
                "X-Nianlun-Include-Images": "true",
            },
            json=body,
        )
    assert response.status_code == 200
    if native:
        events = [
            (lines[0][7:], json.loads(lines[1][6:]))
            for block in response.text.strip().split("\n\n")
            if (lines := block.splitlines()) and lines[0].startswith("event: ")
        ]
        assert events[-1][0] == "done", response.text
        content = "".join(data["delta"] for name, data in events if name == "message")
        assert content == events[-1][1]["answer"]
    else:
        assert response.text.endswith("data: [DONE]\n\n"), response.text
        content = "".join(
            choice["delta"].get("content", "")
            for line in response.text.splitlines()
            if line.startswith("data: {")
            for choice in json.loads(line[6:])["choices"]
        )
    assert "nianlun-image:" not in response.text
    assert "[pic]" not in response.text
    assert content.startswith("Introduction\n\n")
    assert content.count("Source image") == 1
    row = context.service.repository.list(context.app_id)[0]
    saved = context.service.get(context.principal, row["id"], "user").result
    assert saved is not None and content == saved.answer
