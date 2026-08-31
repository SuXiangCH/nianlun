import asyncio
import json
import time
from types import SimpleNamespace

import pytest
from langchain.agents import create_agent
from langchain.agents.middleware import ModelRequest, ModelResponse, ToolCallRequest
from langchain.tools import tool
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.errors import GraphInterrupt
from langgraph.types import Command

from nianlun.agent.batch import is_retryable_batch_error
from nianlun.agent.middleware import (
    AgentLoopGuardConfig,
    ClarificationMiddleware,
    ContextSummarizationMiddleware,
    DanglingToolCall,
    DanglingToolCallMiddleware,
    GuardFinalizationError,
    LoopGuardState,
    ToolErrorHandlingMiddleware,
    RetrievalDeduplicationMiddleware,
    RetrievalLoopGuardMiddleware,
    deduplicate_retrieval_result,
    find_missing_tool_results_for_model_tool_calls,
    repair_missing_tool_results_for_model_tool_calls,
    tool_call_fingerprint,
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
    result = _retrieval_result(
        {"doc_id": "doc-1", "doc_name": "报告", "node_hints": []}
    )

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


def test_loop_guard_blocks_third_identical_tool_call_and_keeps_tool_pairing():
    middleware = RetrievalLoopGuardMiddleware(
        AgentLoopGuardConfig(
            model_round_warn_threshold=8,
            max_model_rounds=10,
            tool_round_warn_threshold=8,
            max_tool_rounds=10,
            total_tool_call_warn_threshold=8,
            max_total_tool_calls=10,
        )
    )
    context = {}
    runtime = SimpleNamespace(context=context)
    executions = []

    for index in range(3):
        call = {
            "name": "get_structure_outline",
            "args": {"doc_id": "doc-1"},
            "id": f"call-{index}",
            "type": "tool_call",
        }
        middleware.after_model(
            {"messages": [AIMessage(content="", tool_calls=[call])]}, runtime
        )
        request = ToolCallRequest(
            tool_call=call,
            tool=None,
            state={"messages": []},
            runtime=runtime,
        )
        result = middleware.wrap_tool_call(
            request,
            lambda _request: (
                executions.append(_request.tool_call["id"])
                or ToolMessage(
                    content='{"doc_id":"doc-1"}', tool_call_id=_request.tool_call["id"]
                )
            ),
        )
        assert isinstance(result, ToolMessage)
        assert result.tool_call_id == f"call-{index}"
        if index == 2:
            assert result.status == "error"
            assert json.loads(result.content)["error"]["code"] == "loop_detected"

    assert executions == ["call-0", "call-1"]
    assert context["loop_guard_state"].trigger == "identical_call_set"


def test_loop_guard_parallel_duplicate_batch_executes_allowance_in_model_order():
    middleware = RetrievalLoopGuardMiddleware()
    guard = LoopGuardState()
    runtime = SimpleNamespace(context={"loop_guard_state": guard})
    calls = [
        {
            "name": "get_document",
            "args": {"doc_id": "doc-1"},
            "id": f"call-{index}",
            "type": "tool_call",
        }
        for index in range(3)
    ]
    middleware.after_model(
        {"messages": [AIMessage(content="", tool_calls=calls)]}, runtime
    )
    executions = []

    results = []
    for call in calls:
        request = ToolCallRequest(
            tool_call=call,
            tool=None,
            state={"messages": []},
            runtime=runtime,
        )
        results.append(
            middleware.wrap_tool_call(
                request,
                lambda current: (
                    executions.append(current.tool_call["id"])
                    or ToolMessage(
                        content='{"doc_id":"doc-1"}',
                        tool_call_id=current.tool_call["id"],
                    )
                ),
            )
        )

    assert executions == ["call-0", "call-1"]
    assert [result.status for result in results] == ["success", "success", "error"]
    assert guard.trigger == "identical_tool_call"


def test_loop_guard_treats_distinct_plain_text_outlines_as_progress():
    middleware = RetrievalLoopGuardMiddleware()
    guard = LoopGuardState()
    runtime = SimpleNamespace(context={"loop_guard_state": guard})

    for index in range(4):
        call = {
            "name": "get_structure_outline",
            "args": {"doc_id": f"doc-{index}"},
            "id": f"call-{index}",
            "type": "tool_call",
        }
        middleware.after_model(
            {"messages": [AIMessage(content="", tool_calls=[call])]}, runtime
        )
        request = ToolCallRequest(
            tool_call=call,
            tool=None,
            state={"messages": []},
            runtime=runtime,
        )
        result = middleware.wrap_tool_call(
            request,
            lambda current: ToolMessage(
                content="[node-1] 第 1 行: 标题",
                tool_call_id=current.tool_call["id"],
            ),
        )
        assert result.status == "success"

    assert guard.finalizing is False
    assert guard.consecutive_no_progress_rounds == 0


def test_loop_guard_treats_new_nodes_in_same_document_as_progress():
    middleware = RetrievalLoopGuardMiddleware()
    guard = LoopGuardState()
    runtime = SimpleNamespace(context={"loop_guard_state": guard})

    for index in range(4):
        call = {
            "name": "search_document_nodes",
            "args": {"query": f"query-{index}"},
            "id": f"call-{index}",
            "type": "tool_call",
        }
        middleware.after_model(
            {"messages": [AIMessage(content="", tool_calls=[call])]}, runtime
        )
        request = ToolCallRequest(
            tool_call=call,
            tool=None,
            state={"messages": []},
            runtime=runtime,
        )
        result = middleware.wrap_tool_call(
            request,
            lambda current, node=index: ToolMessage(
                content=json.dumps(
                    {
                        "documents": [
                            {
                                "doc_id": "doc-1",
                                "node_hints": [{"node_id": f"node-{node}"}],
                            }
                        ]
                    }
                ),
                tool_call_id=current.tool_call["id"],
            ),
        )
        assert result.status == "success"

    assert guard.finalizing is False
    assert guard.consecutive_no_progress_rounds == 0


def test_loop_guard_normalizes_arguments_in_tool_fingerprint():
    first = {
        "name": "search_document_nodes",
        "args": {"query": "  revenue\n growth ", "doc_ids": ["b", "a"]},
    }
    second = {
        "name": "search_document_nodes",
        "args": {"doc_ids": ["a", "b"], "query": "revenue growth"},
    }
    assert tool_call_fingerprint(first) == tool_call_fingerprint(second)


def test_loop_guard_fingerprint_supports_json_string_arguments():
    mapping_call = {
        "name": "search_document_nodes",
        "args": {"query": "revenue growth"},
    }
    string_call = {
        "name": "search_document_nodes",
        "args": '{"query":"revenue growth"}',
    }
    different_call = {
        "name": "search_document_nodes",
        "args": '{"query":"cost growth"}',
    }
    malformed_call = {
        "name": "search_document_nodes",
        "args": '{"query":"revenue growth"',
    }

    assert tool_call_fingerprint(mapping_call) == tool_call_fingerprint(string_call)
    assert tool_call_fingerprint(string_call) != tool_call_fingerprint(different_call)
    assert tool_call_fingerprint(string_call) != tool_call_fingerprint(malformed_call)


def test_loop_guard_fingerprint_uses_effective_line_content_limit():
    first = {
        "name": "get_line_content",
        "args": {"doc_id": "doc-1", "line_spec": "1", "char_limit": 9_000},
    }
    second = {
        "name": "get_line_content",
        "args": {"doc_id": "doc-1", "line_spec": "1", "char_limit": 10_000},
    }

    assert tool_call_fingerprint(first) == tool_call_fingerprint(second)


def test_loop_guard_fingerprint_uses_only_effective_tool_arguments():
    assert tool_call_fingerprint(
        {
            "name": "get_document",
            "args": {"doc_id": "doc-1", "ignored": "first"},
        }
    ) == tool_call_fingerprint(
        {
            "name": "get_document",
            "args": {"doc_id": "doc-1", "ignored": "second"},
        }
    )
    assert tool_call_fingerprint(
        {
            "name": "get_line_content",
            "args": {"doc_id": "doc-1", "line_spec": "1, 2"},
        }
    ) == tool_call_fingerprint(
        {
            "name": "get_line_content",
            "args": {
                "doc_id": "doc-1",
                "line_spec": "1,2",
                "char_offset": 0,
                "char_limit": 4_000,
            },
        }
    )
    assert tool_call_fingerprint(
        {"name": "find_semantic_documents", "args": {"query": "risk"}}
    ) == tool_call_fingerprint(
        {
            "name": "find_semantic_documents",
            "args": {"query": "risk", "top_k": 15},
        }
    )
    assert tool_call_fingerprint(
        {
            "name": "ask_clarification",
            "args": {
                "question": "Which document?",
                "clarification_type": "missing_info",
                "context": "first",
            },
        }
    ) == tool_call_fingerprint(
        {
            "name": "ask_clarification",
            "args": {
                "question": "Which document?",
                "clarification_type": "missing_info",
                "context": "second",
            },
        }
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"enabled": 1},
        {"max_model_rounds": True},
        {"case_timeout_seconds": True},
        {"per_tool_hard_limits": {"get_document": True}},
    ],
)
def test_loop_guard_config_rejects_boolean_numeric_values(kwargs):
    with pytest.raises(ValueError):
        AgentLoopGuardConfig(**kwargs)


