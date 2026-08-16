import asyncio
import json
from types import SimpleNamespace

import pytest
from langchain.agents import create_agent
from langchain.agents.middleware import ModelRequest, ToolCallRequest
from langchain.tools import tool
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.errors import GraphInterrupt
from langgraph.types import Command

from nianlun.agent.middleware import (
    ClarificationMiddleware,
    DanglingToolCall,
    DanglingToolCallMiddleware,
    ToolErrorHandlingMiddleware,
    RetrievalDeduplicationMiddleware,
    deduplicate_retrieval_result,
    find_missing_tool_results_for_model_tool_calls,
    repair_missing_tool_results_for_model_tool_calls,
)
from nianlun.agent.tools import ask_clarification_tool


def _tool_request(name="search_document_nodes", call_id="call-1"):
    return ToolCallRequest(
        tool_call={"name": name, "args": {}, "id": call_id, "type": "tool_call"},
        tool=None,
        state={"messages": []},
        runtime=SimpleNamespace(),
    )


def test_tool_error_middleware_returns_structured_error_message():
    request = _tool_request()
    result = ToolErrorHandlingMiddleware().wrap_tool_call(
        request,
        lambda _request: (_ for _ in ()).throw(ValueError("invalid line range")),
    )

    assert isinstance(result, ToolMessage)
    assert result.status == "error"
    assert result.tool_call_id == "call-1"
    assert result.name == "search_document_nodes"
    payload = json.loads(result.content)
    assert payload["error"]["code"] == "invalid_argument"
    assert payload["error"]["tool_call_id"] == "call-1"
    assert payload["error"]["retryable"] is False


def test_tool_error_middleware_passes_command_through():
    command = Command(goto="end")
    result = ToolErrorHandlingMiddleware().wrap_tool_call(
        _tool_request(), lambda _request: command
    )
    assert result is command


def test_tool_error_middleware_passes_graph_interrupt_through():
    with pytest.raises(GraphInterrupt):
        ToolErrorHandlingMiddleware().wrap_tool_call(
            _tool_request(), lambda _request: (_ for _ in ()).throw(GraphInterrupt())
        )


def test_tool_error_middleware_supports_async_handler():
    async def handler(_request):
        raise TimeoutError("milvus timeout")

    result = asyncio.run(
        ToolErrorHandlingMiddleware().awrap_tool_call(_tool_request(), handler)
    )
    payload = json.loads(result.content)
    assert payload["error"]["code"] == "transient_backend"
    assert payload["error"]["retryable"] is True


def test_tool_error_middleware_handles_real_agent_tool_failure():
    @tool("failing_tool")
    def failing_tool(value: str) -> str:
        """Raise a transient backend failure for integration testing."""
        raise TimeoutError(f"backend timeout for {value}")

    class FakeToolCallingModel(GenericFakeChatModel):
        def bind_tools(self, tools, **kwargs):
            return self

    model = FakeToolCallingModel(
        messages=iter(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "failing_tool",
                            "args": {"value": "doc-1"},
                            "id": "call-1",
                            "type": "tool_call",
                        }
                    ],
                ),
                AIMessage(content="done"),
            ]
        )
    )
    agent = create_agent(
        model=model,
        tools=[failing_tool],
        middleware=[ToolErrorHandlingMiddleware()],
    )

    result = agent.invoke({"messages": [{"role": "user", "content": "run"}]})

    tool_result = next(
        message for message in result["messages"] if isinstance(message, ToolMessage)
    )
    assert tool_result.status == "error"
    assert json.loads(tool_result.content)["error"]["retryable"] is True


def test_tool_error_middleware_catches_clarification_middleware_failure():
    class BrokenClarificationMiddleware(ClarificationMiddleware):
        def wrap_tool_call(self, request, handler):
            raise RuntimeError("clarification state failure")

    class FakeToolCallingModel(GenericFakeChatModel):
        def bind_tools(self, tools, **kwargs):
            return self

    model = FakeToolCallingModel(
        messages=iter(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "ask_clarification",
                            "args": {
                                "question": "需要哪份文档？",
                                "clarification_type": "missing_info",
                            },
                            "id": "call-clarify-1",
                            "type": "tool_call",
                        }
                    ],
                ),
                AIMessage(content="继续回答"),
            ]
        )
    )
    agent = create_agent(
        model=model,
        tools=[ask_clarification_tool],
        middleware=[ToolErrorHandlingMiddleware(), BrokenClarificationMiddleware()],
    )

    result = agent.invoke({"messages": [{"role": "user", "content": "帮我比较"}]})

    tool_result = next(
        message for message in result["messages"] if isinstance(message, ToolMessage)
    )
    assert tool_result.status == "error"
    assert tool_result.name == "ask_clarification"
    assert json.loads(tool_result.content)["error"]["code"] == "internal_error"


