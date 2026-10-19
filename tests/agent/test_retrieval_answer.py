from __future__ import annotations

import json
import threading
from typing import Any

import pytest
from langchain.agents import create_agent
from langchain.tools import tool
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, AIMessageChunk
from langgraph.checkpoint.memory import MemorySaver
from pydantic import PrivateAttr

from nianlun.agent.contracts import AgentRequestContext, RetrievedSnippet
from nianlun.agent.lead_agent.runtime import AgentRuntime
from nianlun.agent.lead_agent.factory import AgentRuntimeFactory
from nianlun.agent.middleware import (
    AgentRequestCancelled,
    RequestCancellationMiddleware,
)
from nianlun.agent.retrieval_answer.graph import build_retrieval_answer_graph
from nianlun.agent.retrieval_answer.prompt import (
    ANSWER_OUTPUT_RESERVE_TOKENS,
    RETRIEVAL_OUTPUT_RESERVE_TOKENS,
    build_answer_messages,
    build_retrieval_system_prompt,
)
from nianlun.agent.token_estimation import estimate_tokens


class _KnowledgeBase:
    has_fts = True
    has_vector = False


class _RetrievalAgent:
    def __init__(
        self,
        *,
        clarify: bool = False,
        direct_answer: str | None = None,
        emit_tool_trace: bool = False,
        record_tool_batches: bool = False,
    ) -> None:
        self.clarify = clarify
        self.direct_answer = direct_answer
        self.emit_tool_trace = emit_tool_trace
        self.record_tool_batches = record_tool_batches
        self.inputs: list[Any] = []
        self.clarification = {"question": "请说明年份。"}

    def stream(self, input: Any, *, context: dict[str, Any], **_kwargs: Any):
        self.inputs.append(input)
        if self.clarify:
            context["status_sink"].emit(
                "clarification_requested",
                "需要补充信息。",
                clarification=self.clarification,
            )
        elif self.direct_answer is None:
            context["retrieval_collector"].add_line_content_result(
                json.dumps(
                    {
                        "doc_id": "doc-1",
                        "doc_name": "报告.md",
                        "line_spec": "12",
                        "content": [
                            {
                                "node_id": "node-1",
                                "title": "经营数据",
                                "line_num": 12,
                                "text": "2025 年营收为 100 亿元。",
                            }
                        ],
                    },
                    ensure_ascii=False,
                )
            )
        usage = {
            "input_tokens": 100,
            "output_tokens": 20,
            "total_tokens": 120,
            "input_token_details": {"cache_read": 80},
        }
        if self.emit_tool_trace:
            yield {
                "type": "messages",
                "data": (
                    AIMessageChunk(
                        content="",
                        tool_call_chunks=[
                            {
                                "name": "search_document_nodes",
                                "args": "{}",
                                "id": "tool-1",
                                "index": 0,
                            }
                        ],
                    ),
                    {"langgraph_node": "model"},
                ),
            }
        batch_messages: list[AIMessage] = []
        if self.record_tool_batches:
            collector = context["retrieval_collector"]
            collector.record_tool_call(
                "search_document_nodes", {"query": "营收"}, tool_call_id="call-1"
            )
            collector.record_tool_call(
                "get_line_content", {"doc_id": "doc-1"}, tool_call_id="call-2"
            )
            collector.record_tool_call(
                "get_line_content", {"doc_id": "doc-2"}, tool_call_id="call-3"
            )
            batch_messages = [
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
                            "name": "get_line_content",
                            "args": {"doc_id": "doc-1"},
                            "id": "call-2",
                        },
                        {
                            "name": "get_line_content",
                            "args": {"doc_id": "doc-2"},
                            "id": "call-3",
                        },
                    ],
                ),
            ]
        message = AIMessage(
            content=self.direct_answer or "错误草稿：营收为 20 亿元。",
            usage_metadata=usage,
        )
        yield {
            "type": "messages",
            "data": (message, {"langgraph_node": "model"}),
        }
        yield {
            "type": "values",
            "data": {"messages": [*batch_messages, message]},
        }