def test_loop_guard_partial_per_tool_limits_preserve_other_defaults():
    config = AgentLoopGuardConfig(per_tool_hard_limits={"get_document": 10})

    assert config.per_tool_hard_limits["get_document"] == 10
    assert config.per_tool_hard_limits["get_structure_outline"] == 50
    assert config.per_tool_hard_limits["get_line_content"] == 200


def test_loop_guard_snapshots_per_tool_limits_at_construction():
    config = AgentLoopGuardConfig(per_tool_hard_limits={"get_document": 1})
    middleware = RetrievalLoopGuardMiddleware(config)
    config.per_tool_hard_limits["get_document"] = 999  # type: ignore[index]
    guard = LoopGuardState()
    runtime = SimpleNamespace(context={"loop_guard_state": guard})
    calls = [
        {
            "name": "get_document",
            "args": {"doc_id": f"doc-{index}"},
            "id": f"call-{index}",
            "type": "tool_call",
        }
        for index in range(2)
    ]

    middleware.after_model(
        {"messages": [AIMessage(content="", tool_calls=calls)]}, runtime
    )

    assert guard.allowed_tool_call_ids == {"call-0"}
    assert guard.blocked_tool_call_ids == {"call-1"}
    assert guard.trigger == "per_tool_limit"


def test_loop_guard_rejects_recursion_limit_below_round_budget():
    with pytest.raises(ValueError, match="recursion_limit must be at least"):
        AgentLoopGuardConfig(
            max_model_rounds=40,
            max_tool_rounds=40,
            recursion_limit=100,
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("model_round_warn_threshold", 33, "max_model_rounds"),
        ("tool_round_warn_threshold", 33, "max_tool_rounds"),
        ("total_tool_call_warn_threshold", 301, "max_total_tool_calls"),
    ],
)
def test_loop_guard_rejects_warning_threshold_above_hard_limit(field, value, message):
    with pytest.raises(ValueError, match=message):
        AgentLoopGuardConfig(**{field: value})


