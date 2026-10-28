import asyncio
import json
import threading
from types import SimpleNamespace
from typing import Any

import pytest
from langchain.agents import create_agent
from langchain.tools import ToolRuntime, tool
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, ToolMessage
from pydantic import ValidationError

from nianlun.agent.image_context import AnswerImagePolicy, RequestImageContext
from nianlun.agent.middleware.answer_image_selection_middleware import (
    AnswerImageSelectionMiddleware,
)
from nianlun.agent.tools.answer_images import select_answer_images_tool


class Model(GenericFakeChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


def context(enabled=True):
    images = RequestImageContext(enabled=enabled, policy=AnswerImagePolicy())
    collector = SimpleNamespace(
        image_context=images, record_tool_call=lambda *a, **k: None
    )
    return images, {"retrieval_collector": collector}


def register(images):
    images.project_item(
        {
            "citation_id": 1,
            "text": "![x](a)",
            "images": [
                {
                    "image_id": "img_" + "a" * 16,
                    "generation": 1,
                    "knowledge_base_id": "kb",
                    "content_version": 1,
                    "asset_relpath": "secret/a.png",
                    "sha256": "a" * 64,
                    "media_type": "image/png",
                    "size_bytes": 10,
                    "width": 1,
                    "height": 1,
                    "source_start": 0,
                    "source_end": 7,
                }
            ],
            "image_spans": [{"kind": "markdown", "source_start": 0, "source_end": 7}],
        },
        "doc",
    )


def call(name, id, **args):
    return {"name": name, "id": id, "args": args, "type": "tool_call"}


@pytest.mark.parametrize(
    "args",
    [
        {"mode": "images", "image_refs": []},
        {"mode": "images", "image_refs": ["img_01", "img_01"]},
        {"mode": "images", "image_refs": ["https://secret"]},
        {"mode": "images", "image_refs": [1]},
        {"mode": "images", "image_refs": ["img_01"], "extra": True},
        {"mode": "text_and_images", "image_refs": ["img_01"]},
    ],
)
def test_strict_schema(args):
    assert (
        "runtime"
        not in select_answer_images_tool.tool_call_schema.model_json_schema()[
            "properties"
        ]
    )
    with pytest.raises(ValidationError):
        select_answer_images_tool.args_schema.model_validate(args)


@pytest.mark.parametrize("asynchronous", [False, True])
def test_real_agent_freezes_before_read_and_next_batch_corrects(asynchronous):
    images, ctx = context()
    read_completed = threading.Event()

    class ReadFirstMiddleware(AnswerImageSelectionMiddleware):
        def wrap_tool_call(self, request, handler):
            if request.tool_call["id"] == "early":
                assert read_completed.wait(2)
            return super().wrap_tool_call(request, handler)

        async def awrap_tool_call(self, request, handler):
            if request.tool_call["id"] == "early":
                assert await asyncio.to_thread(read_completed.wait, 2)
            return await super().awrap_tool_call(request, handler)

    @tool
    def get_line_content(runtime: ToolRuntime[Any, Any]) -> str:
        """Read one image."""
        assert images.selection_preflight_error("early") is None
        register(images)
        read_completed.set()
        return "img_01"

    agent = create_agent(
        Model(
            messages=iter(
                [
                    AIMessage(
                        content="",
                        tool_calls=[
                            call("get_line_content", "read"),
                            call(
                                "select_answer_images",
                                "early",
                                mode="images",
                                image_refs=["img_01"],
                            ),
                        ],
                    ),
                    AIMessage(
                        content="",
                        tool_calls=[
                            call(
                                "select_answer_images",
                                "correct",
                                mode="images",
                                image_refs=["img_01"],
                            )
                        ],
                    ),
                    AIMessage(content="done"),
                ]
            )
        ),
        tools=[get_line_content, select_answer_images_tool],
        middleware=[ReadFirstMiddleware()],
        context_schema=dict,
    )
    result = (
        asyncio.run(
            agent.ainvoke(
                {"messages": [{"role": "user", "content": "images"}]}, context=ctx
            )
        )
        if asynchronous
        else agent.invoke(
            {"messages": [{"role": "user", "content": "images"}]}, context=ctx
        )
    )
    messages = {
        m.tool_call_id: m for m in result["messages"] if isinstance(m, ToolMessage)
    }
    assert messages["early"].status == "error"
    assert messages["correct"].status == "success", messages["correct"].content
    assert json.loads(messages["correct"].content) == {
        "mode": "images",
        "image_refs": ["img_01"],
    }
    assert len(images.selected_images) == 1


@pytest.mark.parametrize("asynchronous", [False, True])
def test_real_agent_rejects_entire_multiple_selection_batch(asynchronous):
    images, ctx = context()
    register(images)
    images.freeze_tool_batch([call("select_answer_images", "old")])
    images.select_answer_images("old", ["img_01"])
    agent = create_agent(
        Model(
            messages=iter(
                [
                    AIMessage(
                        content="",
                        tool_calls=[
                            call(
                                "select_answer_images",
                                "one",
                                mode="images",
                                image_refs=["img_01"],
                            ),
                            call(
                                "select_answer_images",
                                "two",
                                mode="images",
                                image_refs=["img_01"],
                            ),
                        ],
                    ),
                    AIMessage(content="done"),
                ]
            )
        ),
        tools=[select_answer_images_tool],
        middleware=[AnswerImageSelectionMiddleware()],
        context_schema=dict,
    )
    inputs = {"messages": [{"role": "user", "content": "images"}]}
    result = (
        asyncio.run(agent.ainvoke(inputs, context=ctx))
        if asynchronous
        else agent.invoke(inputs, context=ctx)
    )
    results = [m for m in result["messages"] if isinstance(m, ToolMessage)]
    assert len(results) == 2
    assert all(m.status == "error" for m in results)
    assert len(images.selected_images) == 1


@pytest.mark.parametrize("asynchronous", [False, True])
def test_denied_preflight_never_calls_handler(asynchronous):
    from langchain.agents.middleware import ToolCallRequest

    images, ctx = context()
    register(images)
    images.freeze_tool_batch([call("select_answer_images", "old")])
    images.select_answer_images("old", ["img_01"])
    middleware = AnswerImageSelectionMiddleware()
    for reason in ("missing", "multiple", "disabled"):
        if reason == "multiple":
            images.freeze_tool_batch(
                [
                    call("select_answer_images", "bad"),
                    call("select_answer_images", "other"),
                ]
            )
        if reason == "disabled":
            images.enabled = False
        request = ToolCallRequest(
            tool_call=call(
                "select_answer_images", "bad", mode="images", image_refs=["img_01"]
            ),
            tool=select_answer_images_tool,
            state={"messages": []},
            runtime=SimpleNamespace(context=ctx),
        )

        def handler(request):
            raise AssertionError("denied handler executed")

        result = (
            asyncio.run(middleware.awrap_tool_call(request, handler))
            if asynchronous
            else middleware.wrap_tool_call(request, handler)
        )
        assert result.status == "error"
        assert json.loads(result.content)["code"] == "answer_image_selection_invalid"
        assert len(images.selected_images) == 1


@pytest.mark.parametrize("asynchronous", [False, True])
def test_valid_preflight_does_not_commit_before_handler(asynchronous):
    from langchain.agents.middleware import ToolCallRequest

    images, ctx = context()
    register(images)
    images.freeze_tool_batch([call("select_answer_images", "valid")])
    request = ToolCallRequest(
        tool_call=call(
            "select_answer_images", "valid", mode="images", image_refs=["img_01"]
        ),
        tool=select_answer_images_tool,
        state={"messages": []},
        runtime=SimpleNamespace(context=ctx),
    )
    denied = ToolMessage(
        content="downstream denied", tool_call_id="valid", status="error"
    )

    def handler(request):
        assert images.selected_images == []
        return denied

    async def ahandler(request):
        return handler(request)

    middleware = AnswerImageSelectionMiddleware()
    result = (
        asyncio.run(middleware.awrap_tool_call(request, ahandler))
        if asynchronous
        else middleware.wrap_tool_call(request, handler)
    )
    assert result is denied
    assert images.selected_images == []


@pytest.mark.parametrize("asynchronous", [False, True])
def test_non_selection_calls_pass_without_context(asynchronous):
    from langchain.agents.middleware import ToolCallRequest

    request = ToolCallRequest(
        tool_call=call("get_line_content", "read"),
        tool=None,
        state={"messages": []},
        runtime=SimpleNamespace(context={}),
    )
    expected = ToolMessage(content="read", tool_call_id="read")

    async def ahandler(request):
        return expected

    middleware = AnswerImageSelectionMiddleware()
    result = (
        asyncio.run(middleware.awrap_tool_call(request, ahandler))
        if asynchronous
        else middleware.wrap_tool_call(request, lambda request: expected)
    )
    assert result is expected