class _AnswerModel:
    def __init__(
        self,
        *,
        content: str = "2025 年营收为 100 亿元。[1]",
        finish_reason: str | None = None,
        reasoning_content: str | None = None,
    ) -> None:
        self.inputs: list[Any] = []
        self.content = content
        self.finish_reason = finish_reason
        self.reasoning_content = reasoning_content

    def invoke(self, messages: Any) -> AIMessage:
        self.inputs.append(messages)
        additional_kwargs = (
            {"reasoning_content": self.reasoning_content}
            if self.reasoning_content is not None
            else {}
        )
        response_metadata = (
            {"finish_reason": self.finish_reason}
            if self.finish_reason is not None
            else {}
        )
        return AIMessage(
            content=self.content,
            additional_kwargs=additional_kwargs,
            response_metadata=response_metadata,
            usage_metadata={
                "input_tokens": 50,
                "output_tokens": 10,
                "total_tokens": 60,
                "input_token_details": {"cache_read": 40},
            },
        )


def _runtime(
    *,
    clarify: bool = False,
    direct_answer: str | None = None,
    emit_tool_trace: bool = False,
    record_tool_batches: bool = False,
    answer_content: str = "2025 年营收为 100 亿元。[1]",
    answer_finish_reason: str | None = None,
    answer_reasoning_content: str | None = None,
) -> tuple[AgentRuntime, _AnswerModel, _RetrievalAgent]:
    answer_model = _AnswerModel(
        content=answer_content,
        finish_reason=answer_finish_reason,
        reasoning_content=answer_reasoning_content,
    )
    retrieval_agent = _RetrievalAgent(
        clarify=clarify,
        direct_answer=direct_answer,
        emit_tool_trace=emit_tool_trace,
        record_tool_batches=record_tool_batches,
    )
    graph = build_retrieval_answer_graph(
        retrieval_agent=retrieval_agent,
        answer_model=answer_model,
        recursion_limit=32,
        context_window_tokens=16_000,
    )
    return (
        AgentRuntime(
            agent=graph,
            model="fake",
            effective_url="fake",
            tool_logging=False,
            kb=_KnowledgeBase(),
            agent_version="v1",
        ),
        answer_model,
        retrieval_agent,
    )


def test_v1_answer_receives_snippets_without_retrieval_draft() -> None:
    runtime, answer_model, _retrieval_agent = _runtime()

    result = runtime.invoke("2025 年营收是多少？", thread_id="case-1")

    assert result["answer"] == "2025 年营收为 100 亿元。[1]"
    assert result["answer_input_citation_ids"] == [1]
    assert result["retrieved_snippets"][0]["doc_id"] == "doc-1"
    serialized_input = json.dumps(answer_model.inputs, ensure_ascii=False, default=str)
    assert "2025 年营收为 100 亿元" in serialized_input
    assert "错误草稿" not in serialized_input


def test_v1_clarification_skips_answer_node() -> None:
    runtime, answer_model, _retrieval_agent = _runtime(clarify=True)

    result = runtime.invoke(
        "营收是多少？", thread_id="case-2", clarification_enabled=True
    )

    assert result["answer"] == "请说明年份。"
    assert result["clarification"] == {"question": "请说明年份。"}
    assert answer_model.inputs == []


def test_v1_keeps_visible_conversation_without_retrieval_history() -> None:
    runtime, answer_model, _retrieval_agent = _runtime()

    runtime.invoke("2025 年营收是多少？", thread_id="conversation-1")
    runtime.invoke("这个数字的单位是什么？", thread_id="conversation-1")

    second_input = json.dumps(answer_model.inputs[1], ensure_ascii=False, default=str)
    assert "2025 年营收是多少" in second_input
    assert "2025 年营收为 100 亿元" in second_input
    assert "错误草稿" not in second_input


def test_v1_context_only_follow_up_uses_react_answer_without_retrieval() -> None:
    runtime, answer_model, retrieval_agent = _runtime()
    runtime.invoke("What was the 2025 revenue?", thread_id="direct-follow-up")
    retrieval_agent.direct_answer = "2025 年营收为 100 亿元。"

    result = runtime.invoke("将上述回答翻译成中文", thread_id="direct-follow-up")

    assert result["answer"] == "2025 年营收为 100 亿元。"
    assert result["route"] == "direct"
    assert result["retrieved_snippets"] == []
    assert result["tool_calls"] == []
    assert result["usage_by_stage"]["retrieval"] is not None
    assert result["usage_by_stage"]["answer"] is None
    assert len(answer_model.inputs) == 1
    follow_up = json.dumps(retrieval_agent.inputs[1], ensure_ascii=False, default=str)
    assert "What was the 2025 revenue?" in follow_up
    assert "2025 年营收为 100 亿元。[1]" in follow_up