def test_loop_guard_absolute_budget_warnings_are_transient_and_once_per_trigger():
    middleware = RetrievalLoopGuardMiddleware(
        AgentLoopGuardConfig(
            model_round_warn_threshold=2,
            max_model_rounds=4,
            tool_round_warn_threshold=2,
            max_tool_rounds=4,
            total_tool_call_warn_threshold=2,
            max_total_tool_calls=4,
        )
    )
    guard = LoopGuardState()
    runtime = SimpleNamespace(context={"loop_guard_state": guard})
    for index in range(2):
        call = {
            "name": "get_document",
            "args": {"doc_id": f"doc-{index}"},
            "id": f"call-{index}",
            "type": "tool_call",
        }
        middleware.after_model(
            {"messages": [AIMessage(content="", tool_calls=[call])]}, runtime
        )

    request = ModelRequest(
        model=object(),
        messages=[],
        tools=[object()],
        runtime=runtime,
        system_message=SystemMessage(content="base"),
    )
    seen = []
    response = type("Response", (), {"result": [AIMessage(content="continue")]})()
    middleware.wrap_model_call(
        request, lambda updated: seen.append(updated) or response
    )
    middleware.wrap_model_call(
        request, lambda updated: seen.append(updated) or response
    )

    assert guard.finalizing is False
    assert guard.warned_warning_keys == {
        ("model_round_limit", None),
        ("tool_round_limit", None),
        ("total_tool_limit", None),
    }
    assert "交互较多轮" in seen[0].system_message.text
    assert "工具调用轮次" in seen[0].system_message.text
    assert "工具调用总量" in seen[0].system_message.text
    assert seen[1].system_message.text == "base"