def _retrieval_result(*documents):
    return json.dumps(
        {"query": "收入", "documents": list(documents), "truncated": False},
        ensure_ascii=False,
    )


def _retrieval_request(name="search_document_nodes"):
    return ToolCallRequest(
        tool_call={
            "name": name,
            "args": {},
            "id": "call-retrieval",
            "type": "tool_call",
        },
        tool=None,
        state={"messages": []},
        runtime=SimpleNamespace(
            context={
                "retrieval_deduplication_state": {"documents": set(), "nodes": set()}
            }
        ),
    )


def test_retrieval_deduplication_keeps_only_new_nodes_from_later_searches():
    middleware = RetrievalDeduplicationMiddleware()
    request = _retrieval_request()
    first = ToolMessage(
        content=_retrieval_result(
            {
                "doc_id": "doc-1",
                "node_hints": [
                    {"node_id": "n1", "title": "收入", "line_num": 10},
                    {"node_id": "n2", "title": "成本", "line_num": 20},
                    {"node_id": "n2", "title": "成本", "line_num": 20},
                ],
            }
        ),
        tool_call_id="call-retrieval",
        name="search_document_nodes",
    )
    second = ToolMessage(
        content=_retrieval_result(
            {
                "doc_id": "doc-1",
                "node_hints": [
                    {"node_id": "n2", "title": "成本", "line_num": 20},
                    {"node_id": "n3", "title": "风险", "line_num": 30},
                ],
            }
        ),
        tool_call_id="call-retrieval",
        name="search_document_nodes",
    )

    first_result = middleware.wrap_tool_call(request, lambda _request: first)
    result = middleware.wrap_tool_call(request, lambda _request: second)

    assert json.loads(result.content)["documents"] == [
        {
            "doc_id": "doc-1",
            "node_hints": [{"node_id": "n3", "title": "风险", "line_num": 30}],
        }
    ]
    assert json.loads(result.content)["deduplication"] == {
        "applied": True,
        "reason": "本次检索命中存在重复的文档或节点。系统已排除与当前请求内已处理的检索结果重合的部分，仅返回尚未提供的新内容，以减少重复上下文。",
        "removed_documents": 0,
        "removed_node_hints": 1,
    }
    assert len(json.loads(first_result.content)["documents"][0]["node_hints"]) == 2


def test_retrieval_deduplication_removes_repeated_document_only_results():
    state = {"documents": set(), "nodes": set()}
    result = _retrieval_result({"doc_id": "doc-1", "doc_name": "报告", "node_hints": []})

    assert json.loads(deduplicate_retrieval_result(result, state))["documents"]
    deduplicated = json.loads(deduplicate_retrieval_result(result, state))
    assert deduplicated["documents"] == []
    assert deduplicated["deduplication"]["removed_documents"] == 1


def test_retrieval_deduplication_leaves_errors_invalid_json_and_other_tools_unchanged():
    middleware = RetrievalDeduplicationMiddleware()
    request = _retrieval_request("get_line_content")
    invalid = ToolMessage(content="not-json", tool_call_id="call-retrieval")

    assert middleware.wrap_tool_call(request, lambda _request: invalid) is invalid
    assert (
        deduplicate_retrieval_result(
            '{"error":"backend"}', {"documents": set(), "nodes": set()}
        )
        == '{"error":"backend"}'
    )


def test_retrieval_deduplication_leaves_structured_content_unchanged():
    middleware = RetrievalDeduplicationMiddleware()
    request = _retrieval_request()
    structured = ToolMessage(
        content=[{"type": "text", "text": "structured result"}],
        tool_call_id="call-retrieval",
    )

    assert middleware.wrap_tool_call(request, lambda _request: structured) is structured