def test_v1_stream_emits_context_only_react_answer() -> None:
    runtime, answer_model, _retrieval_agent = _runtime(
        direct_answer="2025 年营收为 100 亿元。"
    )

    events = list(
        runtime.iter_events("将上述回答翻译成中文", thread_id="direct-stream")
    )

    assert (
        "".join(
            str(event["data"]["delta"])
            for event in events
            if event["type"] == "message"
        )
        == "2025 年营收为 100 亿元。"
    )
    assert events[-1]["type"] == "done"
    assert events[-1]["data"]["route"] == "direct"
    assert answer_model.inputs == []


def test_v1_stream_only_emits_answer_text() -> None:
    runtime, _answer_model, _retrieval_agent = _runtime()

    events = list(runtime.iter_events("2025 年营收是多少？", thread_id="stream-1"))

    deltas = "".join(
        str(event["data"]["delta"]) for event in events if event["type"] == "message"
    )
    assert deltas == "2025 年营收为 100 亿元。[1]"
    assert "错误草稿" not in deltas
    assert [event["type"] for event in events].count("done") == 1


@pytest.mark.parametrize("public", [False, True])
def test_v1_answer_chunks_append_before_model_finishes(public: bool) -> None:
    class PausedAnswerModel(GenericFakeChatModel):
        _release: threading.Event = PrivateAttr(default_factory=threading.Event)
        _finished: threading.Event = PrivateAttr(default_factory=threading.Event)

        def _stream(self, *args: Any, **kwargs: Any):
            for index, chunk in enumerate(super()._stream(*args, **kwargs)):
                yield chunk
                if index == 0 and not self._release.wait(timeout=5):
                    raise RuntimeError("test did not release answer stream")
            self._finished.set()

    model = PausedAnswerModel(messages=iter([AIMessage(content="Revenue is 100 [1].")]))
    graph = build_retrieval_answer_graph(
        retrieval_agent=_RetrievalAgent(),
        answer_model=model,
        recursion_limit=32,
        context_window_tokens=16_000,
    )
    runtime = AgentRuntime(
        agent=graph,
        model="fake",
        effective_url="fake",
        tool_logging=False,
        kb=_KnowledgeBase(),
        agent_version="v1",
    )
    events = (
        runtime.iter_public_events(
            [{"role": "user", "content": "What was the revenue?"}],
            thread_id="live-answer",
        )
        if public
        else runtime.iter_events("What was the revenue?", thread_id="live-answer")
    )
    try:
        first = next(events)
        assert first["type"] == "message"
        assert first["data"]["delta"] == "Revenue"
        assert not model._finished.is_set()
    finally:
        model._release.set()
    remaining = list(events)
    chunks = [first, *[event for event in remaining if event["type"] == "message"]]
    assert len(chunks) > 1
    if not public:
        assert all(event["data"]["phase"] == "candidate" for event in chunks)
        assert {event["data"]["round"] for event in chunks} == {1}
    assert "".join(event["data"]["delta"] for event in chunks) == "Revenue is 100 [1]."
    assert remaining[-1]["type"] == "done"
    assert remaining[-1]["data"]["answer"] == "Revenue is 100 [1]."


def test_v1_clarification_context_is_used_by_follow_up_retrieval() -> None:
    runtime, _answer_model, retrieval_agent = _runtime(clarify=True)

    runtime.invoke(
        "公司的营收是多少？",
        thread_id="clarification-context",
        clarification_enabled=True,
    )
    retrieval_agent.clarify = False
    runtime.invoke("2025 年", thread_id="clarification-context")

    follow_up = json.dumps(retrieval_agent.inputs[1], ensure_ascii=False, default=str)
    assert "公司的营收是多少" in follow_up
    assert "请说明年份" in follow_up
    assert "2025 年" in follow_up


def test_v1_clarification_history_keeps_visible_context_and_options() -> None:
    runtime, _answer_model, retrieval_agent = _runtime(clarify=True)
    retrieval_agent.clarification = {
        "question": "请选择一种口径",
        "context": "营收可以按不同报表口径统计",
        "options": ["合并报表", "母公司报表"],
    }

    runtime.invoke(
        "公司的营收是多少？",
        thread_id="clarification-options",
        clarification_enabled=True,
    )
    retrieval_agent.clarify = False
    runtime.invoke("选第一个", thread_id="clarification-options")

    follow_up = json.dumps(retrieval_agent.inputs[1], ensure_ascii=False, default=str)
    assert "营收可以按不同报表口径统计" in follow_up
    assert "请选择一种口径" in follow_up
    assert "1. 合并报表" in follow_up
    assert "2. 母公司报表" in follow_up
    assert "选第一个" in follow_up