def test_loop_guard_deduplicates_warning_text_and_scopes_repeats_by_fingerprint():
    middleware = RetrievalLoopGuardMiddleware(
        AgentLoopGuardConfig(
            no_progress_warn_rounds=10,
            no_progress_hard_rounds=11,
        )
    )
    guard = LoopGuardState()
    runtime = SimpleNamespace(context={"loop_guard_state": guard})
    call_index = 0

    def record_call(doc_id):
        nonlocal call_index
        call = {
            "name": "get_document",
            "args": {"doc_id": doc_id},
            "id": f"call-{call_index}",
            "type": "tool_call",
        }
        call_index += 1
        middleware.after_model(
            {"messages": [AIMessage(content="", tool_calls=[call])]}, runtime
        )

    record_call("doc-1")
    record_call("doc-1")

    assert guard.pending_warnings == [
        "重复的工具调用没有带来新的信息，请基于已有证据完成回答。"
    ]
    seen = []
    middleware.wrap_model_call(
        ModelRequest(model=object(), messages=[], tools=[], runtime=runtime),
        lambda updated: (
            seen.append(updated)
            or ModelResponse(result=[AIMessage(content="continue")])
        ),
    )
    assert seen[0].system_message.text.count("重复的工具调用") == 1

    record_call("doc-2")
    record_call("doc-2")

    assert guard.pending_warnings == [
        "重复的工具调用没有带来新的信息，请基于已有证据完成回答。"
    ]
    repeat_warning_keys = {
        key
        for key in guard.warned_warning_keys
        if key[0] in {"identical_call_set", "identical_tool_call"}
    }
    assert len(repeat_warning_keys) == 4


def test_loop_guard_can_be_disabled_without_mutating_or_blocking_requests():
    middleware = RetrievalLoopGuardMiddleware(AgentLoopGuardConfig(enabled=False))
    guard = LoopGuardState(finalizing=True)
    runtime = SimpleNamespace(context={"loop_guard_state": guard})
    call = {
        "name": "get_document",
        "args": {"doc_id": "doc-1"},
        "id": "call-disabled",
        "type": "tool_call",
    }
    assert (
        middleware.after_model(
            {"messages": [AIMessage(content="", tool_calls=[call])]}, runtime
        )
        is None
    )
    model_request = ModelRequest(
        model=object(), messages=[], tools=[object()], runtime=runtime
    )
    seen_model_requests = []
    middleware.wrap_model_call(
        model_request,
        lambda updated: (
            seen_model_requests.append(updated)
            or type("Response", (), {"result": [AIMessage(content="ok")]})()
        ),
    )
    tool_request = ToolCallRequest(
        tool_call=call, tool=None, state={"messages": []}, runtime=runtime
    )
    executions = []
    tool_result = middleware.wrap_tool_call(
        tool_request,
        lambda current: (
            executions.append(current.tool_call["id"])
            or ToolMessage(content="ok", tool_call_id=current.tool_call["id"])
        ),
    )

    assert seen_model_requests == [model_request]
    assert executions == ["call-disabled"]
    assert tool_result.status == "success"
    assert guard.model_rounds == 0


def test_loop_guard_total_budget_uses_budget_exhausted_code():
    middleware = RetrievalLoopGuardMiddleware(
        AgentLoopGuardConfig(
            total_tool_call_warn_threshold=1,
            max_total_tool_calls=1,
        )
    )
    guard = LoopGuardState()
    runtime = SimpleNamespace(context={"loop_guard_state": guard})
    calls = [
        {
            "name": "get_document",
            "args": {"doc_id": f"doc-{index}"},
            "id": f"call-budget-{index}",
            "type": "tool_call",
        }
        for index in range(2)
    ]
    for call in calls:
        middleware.after_model(
            {"messages": [AIMessage(content="", tool_calls=[call])]}, runtime
        )
    request = ToolCallRequest(
        tool_call=calls[1], tool=None, state={"messages": []}, runtime=runtime
    )
    result = middleware.wrap_tool_call(
        request,
        lambda _request: pytest.fail("over-budget tool handler must not run"),
    )

    assert json.loads(result.content)["error"]["code"] == "budget_exhausted"