def test_retrieval_deduplication_supports_async_tool_handlers():
    middleware = RetrievalDeduplicationMiddleware()
    request = _retrieval_request()
    result = ToolMessage(
        content=_retrieval_result({"doc_id": "doc-1", "node_hints": []}),
        tool_call_id="call-retrieval",
    )

    async def handler(_request):
        return result

    assert asyncio.run(middleware.awrap_tool_call(request, handler)).content


def _ai(*calls):
    return AIMessage(
        content="",
        tool_calls=[
            {"name": name, "args": {}, "id": call_id, "type": "tool_call"}
            for name, call_id in calls
        ],
    )


def test_find_missing_tool_results_for_model_tool_calls_reports_only_missing_results():
    messages = [
        _ai(("search_document_nodes", "call-1"), ("get_line_content", "call-2")),
        ToolMessage(content="ok", tool_call_id="call-1"),
        HumanMessage(content="continue"),
    ]

    assert find_missing_tool_results_for_model_tool_calls(messages) == (
        # The second result is missing, while call-1 is complete.
        DanglingToolCall("call-2", "get_line_content"),
    )


def test_repair_missing_tool_results_inserts_results_after_tool_group():
    first = _ai(("search_document_nodes", "call-1"), ("get_line_content", "call-2"))
    existing = ToolMessage(content="ok", tool_call_id="call-1")
    user = HumanMessage(content="next")
    messages = [first, existing, user]

    repaired = repair_missing_tool_results_for_model_tool_calls(messages)

    assert repaired[:2] == [first, existing]
    assert isinstance(repaired[2], ToolMessage)
    assert repaired[2].tool_call_id == "call-2"
    assert repaired[2].status == "error"
    assert repaired[3] is user
    assert messages == [first, existing, user]


def test_repair_parallel_model_tool_calls_only_adds_missing_call():
    first = _ai(("a", "call-1"), ("b", "call-2"), ("c", "call-3"))
    results = [
        ToolMessage(content="a-result", tool_call_id="call-1"),
        ToolMessage(content="c-result", tool_call_id="call-3"),
    ]

    repaired = repair_missing_tool_results_for_model_tool_calls([first, *results])

    assert [message.tool_call_id for message in repaired[1:]] == [
        "call-1",
        "call-3",
        "call-2",
    ]


def test_dangling_middleware_rewrites_only_model_request():
    first = _ai(("search_document_nodes", "call-1"))
    request = ModelRequest(model=object(), messages=[first])
    seen = []
    repaired_by_callback = []

    response = DanglingToolCallMiddleware(
        on_repair=repaired_by_callback.extend
    ).wrap_model_call(
        request,
        lambda updated: seen.append(updated) or "model-response",
    )

    assert response == "model-response"
    assert seen[0] is not request
    assert seen[0].messages[0] is first
    assert isinstance(seen[0].messages[1], ToolMessage)
    assert repaired_by_callback[0].tool_call_id == "call-1"
    assert request.messages == [first]


def test_dangling_middleware_async_handler():
    request = ModelRequest(
        model=object(),
        messages=[_ai(("search_document_nodes", "call-1"))],
    )

    async def handler(updated):
        assert isinstance(updated.messages[1], ToolMessage)
        return "async-response"

    assert (
        asyncio.run(DanglingToolCallMiddleware().awrap_model_call(request, handler))
        == "async-response"
    )


def test_dangling_middleware_persists_repair_in_state():
    middleware = DanglingToolCallMiddleware()
    state = {"messages": [_ai(("search_document_nodes", "call-1"))]}

    update = middleware.before_model(state, SimpleNamespace())

    assert update is not None
    assert isinstance(update["messages"][1], ToolMessage)
    assert middleware.before_model(update, SimpleNamespace()) is None


def test_complete_history_is_returned_without_rewrite():
    ai = _ai(("search_document_nodes", "call-1"))
    tool = ToolMessage(content="ok", tool_call_id="call-1")
    messages = [ai, tool]

    repaired = repair_missing_tool_results_for_model_tool_calls(messages)

    assert repaired == messages
    assert repaired is not messages
