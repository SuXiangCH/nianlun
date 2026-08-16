"""Token-usage extraction and SSE/stream wiring.

Covers the normalized ``usage_metadata`` path (input/output/total + cache_read),
the DeepSeek-style ``prompt_cache_hit_tokens`` fallback, multi-message summing,
and that ``iter_agent_stream_events`` surfaces this-turn usage on the ``done``
event via a fake agent streaming realistic LangGraph chunks.
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from nianlun.agent.lead_agent.runtime import (
    AgentRuntime,
    iter_agent_stream_events,
    run_agent,
)
from nianlun.agent.lead_agent.runner import _last_usage, _message_usage, _sum_usage


def _ai(
    usage_metadata: dict | None = None, token_usage: dict | None = None
) -> AIMessage:
    kwargs: dict[str, Any] = {"content": "ok"}
    if usage_metadata is not None:
        kwargs["usage_metadata"] = usage_metadata
    if token_usage is not None:
        kwargs["response_metadata"] = {"token_usage": token_usage}
    return AIMessage(**kwargs)


def test_message_usage_reads_normalized_cache_read() -> None:
    message = _ai(
        usage_metadata={
            "input_tokens": 100,
            "output_tokens": 50,
            "total_tokens": 150,
            "input_token_details": {"cache_read": 80},
        }
    )
    assert _message_usage(message) == {
        "input_tokens": 100,
        "output_tokens": 50,
        "total_tokens": 150,
        "cached_tokens": 80,
    }


def test_message_usage_falls_back_to_deepseek_prompt_cache_hit() -> None:
    message = _ai(
        usage_metadata={"input_tokens": 100, "output_tokens": 50, "total_tokens": 150},
        token_usage={"prompt_cache_hit_tokens": 64},
    )
    assert _message_usage(message)["cached_tokens"] == 64


def test_message_usage_returns_none_without_usage_metadata() -> None:
    assert _message_usage(AIMessage(content="no usage here")) is None
    assert _message_usage(ToolMessage(content="tool", tool_call_id="t1")) is None


def test_sum_usage_accumulates_and_returns_none_when_empty() -> None:
    assert _sum_usage(None) is None
    assert _sum_usage([AIMessage(content="x")]) is None
    total = _sum_usage(
        [
            _ai(
                usage_metadata={
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "total_tokens": 150,
                    "input_token_details": {"cache_read": 80},
                }
            ),
            _ai(
                usage_metadata={
                    "input_tokens": 200,
                    "output_tokens": 30,
                    "total_tokens": 230,
                }
            ),
        ]
    )
    assert total == {
        "input_tokens": 300,
        "output_tokens": 80,
        "total_tokens": 380,
        "cached_tokens": 80,
    }


def test_last_usage_picks_final_message_with_usage() -> None:
    first = _ai(
        usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}
    )
    last = _ai(
        usage_metadata={"input_tokens": 9, "output_tokens": 9, "total_tokens": 18}
    )
    assert _last_usage([first, AIMessage(content="nope"), last]) == {
        "input_tokens": 9,
        "output_tokens": 9,
        "total_tokens": 18,
        "cached_tokens": 0,
    }


def test_run_agent_sums_all_model_usage_from_the_current_turn() -> None:
    previous = _ai(
        usage_metadata={"input_tokens": 9, "output_tokens": 1, "total_tokens": 10}
    )
    tool_decision = _ai(
        usage_metadata={"input_tokens": 100, "output_tokens": 50, "total_tokens": 150}
    )
    final_answer = _ai(
        usage_metadata={"input_tokens": 200, "output_tokens": 30, "total_tokens": 230}
    )

    class _InvokeAgent:
        def invoke(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            return {
                "messages": [
                    HumanMessage(content="上一轮问题"),
                    previous,
                    HumanMessage(content="请检索文档里的营收数据"),
                    tool_decision,
                    final_answer,
                ]
            }

    runtime = AgentRuntime(
        agent=_InvokeAgent(),
        model="test-model",
        effective_url="",
        tool_logging=False,
        kb=None,
    )

    result = run_agent(runtime, "请检索文档里的营收数据")

    assert result["usage"] == {
        "input_tokens": 300,
        "output_tokens": 80,
        "total_tokens": 380,
        "cached_tokens": 0,
    }


class _FakeAgent:
    """Mimic LangGraph agent.stream enough to drive iter_agent_stream_events."""

    def __init__(self, chunks: list[dict[str, Any]]) -> None:
        self._chunks = chunks

    def stream(self, *args: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
        yield from self._chunks


def _runtime(chunks: list[dict[str, Any]]) -> AgentRuntime:
    return AgentRuntime(
        agent=_FakeAgent(chunks),
        model="test-model",
        effective_url="",
        tool_logging=False,
        kb=None,
    )


def _collect_events(runtime: AgentRuntime, query: str) -> list[dict[str, Any]]:
    return list(iter_agent_stream_events(runtime, query, thread_id="t1"))


def test_iter_agent_stream_events_sums_this_turn_usage_on_done() -> None:
    u1 = {
        "input_tokens": 100,
        "output_tokens": 50,
        "total_tokens": 150,
        "input_token_details": {"cache_read": 80},
    }
    u2 = {"input_tokens": 200, "output_tokens": 30, "total_tokens": 230}
    chunks = [
        # generation 1: model decides to call a tool (usage on the final chunk)
        {
            "type": "messages",
            "data": (
                AIMessage(content="", usage_metadata=u1),
                {"langgraph_node": "model"},
            ),
        },
        # tool node output: no usage, not streamed
        {
            "type": "messages",
            "data": (
                ToolMessage(content="doc", tool_call_id="t1"),
                {"langgraph_node": "tools"},
            ),
        },
        # generation 2: final answer tokens + usage chunk
        {
            "type": "messages",
            "data": (AIMessage(content="hel"), {"langgraph_node": "model"}),
        },
        {
            "type": "messages",
            "data": (AIMessage(content="lo"), {"langgraph_node": "model"}),
        },
        {
            "type": "messages",
            "data": (
                AIMessage(content="", usage_metadata=u2),
                {"langgraph_node": "model"},
            ),
        },
        {
            "type": "values",
            "data": {
                "messages": [
                    HumanMessage(content="请检索文档里的营收数据"),
                    AIMessage(content="hello", usage_metadata=u2),
                ]
            },
        },
    ]
    events = _collect_events(_runtime(chunks), "请检索文档里的营收数据")

    deltas = [e for e in events if e["type"] == "message"]
    assert "".join(e["data"]["delta"] for e in deltas) == "hello"

    done = next(e for e in events if e["type"] == "done")
    assert done["data"]["answer"] == "hello"
    assert done["data"]["usage"] == {
        "input_tokens": 300,
        "output_tokens": 80,
        "total_tokens": 380,
        "cached_tokens": 80,
    }
    # TTFT is measured from stream start to the first streamed token ("hel").
    assert isinstance(done["data"]["ttft_ms"], int)
    assert done["data"]["ttft_ms"] >= 0


def test_iter_agent_stream_events_direct_route_has_no_usage() -> None:
    # "你好" hits the greeting rule -> direct route, agent is never invoked.
    events = _collect_events(_runtime([]), "你好")
    done = next(e for e in events if e["type"] == "done")
    assert done["data"]["route"] == "direct"
    assert done["data"]["usage"] is None
    # Direct route still reports TTFT (time to emit the single answer delta).
    assert isinstance(done["data"]["ttft_ms"], int)
    assert done["data"]["ttft_ms"] >= 0


def test_iter_agent_stream_events_falls_back_to_final_state_when_no_usage_chunk() -> (
    None
):
    # Model chunks never carry usage_metadata, but the final values state does.
    u = {"input_tokens": 42, "output_tokens": 7, "total_tokens": 49}
    chunks = [
        {
            "type": "messages",
            "data": (AIMessage(content="hi"), {"langgraph_node": "model"}),
        },
        {
            "type": "values",
            "data": {
                "messages": [
                    HumanMessage(content="请检索文档"),
                    AIMessage(content="hi", usage_metadata=u),
                ]
            },
        },
    ]
    events = _collect_events(_runtime(chunks), "请检索文档")
    done = next(e for e in events if e["type"] == "done")
    assert done["data"]["usage"] == {
        "input_tokens": 42,
        "output_tokens": 7,
        "total_tokens": 49,
        "cached_tokens": 0,
    }


def test_iter_agent_stream_events_ttft_is_end_to_end_not_first_llm_call(
    monkeypatch,
) -> None:
    # 工具决策轮（generation 1）也会吐出文本（前言/思考外显），但端到端首 token 时延
    # 必须落在最后一次工具调用之后那轮（最终答案）的首个文本 token 上——检索耗时要计入。
    import time as time_module

    ticks = iter([0.0, 1.0, 5.0])  # start / gen1 前言 token / gen2 答案首 token
    monkeypatch.setattr(time_module, "monotonic", lambda: next(ticks, 5.0))

    chunks = [
        # generation 1: 工具决策轮吐了一段前言文本（1.0s 时刻），随后发起工具调用
        {
            "type": "messages",
            "data": (AIMessage(content="我先检索一下"), {"langgraph_node": "model"}),
        },
        # 工具节点执行检索
        {
            "type": "messages",
            "data": (
                ToolMessage(content="doc", tool_call_id="t1"),
                {"langgraph_node": "tools"},
            ),
        },
        # generation 2: 最终答案（5.0s 时刻才出第一个字）
        {
            "type": "messages",
            "data": (AIMessage(content="答案是"), {"langgraph_node": "model"}),
        },
        {
            "type": "values",
            "data": {
                "messages": [
                    HumanMessage(content="请检索文档里的营收数据"),
                    AIMessage(content="答案是"),
                ]
            },
        },
    ]
    events = _collect_events(_runtime(chunks), "请检索文档里的营收数据")

    deltas = [e for e in events if e["type"] == "message"]
    assert "".join(e["data"]["delta"] for e in deltas) == "我先检索一下答案是"

    done = next(e for e in events if e["type"] == "done")
    # 若误取 generation 1 的前言 token，则 ttft=1000；端到端应为 5000（含检索耗时）。
    assert done["data"]["ttft_ms"] == 5000


def test_iter_agent_stream_events_done_includes_recorded_tool_calls() -> None:
    # 工具在执行时把调用记录（含耗时、tool_call_id）写进 context 里的 collector；
    # done 事件应带上，并按 AIMessage.tool_calls 归组标注 batch：
    # call-2/call-3 来自同一条 AIMessage（并行），与单独一轮的 call-1 区分开。
    class _RecordingAgent:
        def stream(self, *args: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
            collector = kwargs["context"]["retrieval_collector"]
            collector.record_tool_call(
                "search_document_nodes",
                {"query": "营收"},
                elapsed_ms=120,
                tool_call_id="call-1",
            )
            collector.record_tool_call(
                "search_document_nodes",
                {"query": "利润"},
                elapsed_ms=98,
                tool_call_id="call-2",
            )
            collector.record_tool_call(
                "get_line_content",
                {"doc_id": "doc-1"},
                elapsed_ms=45,
                tool_call_id="call-3",
            )
            yield {
                "type": "messages",
                "data": (AIMessage(content="答案是"), {"langgraph_node": "model"}),
            }
            yield {
                "type": "values",
                "data": {
                    "messages": [
                        HumanMessage(content="请检索文档里的营收数据"),
                        AIMessage(
                            content="",
                            tool_calls=[
                                {
                                    "name": "search_document_nodes",
                                    "args": {"query": "营收"},
                                    "id": "call-1",
                                }
                            ],
                        ),
                        AIMessage(
                            content="",
                            tool_calls=[
                                {
                                    "name": "search_document_nodes",
                                    "args": {"query": "利润"},
                                    "id": "call-2",
                                },
                                {
                                    "name": "get_line_content",
                                    "args": {"doc_id": "doc-1"},
                                    "id": "call-3",
                                },
                            ],
                        ),
                        AIMessage(content="答案是"),
                    ]
                },
            }

    runtime = AgentRuntime(
        agent=_RecordingAgent(),
        model="test-model",
        effective_url="",
        tool_logging=False,
        kb=None,
    )
    events = _collect_events(runtime, "请检索文档里的营收数据")
    done = next(e for e in events if e["type"] == "done")
    assert done["data"]["tool_calls"] == [
        {
            "name": "search_document_nodes",
            "args": {"query": "营收"},
            "elapsed_ms": 120,
            "tool_call_id": "call-1",
            "batch": 1,
        },
        {
            "name": "search_document_nodes",
            "args": {"query": "利润"},
            "elapsed_ms": 98,
            "tool_call_id": "call-2",
            "batch": 2,
        },
        {
            "name": "get_line_content",
            "args": {"doc_id": "doc-1"},
            "elapsed_ms": 45,
            "tool_call_id": "call-3",
            "batch": 2,
        },
    ]
    assert done["data"]["route"] == "retrieval"


def test_iter_agent_stream_events_ttft_ms_is_none_when_no_tokens_streamed() -> None:
    # The model emits no text deltas (empty answer), so first_token_at is never set
    # and TTFT cannot be measured.
    chunks = [
        {
            "type": "messages",
            "data": (AIMessage(content=""), {"langgraph_node": "model"}),
        },
        {
            "type": "values",
            "data": {
                "messages": [HumanMessage(content="请检索"), AIMessage(content="")]
            },
        },
    ]
    events = _collect_events(_runtime(chunks), "请检索")
    done = next(e for e in events if e["type"] == "done")
    assert done["data"]["ttft_ms"] is None