def test_loop_guard_finalization_failure_is_wrapped_without_sensitive_message():
    middleware = RetrievalLoopGuardMiddleware()
    guard = LoopGuardState(finalizing=True, trigger="no_progress")
    runtime = SimpleNamespace(context={"loop_guard_state": guard})
    request = ModelRequest(model=object(), messages=[], tools=[], runtime=runtime)

    class ProviderError(RuntimeError):
        status_code = 503

    with pytest.raises(GuardFinalizationError) as raised:
        middleware.wrap_model_call(
            request,
            lambda _request: (_ for _ in ()).throw(
                ProviderError("secret query and credential")
            ),
        )

    assert "ProviderError status=503" in str(raised.value)
    assert "secret" not in str(raised.value)
    assert raised.value.__cause__ is None
    assert raised.value.guard["trigger"] == "no_progress"
    assert is_retryable_batch_error(raised.value) is False


def test_loop_guard_async_finalization_failure_is_not_retryable():
    middleware = RetrievalLoopGuardMiddleware()
    guard = LoopGuardState(finalizing=True, trigger="timeout")
    runtime = SimpleNamespace(context={"loop_guard_state": guard})
    request = ModelRequest(model=object(), messages=[], tools=[], runtime=runtime)

    async def handler(_request):
        raise TimeoutError("secret request timed out")

    with pytest.raises(GuardFinalizationError) as raised:
        asyncio.run(middleware.awrap_model_call(request, handler))

    assert "TimeoutError" in str(raised.value)
    assert "secret" not in str(raised.value)
    assert raised.value.__cause__ is None
    assert is_retryable_batch_error(raised.value) is False


def test_loop_guard_does_not_wrap_pre_finalization_model_failures():
    middleware = RetrievalLoopGuardMiddleware()
    runtime = SimpleNamespace(context={"loop_guard_state": LoopGuardState()})
    request = ModelRequest(model=object(), messages=[], tools=[], runtime=runtime)
    original = ValueError("ordinary model failure")

    with pytest.raises(ValueError) as raised:
        middleware.wrap_model_call(
            request, lambda _request: (_ for _ in ()).throw(original)
        )

    assert raised.value is original


def test_loop_guard_expired_request_skips_model_and_tool_handlers():
    middleware = RetrievalLoopGuardMiddleware(
        AgentLoopGuardConfig(case_timeout_seconds=1)
    )
    model_guard = LoopGuardState(started_at=time.monotonic() - 2)
    model_runtime = SimpleNamespace(context={"loop_guard_state": model_guard})
    model_request = ModelRequest(
        model=object(), messages=[], tools=[object()], runtime=model_runtime
    )
    seen_model_requests = []

    middleware.wrap_model_call(
        model_request,
        lambda updated: (
            seen_model_requests.append(updated)
            or type("Response", (), {"result": [AIMessage(content="final")]})()
        ),
    )

    assert seen_model_requests[0].tools == []
    assert model_guard.trigger == "timeout"

    tool_guard = LoopGuardState()
    tool_runtime = SimpleNamespace(context={"loop_guard_state": tool_guard})
    call = {
        "name": "get_document",
        "args": {"doc_id": "doc-1"},
        "id": "call-timeout",
        "type": "tool_call",
    }
    middleware.after_model(
        {"messages": [AIMessage(content="", tool_calls=[call])]}, tool_runtime
    )
    tool_guard.started_at = time.monotonic() - 2
    tool_request = ToolCallRequest(
        tool_call=call,
        tool=None,
        state={"messages": []},
        runtime=tool_runtime,
    )
    executions = []
    result = middleware.wrap_tool_call(
        tool_request,
        lambda current: (
            executions.append(current.tool_call["id"])
            or ToolMessage(
                content="should not run", tool_call_id=current.tool_call["id"]
            )
        ),
    )

    assert executions == []
    assert result.status == "error"
    assert tool_guard.trigger == "timeout"
    assert json.loads(result.content)["error"]["code"] == "timeout"