def test_v1_clarification_clears_previous_answer_metadata() -> None:
    runtime, _answer_model, retrieval_agent = _runtime()

    answered = runtime.invoke("2025 年营收是多少？", thread_id="clear-answer")
    assert answered["answer_input_citation_ids"] == [1]
    assert answered["usage_by_stage"]["answer"] is not None

    retrieval_agent.clarify = True
    clarified = runtime.invoke(
        "营收是多少？", thread_id="clear-answer", clarification_enabled=True
    )

    assert clarified["answer_input_citation_ids"] == []
    assert clarified["omitted_citation_ids"] == []
    assert clarified["usage_by_stage"]["answer"] is None


def test_v1_stream_emits_retrieval_trace_before_answer() -> None:
    runtime, _answer_model, _retrieval_agent = _runtime(emit_tool_trace=True)

    events = list(runtime.iter_events("2025 年营收是多少？", thread_id="trace-1"))

    trace_index = next(i for i, event in enumerate(events) if event["type"] == "trace")
    answer_index = next(
        i for i, event in enumerate(events) if event["type"] == "message"
    )
    assert trace_index < answer_index
    assert events[trace_index]["data"] == {
        "kind": "status",
        "event": "tool_call_started",
        "message": "正在搜索相关文档。",
    }


def test_v1_usage_preserves_cached_tokens_for_each_stage() -> None:
    runtime, _answer_model, _retrieval_agent = _runtime()

    result = runtime.invoke("2025 年营收是多少？", thread_id="usage-1")

    assert result["usage_by_stage"]["retrieval"]["cached_tokens"] == 80
    assert result["usage_by_stage"]["answer"]["cached_tokens"] == 40
    assert result["usage"]["cached_tokens"] == 120


def test_v1_long_answer_history_is_trimmed_and_later_turns_recover() -> None:
    long_answer = f"{'营收数据。' * 700}[1]"
    runtime, answer_model, _retrieval_agent = _runtime(answer_content=long_answer)

    for turn in range(5):
        result = runtime.invoke(
            f"第 {turn + 1} 轮营收是多少？", thread_id="long-conversation"
        )
        assert result["answer"] == long_answer

    assert len(answer_model.inputs) == 5
    assert all(
        estimate_tokens(messages) + ANSWER_OUTPUT_RESERVE_TOKENS <= 16_000
        for messages in answer_model.inputs
    )
    final_retrieval_messages = _retrieval_agent.inputs[-1]["messages"]
    assert final_retrieval_messages[-1] == {
        "role": "user",
        "content": "本轮问题：\n第 5 轮营收是多少？",
    }
    assert (
        estimate_tokens(final_retrieval_messages) + RETRIEVAL_OUTPUT_RESERVE_TOKENS
        <= 16_000
    )


def test_v1_tool_calls_keep_model_round_batches() -> None:
    runtime, _answer_model, _retrieval_agent = _runtime(record_tool_batches=True)

    result = runtime.invoke("请检索营收。", thread_id="tool-batches")

    assert [call["batch"] for call in result["tool_calls"]] == [1, 2, 2]


def test_v1_sync_rejects_empty_answer_content() -> None:
    runtime, _answer_model, _retrieval_agent = _runtime(
        answer_content="", answer_finish_reason="length"
    )

    with pytest.raises(
        RuntimeError, match="回答模型未返回可见正文.*finish_reason=length"
    ):
        runtime.invoke("2025 年营收是多少？", thread_id="empty-sync")


def test_v1_stream_rejects_empty_answer_without_done_event() -> None:
    runtime, _answer_model, _retrieval_agent = _runtime(
        answer_content="", answer_finish_reason="length"
    )
    events = runtime.iter_events("2025 年营收是多少？", thread_id="empty-stream")

    with pytest.raises(RuntimeError, match="回答模型未返回可见正文"):
        list(events)


