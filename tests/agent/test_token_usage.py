"""Token-usage extraction and SSE/stream wiring.

Covers the normalized ``usage_metadata`` path (input/output/total + cache_read),
the DeepSeek-style ``prompt_cache_hit_tokens`` fallback, multi-message summing,
and that ``iter_agent_stream_events`` surfaces this-turn usage on the ``done``
event via a fake agent streaming realistic LangGraph chunks.
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage

from nianlun.agent.lead_agent.runtime import (
    AgentRuntime,
    iter_agent_stream_events,
    run_agent,
    run_agent_streaming,
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
    assert result["guard"] == {
        "triggered": False,
        "trigger": None,
        "model_rounds": 0,
        "tool_rounds": 0,
        "tool_calls": 0,
        "blocked_tool_calls": 0,
        "finalized": False,
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
    assert done["data"]["guard"]["triggered"] is False
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
    # 工具决策轮会先输出自然进度说明；端到端首 token 时延仍必须落在最后一次
    # 工具调用之后那轮最终答案的首个文本 token 上——检索耗时要计入。
    import time as time_module

    ticks = iter([0.0, 1.0, 5.0])  # start / gen1 进度说明 / gen2 答案首 token
    monkeypatch.setattr(time_module, "monotonic", lambda: next(ticks, 5.0))

    chunks = [
        # generation 1: 自然进度说明随后进入同一轮的工具调用。
        {
            "type": "messages",
            "data": (
                AIMessage(content="我先检索相关文档。"),
                {"langgraph_node": "model"},
            ),
        },
        {
            "type": "messages",
            "data": (
                AIMessageChunk(
                    content="",
                    tool_call_chunks=[
                        {
                            "name": "search_document_nodes",
                            "args": "{}",
                            "id": "t1",
                            "index": 0,
                        }
                    ],
                ),
                {"langgraph_node": "model"},
            ),
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
    assert "".join(e["data"]["delta"] for e in deltas) == "我先检索相关文档。答案是"
    assert deltas[0]["data"]["phase"] == "candidate"
    assert deltas[-1]["data"]["round"] == 2
    assert [e["data"] for e in events if e["type"] == "trace"] == [
        {
            "kind": "agent_message",
            "message": "我先检索相关文档。",
            "round": 1,
        },
    ]

    done = next(e for e in events if e["type"] == "done")
    # 若误取 generation 1 的前言 token，则 ttft=1000；端到端应为 5000（含检索耗时）。
    assert done["data"]["ttft_ms"] == 5000


def test_iter_agent_stream_events_promotes_natural_activity_before_tool_runs() -> None:
    chunks = [
        {
            "type": "messages",
            "data": (
                AIMessageChunk(
                    content="我先搜索相关文档。",
                    tool_call_chunks=[
                        {
                            "name": "search_document_nodes",
                            "args": "{}",
                            "id": "t1",
                            "index": 0,
                        }
                    ],
                ),
                {"langgraph_node": "model"},
            ),
        },
    ]

    events = iter_agent_stream_events(_runtime(chunks), "请检索文档", thread_id="t1")
    try:
        assert next(events) == {
            "type": "message",
            "data": {
                "delta": "我先搜索相关文档。",
                "phase": "candidate",
                "round": 1,
            },
        }
        assert next(events) == {
            "type": "trace",
            "data": {
                "kind": "agent_message",
                "message": "我先搜索相关文档。",
                "round": 1,
            },
        }
    finally:
        events.close()


def test_iter_agent_stream_events_waits_for_complete_tool_name() -> None:
    chunks = [
        {
            "type": "messages",
            "data": (
                AIMessageChunk(
                    content="",
                    tool_call_chunks=[
                        {"name": "get_line", "args": "", "id": "t1", "index": 0}
                    ],
                ),
                {"langgraph_node": "model"},
            ),
        },
        {
            "type": "messages",
            "data": (
                AIMessageChunk(
                    content="",
                    tool_call_chunks=[
                        {
                            "name": "get_line_content",
                            "args": "{}",
                            "id": None,
                            "index": 0,
                        }
                    ],
                ),
                {"langgraph_node": "model"},
            ),
        },
        {
            "type": "messages",
            "data": (
                ToolMessage(content="doc", tool_call_id="t1"),
                {"langgraph_node": "tools"},
            ),
        },
        {
            "type": "messages",
            "data": (AIMessage(content="最终答案"), {"langgraph_node": "model"}),
        },
        {
            "type": "values",
            "data": {
                "messages": [
                    HumanMessage(content="请读取正文"),
                    AIMessage(content="最终答案"),
                ]
            },
        },
    ]

    events = _collect_events(_runtime(chunks), "请读取正文")

    assert [event["data"] for event in events if event["type"] == "trace"] == [
        {
            "kind": "status",
            "event": "tool_call_started",
            "message": "正在读取相关内容。",
        }
    ]


def test_iter_agent_stream_events_reads_tool_name_from_model_state() -> None:
    chunks = [
        {
            "type": "messages",
            "data": (AIMessageChunk(content=""), {"langgraph_node": "model"}),
        },
        {
            "type": "values",
            "data": {
                "messages": [
                    HumanMessage(content="请查看目录"),
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "get_structure_outline",
                                "args": {"doc_id": "doc-1"},
                                "id": "t1",
                            }
                        ],
                    ),
                ]
            },
        },
        {
            "type": "messages",
            "data": (
                ToolMessage(content="outline", tool_call_id="t1"),
                {"langgraph_node": "tools"},
            ),
        },
        {
            "type": "messages",
            "data": (AIMessage(content="最终答案"), {"langgraph_node": "model"}),
        },
        {
            "type": "values",
            "data": {
                "messages": [
                    HumanMessage(content="请查看目录"),
                    AIMessage(content="最终答案"),
                ]
            },
        },
    ]

    events = _collect_events(_runtime(chunks), "请查看目录")

    trace_index = next(
        index for index, event in enumerate(events) if event["type"] == "trace"
    )
    answer_index = next(
        index for index, event in enumerate(events) if event["type"] == "message"
    )
    assert events[trace_index]["data"] == {
        "kind": "status",
        "event": "tool_call_started",
        "message": "正在查看文档结构。",
    }
    assert trace_index < answer_index


def test_iter_agent_stream_events_emits_one_trace_for_parallel_tool_messages() -> None:
    chunks = [
        {
            "type": "messages",
            "data": (
                ToolMessage(content="doc-1", tool_call_id="t1"),
                {"langgraph_node": "tools"},
            ),
        },
        {
            "type": "messages",
            "data": (
                ToolMessage(content="doc-2", tool_call_id="t2"),
                {"langgraph_node": "tools"},
            ),
        },
        {
            "type": "messages",
            "data": (AIMessage(content="最终答案"), {"langgraph_node": "model"}),
        },
        {
            "type": "values",
            "data": {
                "messages": [
                    HumanMessage(content="请检索"),
                    AIMessage(content="最终答案"),
                ]
            },
        },
    ]

    events = _collect_events(_runtime(chunks), "请检索")

    assert [event["data"] for event in events if event["type"] == "trace"] == [
        {
            "kind": "status",
            "event": "tool_call_started",
            "message": "正在调用工具。",
        }
    ]


def test_iter_agent_stream_events_done_includes_recorded_tool_calls() -> None:
    # 工具在执行时把调用记录（含耗时、tool_call_id）写进 context 里的 collector；
    # done 事件应带上，并按 AIMessage.tool_calls 归组标注 batch：
    # call-2/call-3 来自同一条 AIMessage（并行），与单独一轮的 call-1 区分开。
    class _RecordingAgent:
        def stream(self, *args: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
            collector = kwargs["context"]["retrieval_collector"]
            kwargs["context"]["status_sink"].emit(
                "context_compaction_completed", "历史上下文整理完成。"
            )
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
    trace_events = [e["data"] for e in events if e["type"] == "trace"]
    assert trace_events[0] == {
        "kind": "status",
        "event": "context_compaction_completed",
        "message": "历史上下文整理完成。",
    }
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
    assert done["data"]["trace"] == trace_events
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


def test_iter_agent_stream_events_drops_whitespace_only_processing_round() -> None:
    chunks = [
        {
            "type": "messages",
            "data": (AIMessage(content=" \n "), {"langgraph_node": "model"}),
        },
        {
            "type": "messages",
            "data": (
                ToolMessage(content="doc", tool_call_id="t1"),
                {"langgraph_node": "tools"},
            ),
        },
        {
            "type": "messages",
            "data": (AIMessage(content="最终答案"), {"langgraph_node": "model"}),
        },
        {
            "type": "values",
            "data": {
                "messages": [
                    HumanMessage(content="请检索"),
                    AIMessage(content="最终答案"),
                ]
            },
        },
    ]

    events = _collect_events(_runtime(chunks), "请检索")

    assert [event["data"] for event in events if event["type"] == "trace"] == [
        {
            "kind": "status",
            "event": "tool_call_started",
            "message": "正在调用工具。",
        }
    ]
    assert [
        event["data"]["delta"] for event in events if event["type"] == "message"
    ] == [" \n ", "最终答案"]


class _GuardFinalStreamAgent:
    """Emit an unsafe provider chunk followed by the guard-sanitized final state."""

    safe_answer = "基于目前已获得的证据，我无法确认答案。"

    def stream(self, *args: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
        guard = kwargs["context"]["loop_guard_state"]
        guard.finalizing = True
        guard.final_model_started = True
        yield {
            "type": "messages",
            "data": (
                AIMessage(content="I will retrieve one more document."),
                {"langgraph_node": "model"},
            ),
        }
        guard.finalized = True
        guard.trigger = "identical_tool_call"
        yield {
            "type": "values",
            "data": {
                "messages": [
                    HumanMessage(content="请检索文档"),
                    AIMessage(content=self.safe_answer),
                ]
            },
        }


def _guard_final_runtime() -> AgentRuntime:
    return AgentRuntime(
        agent=_GuardFinalStreamAgent(),
        model="test-model",
        effective_url="",
        tool_logging=False,
        kb=None,
    )


def test_iter_agent_stream_events_only_emits_sanitized_guard_final_answer() -> None:
    events = _collect_events(_guard_final_runtime(), "请检索文档")

    deltas = "".join(
        event["data"]["delta"] for event in events if event["type"] == "message"
    )
    done = next(event["data"] for event in events if event["type"] == "done")

    assert deltas == _GuardFinalStreamAgent.safe_answer
    assert done["answer"] == _GuardFinalStreamAgent.safe_answer
    assert done["guard"]["finalized"] is True
    assert isinstance(done["ttft_ms"], int)


def test_run_agent_streaming_only_prints_sanitized_guard_final_answer(capsys) -> None:
    result = run_agent_streaming(_guard_final_runtime(), "请检索文档")

    assert capsys.readouterr().out == f"{_GuardFinalStreamAgent.safe_answer}\n"
    assert result["answer"] == _GuardFinalStreamAgent.safe_answer