def test_loop_guard_final_model_call_hides_tools_and_ends_graph():
    middleware = RetrievalLoopGuardMiddleware()
    runtime = SimpleNamespace(context={})
    # Initialize through the middleware so production and test state creation match.
    middleware.after_model({"messages": [AIMessage(content="")]}, runtime)
    runtime.context["loop_guard_state"].finalizing = True
    request = ModelRequest(
        model=object(), messages=[], tools=[object()], runtime=runtime
    )
    seen = []
    final_call = {
        "name": "get_document",
        "args": {"doc_id": "doc-1"},
        "id": "final-call",
        "type": "tool_call",
    }
    provider_message = AIMessage(
        content="final",
        tool_calls=[final_call],
        additional_kwargs={
            "tool_calls": [{"id": "final-call"}],
            "function_call": {"name": "get_document"},
            "provider_data": "preserved",
        },
        response_metadata={"finish_reason": "tool_calls", "model": "test-model"},
    )

    response = middleware.wrap_model_call(
        request,
        lambda updated: (
            seen.append(updated) or ModelResponse(result=[provider_message])
        ),
    )

    assert seen[0].tools == []
    assert seen[0].tool_choice == "none"
    final_message = response.result[0]
    assert isinstance(final_message, AIMessage)
    assert final_message.tool_calls == []
    assert final_message.content == "基于目前已获得的证据，我无法确认答案。"
    assert "tool_calls" not in final_message.additional_kwargs
    assert "function_call" not in final_message.additional_kwargs
    assert final_message.additional_kwargs["provider_data"] == "preserved"
    assert final_message.response_metadata == {
        "finish_reason": "stop",
        "model": "test-model",
    }
    assert provider_message.tool_calls == [final_call]
    assert runtime.context["loop_guard_state"].finalized is True
    assert middleware.after_model({"messages": response.result}, runtime) == {
        "jump_to": "end"
    }


def test_loop_guard_final_tool_call_fallback_matches_english_request():
    middleware = RetrievalLoopGuardMiddleware()
    guard = LoopGuardState(finalizing=True)
    runtime = SimpleNamespace(context={"loop_guard_state": guard})
    request = ModelRequest(
        model=object(),
        messages=[HumanMessage(content="Find the revenue figure")],
        tools=[object()],
        runtime=runtime,
    )
    call = {
        "name": "get_document",
        "args": {"doc_id": "doc-1"},
        "id": "english-final-call",
        "type": "tool_call",
    }

    response = middleware.wrap_model_call(
        request,
        lambda _updated: ModelResponse(
            result=[AIMessage(content="Let me retrieve it.", tool_calls=[call])]
        ),
    )

    assert response.result[0].content == (
        "I cannot confirm the answer based on the evidence currently available."
    )


def test_loop_guard_async_final_model_call_clears_anthropic_tool_metadata():
    middleware = RetrievalLoopGuardMiddleware()
    guard = LoopGuardState(finalizing=True)
    runtime = SimpleNamespace(context={"loop_guard_state": guard})
    request = ModelRequest(
        model=object(), messages=[], tools=[object()], runtime=runtime
    )
    call = {
        "name": "get_document",
        "args": {"doc_id": "doc-1"},
        "id": "async-final-call",
        "type": "tool_call",
    }

    async def handler(updated):
        assert updated.tools == []
        assert updated.tool_choice == "none"
        return ModelResponse(
            result=[
                AIMessage(
                    content=[
                        {"type": "text", "text": "I will call another tool."},
                        {
                            "type": "tool_use",
                            "id": "async-final-call",
                            "name": "get_document",
                            "input": {"doc_id": "doc-1"},
                        },
                    ],
                    tool_calls=[call],
                    response_metadata={"stop_reason": "tool_use"},
                )
            ]
        )

    response = asyncio.run(middleware.awrap_model_call(request, handler))

    final_message = response.result[0]
    assert isinstance(final_message, AIMessage)
    assert final_message.tool_calls == []
    assert final_message.content == "基于目前已获得的证据，我无法确认答案。"
    assert final_message.response_metadata["stop_reason"] == "end_turn"
    assert guard.finalized is True