def test_v1_rejects_reasoning_only_answer() -> None:
    runtime, _answer_model, _retrieval_agent = _runtime(
        answer_content="",
        answer_finish_reason="length",
        answer_reasoning_content="内部推理内容",
    )

    with pytest.raises(RuntimeError, match="仅返回 reasoning_content"):
        runtime.invoke("2025 年营收是多少？", thread_id="reasoning-only")


def test_v1_citation_validation_ignores_markdown_code_and_links() -> None:
    answer = (
        "使用 `values[0]` 读取第一项，公式为 $x[0]$。[1]\n\n"
        "```html\n<table><tr><td>values[0]</td></tr></table>\n```\n\n"
        "详情见[示例 [3]](https://example.invalid/report)。"
    )
    runtime, _answer_model, _retrieval_agent = _runtime(answer_content=answer)

    result = runtime.invoke("如何读取第一项？", thread_id="markdown-citations")

    assert result["answer"] == answer


def test_v1_citation_validation_checks_html_table_text() -> None:
    answer = "<table><tr><td>未经支持的结论 [999]</td></tr></table> 说明见 [1]。"
    runtime, _answer_model, _retrieval_agent = _runtime(answer_content=answer)

    with pytest.raises(ValueError, match=r"回答引用了未进入上下文的证据: \[999\]"):
        runtime.invoke("结论是什么？", thread_id="html-table-citations")


def test_v1_citation_validation_ignores_multiline_display_math() -> None:
    answer = "$$\nx[0]\n\ny[2]\n$$\n\n说明见 [1]。"
    runtime, _answer_model, _retrieval_agent = _runtime(answer_content=answer)

    result = runtime.invoke("公式是什么？", thread_id="display-math-citations")

    assert result["answer"] == answer


def test_v1_citation_validation_checks_unmatched_double_dollar_text() -> None:
    runtime, _answer_model, _retrieval_agent = _runtime(
        answer_content="未闭合公式 $$x[999]$，说明见 [1]。"
    )

    with pytest.raises(ValueError, match=r"回答引用了未进入上下文的证据: \[999\]"):
        runtime.invoke("公式是什么？", thread_id="unmatched-math-citations")


def test_v1_citation_validation_keeps_math_within_gfm_table_cells() -> None:
    answer = (
        "| 价格 | 说明 |\n| --- | --- |\n| $100 | 未支持 [999] $200 |\n\n说明见 [1]。"
    )
    runtime, _answer_model, _retrieval_agent = _runtime(answer_content=answer)

    with pytest.raises(ValueError, match=r"回答引用了未进入上下文的证据: \[999\]"):
        runtime.invoke("价格是什么？", thread_id="gfm-table-citations")


def test_v1_citation_validation_handles_implicit_html_cell_closing() -> None:
    answer = "<table><tr><td>营收<td>100 [999]</table>\n\n说明见 [1]。"
    runtime, _answer_model, _retrieval_agent = _runtime(answer_content=answer)

    with pytest.raises(ValueError, match=r"回答引用了未进入上下文的证据: \[999\]"):
        runtime.invoke("营收是什么？", thread_id="implicit-html-cell-citations")


def test_v1_stream_close_stops_before_next_retrieval_model_call() -> None:
    @tool
    def search_document_nodes(query: str) -> str:
        """Search the local test documents."""
        return f"result for {query}"

    class CountingToolModel(GenericFakeChatModel):
        _calls: int = PrivateAttr(default=0)

        def bind_tools(self, _tools: Any, **_kwargs: Any) -> "CountingToolModel":
            return self

        def _generate(self, *args: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
            self._calls += 1
            return super()._generate(*args, **kwargs)

    retrieval_model = CountingToolModel(
        messages=iter(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "search_document_nodes",
                            "args": {"query": str(index)},
                            "id": f"call-{index}",
                            "type": "tool_call",
                        }
                    ],
                )
                for index in range(5)
            ]
            + [AIMessage(content="retrieval complete")]
        ),
        disable_streaming=True,
    )
    retrieval_agent = create_agent(
        retrieval_model,
        tools=[search_document_nodes],
        checkpointer=MemorySaver(),
        context_schema=AgentRequestContext,
        middleware=[RequestCancellationMiddleware()],
    )
    outer_graph = build_retrieval_answer_graph(
        retrieval_agent=retrieval_agent,
        answer_model=_AnswerModel(),
        recursion_limit=32,
        context_window_tokens=16_000,
    )
    runtime = AgentRuntime(
        agent=outer_graph,
        model="fake",
        effective_url="fake",
        tool_logging=False,
        kb=_KnowledgeBase(),
        agent_version="v1",
    )

    events = runtime.iter_events("请检索营收。", thread_id="cancel-v1")
    try:
        assert next(events)["type"] == "trace"
    finally:
        events.close()

    assert retrieval_model._calls == 1