def test_loop_guard_blocks_repeated_call_in_compiled_agent():
    @tool("get_structure_outline")
    def get_structure_outline(doc_id: str) -> str:
        """Return an outline for the requested document."""
        return json.dumps({"doc_id": doc_id})

    class ToolCallingModel(GenericFakeChatModel):
        bound_tools: list[list] = []

        def bind_tools(self, tools, **kwargs):
            self.bound_tools.append(list(tools))
            return self

    model = ToolCallingModel(
        messages=iter(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "get_structure_outline",
                            "args": {"doc_id": "doc-1"},
                            "id": f"call-{index}",
                            "type": "tool_call",
                        }
                    ],
                )
                for index in range(3)
            ]
            + [AIMessage(content="final answer")]
        )
    )
    agent = create_agent(
        model=model,
        tools=[get_structure_outline],
        middleware=[RetrievalLoopGuardMiddleware()],
    )

    result = agent.invoke(
        {"messages": [{"role": "user", "content": "go"}]},
        context={"loop_guard_state": LoopGuardState()},
    )

    tool_results = [
        message for message in result["messages"] if isinstance(message, ToolMessage)
    ]
    assert [message.status for message in tool_results] == [
        "success",
        "success",
        "error",
    ]
    assert json.loads(tool_results[-1].content)["error"]["code"] == "loop_detected"
    assert result["messages"][-1].content == "final answer"
    assert len(model.bound_tools) == 3


def test_loop_guard_sanitizes_final_tool_call_in_compiled_stream():
    @tool("get_document")
    def get_document(doc_id: str) -> str:
        """Return metadata for the requested document."""
        return json.dumps({"doc_id": doc_id})

    class ToolCallingModel(GenericFakeChatModel):
        def bind_tools(self, tools, **kwargs):
            return self

    repeated_calls = [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "get_document",
                    "args": {"doc_id": "doc-1"},
                    "id": f"call-{index}",
                    "type": "tool_call",
                }
            ],
        )
        for index in range(3)
    ]
    final_call = {
        "name": "get_document",
        "args": {"doc_id": "doc-2"},
        "id": "final-call",
        "type": "tool_call",
    }
    model = ToolCallingModel(
        messages=iter(
            [
                *repeated_calls,
                AIMessage(
                    content="I will retrieve one more document.",
                    tool_calls=[final_call],
                    additional_kwargs={"tool_calls": [{"id": "final-call"}]},
                    response_metadata={"finish_reason": "tool_calls"},
                ),
            ]
        )
    )
    agent = create_agent(
        model=model,
        tools=[get_document],
        middleware=[RetrievalLoopGuardMiddleware()],
    )

    states = list(
        agent.stream(
            {"messages": [{"role": "user", "content": "go"}]},
            context={"loop_guard_state": LoopGuardState()},
            stream_mode="values",
        )
    )

    final_message = states[-1]["messages"][-1]
    assert isinstance(final_message, AIMessage)
    assert final_message.content == (
        "I cannot confirm the answer based on the evidence currently available."
    )
    assert final_message.tool_calls == []
    assert "tool_calls" not in final_message.additional_kwargs
    assert final_message.response_metadata["finish_reason"] == "stop"
    assert find_missing_tool_results_for_model_tool_calls(states[-1]["messages"]) == ()


def test_loop_guard_default_recursion_limit_covers_production_middleware_graph():
    @tool("get_document")
    def get_document(doc_id: str) -> str:
        """Return metadata for the requested document."""
        return json.dumps({"doc_id": doc_id})

    class ToolCallingModel(GenericFakeChatModel):
        def bind_tools(self, tools, **kwargs):
            return self

    model = ToolCallingModel(
        messages=iter(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "get_document",
                            "args": {"doc_id": f"doc-{index}"},
                            "id": f"call-{index}",
                            "type": "tool_call",
                        }
                    ],
                )
                for index in range(32)
            ]
            + [AIMessage(content="final answer")]
        )
    )
    middleware = RetrievalLoopGuardMiddleware()
    agent = create_agent(
        model=model,
        tools=[get_document],
        middleware=[
            ContextSummarizationMiddleware(model),
            DanglingToolCallMiddleware(),
            ToolErrorHandlingMiddleware(),
            middleware,
            RetrievalDeduplicationMiddleware(),
            ClarificationMiddleware(),
        ],
    )

    result = agent.invoke(
        {"messages": [{"role": "user", "content": "go"}]},
        context={
            "loop_guard_state": LoopGuardState(),
            "retrieval_deduplication_state": {
                "documents": set(),
                "nodes": set(),
            },
            "clarification_enabled": False,
        },
        config={"recursion_limit": middleware.config.recursion_limit},
    )

    assert result["messages"][-1].content == "final answer"


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