def test_v1_cancellation_before_answer_skips_answer_model() -> None:
    cancellation_event = threading.Event()

    class CancellingRetrievalAgent(_RetrievalAgent):
        def stream(self, input: Any, *, context: dict[str, Any], **kwargs: Any):
            yield from super().stream(input, context=context, **kwargs)
            cancellation_event.set()

    answer_model = _AnswerModel()
    graph = build_retrieval_answer_graph(
        retrieval_agent=CancellingRetrievalAgent(),
        answer_model=answer_model,
        recursion_limit=32,
        context_window_tokens=16_000,
    )
    runtime = AgentRuntime(
        agent=graph,
        model="fake",
        effective_url="fake",
        tool_logging=False,
        kb=_KnowledgeBase(),
        agent_version="v1",
    )

    with pytest.raises(AgentRequestCancelled):
        list(
            runtime.iter_events(
                "请检索营收。",
                thread_id="cancel-before-answer",
                cancellation_event=cancellation_event,
            )
        )

    assert answer_model.inputs == []


def test_answer_budget_counts_the_final_indented_evidence_payload() -> None:
    snippets: list[RetrievedSnippet] = [
        {
            "citation_id": citation_id,
            "doc_id": f"doc-{citation_id}",
            "doc_name": "季度报告.md",
            "line_spec": str(citation_id),
            "node_id": f"node-{citation_id}",
            "title": "经营数据",
            "line_num": citation_id,
            "text": f"第 {citation_id} 条营收数据：{'增长稳定。' * 8}",
            "char_offset": None,
            "char_limit": None,
            "total_chars": None,
            "text_truncated": False,
        }
        for citation_id in range(1, 141)
    ]

    messages, selected_ids, omitted_ids = build_answer_messages(
        "请总结全部营收数据。",
        snippets,
        context_window_tokens=16_000,
        conversation_context=[],
    )

    assert estimate_tokens(messages) + ANSWER_OUTPUT_RESERVE_TOKENS <= 16_000
    assert selected_ids
    assert omitted_ids
    assert selected_ids + omitted_ids == list(range(1, 141))


@pytest.mark.parametrize("agent_version", ["v0", "v1"])
def test_factory_selects_stage_prompt_and_graph(monkeypatch, agent_version) -> None:
    import nianlun.agent.lead_agent.factory as factory_module

    calls: list[dict[str, Any]] = []
    inner_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        factory_module, "build_chat_model", lambda *_args, **_kwargs: object()
    )
    monkeypatch.setattr(factory_module, "build_tools", lambda **_kwargs: [])
    monkeypatch.setattr(factory_module, "build_system_prompt", lambda _kb: "base")

    def build_inner(*_args: Any, **kwargs: Any) -> str:
        inner_calls.append(kwargs)
        return "inner"

    monkeypatch.setattr(factory_module, "build_agent", build_inner)

    def build_outer(**kwargs: Any) -> str:
        calls.append(kwargs)
        return "outer"

    monkeypatch.setattr(factory_module, "build_retrieval_answer_graph", build_outer)

    runtime = AgentRuntimeFactory(
        knowledge_base=_KnowledgeBase(),
        model="fake",
        api_key="fake",
        allow_env_fallback=False,
        agent_version=agent_version,
    ).create()

    assert len(inner_calls) == 1
    if agent_version == "v0":
        assert runtime.agent == "inner"
        assert runtime.agent_version == "v0"
        assert inner_calls[0]["system_prompt"] == "base"
        assert not calls
        return

    assert runtime.agent == "outer"
    assert runtime.agent_version == "v1"
    assert len(calls) == 1
    assert calls[0]["retrieval_agent"] == "inner"
    assert inner_calls[0]["system_prompt"] == build_retrieval_system_prompt(
        _KnowledgeBase()
    )
    assert not inner_calls[0]["system_prompt"].startswith("base")
    assert isinstance(inner_calls[0]["middleware"][0], RequestCancellationMiddleware)
