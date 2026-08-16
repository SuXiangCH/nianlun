"""LangChain Agent 运行时：状态收集、单轮问答执行和流式输出。

包含：
- ``RetrievalCollector``：收集单次问答过程中 get_line_content 返回的正文片段与工具调用。
- ``AgentRunner``：执行单轮问答、流式输出，并为每次请求创建隔离上下文。
- content 归一化辅助：兼容 LangChain 1.x 的 message / content_blocks 结构。
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from collections.abc import Iterator
from typing import Any

from nianlun.models.llm import content_to_text
from nianlun.agent.contracts import AgentRequestContext, KnowledgeBasePort
from nianlun.knowledgebase import sanitize_text
from nianlun.agent.middleware import CONTEXT_SUMMARIZATION_NO_STREAM_TAG
from nianlun.agent.lead_agent.routing import maybe_handle_non_retrieval_query


# ============ 检索状态收集 ============


@dataclass
class RetrievalCollector:
    """收集单次问答过程中 get_line_content 返回的正文片段。"""

    snippets: list[dict[str, Any]] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    _citation_ids: dict[tuple[Any, ...], int] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def clear(self) -> None:
        with self._lock:
            self.snippets.clear()
            self.tool_calls.clear()
            self._citation_ids.clear()

    def record_tool_call(
        self,
        name: str,
        args: dict[str, Any],
        elapsed_ms: int | None = None,
        tool_call_id: str | None = None,
    ) -> None:
        """记录本轮调用过的工具及耗时；``tool_call_id`` 用于事后归组并行调用。"""
        with self._lock:
            self.tool_calls.append(
                {
                    "name": name,
                    "args": dict(args),
                    "elapsed_ms": elapsed_ms,
                    "tool_call_id": tool_call_id,
                }
            )

    def add_line_content_result(self, result: str) -> str:
        """收集正文片段，并将稳定的引用编号写回工具结果。"""
        try:
            payload = json.loads(result)
        except json.JSONDecodeError:
            return result

        if not isinstance(payload, dict) or payload.get("error"):
            return result

        doc_id = payload.get("doc_id")
        doc_name = payload.get("doc_name")
        line_spec = payload.get("line_spec")
        content_items = payload.get("content", [])
        if not isinstance(content_items, list):
            return result

        with self._lock:
            for item in content_items:
                if not isinstance(item, dict):
                    continue

                text = sanitize_text(str(item.get("text", ""))).strip()
                if not text:
                    continue

                key = (
                    str(doc_id),
                    item.get("node_id"),
                    item.get("line_num"),
                    item.get("char_offset"),
                    text,
                )
                citation_id = self._citation_ids.get(key)
                if citation_id is None:
                    citation_id = len(self.snippets) + 1
                    self._citation_ids[key] = citation_id
                    self.snippets.append(
                        {
                            "citation_id": citation_id,
                            "doc_id": doc_id,
                            "doc_name": doc_name,
                            "line_spec": line_spec,
                            "node_id": item.get("node_id"),
                            "title": sanitize_text(str(item.get("title", ""))),
                            "line_num": item.get("line_num"),
                            "text": text,
                            "char_offset": item.get("char_offset"),
                            "char_limit": item.get("char_limit"),
                            "total_chars": item.get("total_chars"),
                            "text_truncated": bool(item.get("text_truncated", False)),
                        }
                    )
                item["citation_id"] = citation_id

        return json.dumps(payload, ensure_ascii=False, indent=2)

    @property
    def texts(self) -> list[str]:
        """仅返回片段文本列表，便于下游直接消费。"""
        return [item["text"] for item in self.snippets]


@dataclass
class AgentStatusSink:
    """收集 Agent 状态事件，并可在交互模式下即时打印。"""

    print_to_stdout: bool = False
    events: list[dict[str, Any]] = field(default_factory=list)

    def emit(self, event: str, message: str, **details: Any) -> None:
        payload = {"event": event, "message": message, **details}
        self.events.append(payload)
        if self.print_to_stdout:
            print(f"[状态] {message}", flush=True)


# ============ 运行时对象 ============


@dataclass(frozen=True, slots=True)
class AgentRequestContextFactory:
    """为每次执行创建隔离的 ToolRuntime context。"""

    knowledge_base: KnowledgeBasePort
    tool_logging: bool

    def create(
        self,
        status_sink: AgentStatusSink | None = None,
        *,
        clarification_enabled: bool = False,
    ) -> tuple[RetrievalCollector, AgentRequestContext]:
        collector = RetrievalCollector()
        context: AgentRequestContext = {
            "knowledge_base": self.knowledge_base,
            "retrieval_collector": collector,
            "tool_logging": self.tool_logging,
            "clarification_enabled": clarification_enabled,
            "retrieval_deduplication_state": {"documents": set(), "nodes": set()},
        }
        if status_sink is not None:
            context["status_sink"] = status_sink
        return collector, context


@dataclass(frozen=True, slots=True)
class AgentRunner:
    """执行 compiled agent，并为每次调用创建独立请求上下文。"""

    agent: Any
    context_factory: AgentRequestContextFactory

    def new_request_context(
        self,
        status_sink: AgentStatusSink | None = None,
        *,
        clarification_enabled: bool = False,
    ) -> tuple[RetrievalCollector, AgentRequestContext]:
        return self.context_factory.create(
            status_sink,
            clarification_enabled=clarification_enabled,
        )

    def invoke(
        self,
        user_query: str,
        thread_id: str = "default",
        *,
        clarification_enabled: bool = False,
    ) -> dict[str, Any]:
        return _run_agent_impl(
            self,
            user_query,
            thread_id=thread_id,
            clarification_enabled=clarification_enabled,
        )

    def stream_to_stdout(
        self, user_query: str, thread_id: str = "default"
    ) -> dict[str, Any]:
        return _run_agent_streaming_impl(self, user_query, thread_id=thread_id)

    def iter_events(
        self,
        user_query: str,
        thread_id: str = "default",
        *,
        clarification_enabled: bool = False,
    ) -> Iterator[dict[str, Any]]:
        return _iter_agent_stream_events_impl(
            self,
            user_query,
            thread_id=thread_id,
            clarification_enabled=clarification_enabled,
        )


# ============ content 归一化辅助 ============
#
# content 文本提取统一走 ``models.llm.content_to_text``（兼容 str / list /
# message 对象的 .content / .content_blocks）。此处仅补充思考模型的
# ``reasoning_content`` 提取（agent 专用，未与索引侧重复）。


def _message_reasoning_text(message: Any) -> str:
    """提取思考模型的 reasoning_content（部分后端把输出放在这里而非 content）。"""
    if isinstance(message, dict):
        kwargs = message.get("additional_kwargs", {}) or {}
    else:
        kwargs = getattr(message, "additional_kwargs", None) or {}
    if not isinstance(kwargs, dict):
        return ""
    reasoning = kwargs.get("reasoning_content")
    return reasoning.strip() if isinstance(reasoning, str) else ""


def _extract_final_answer(result: Any) -> str:
    """从 LangChain agent invoke 的返回值中提取最终文本回答。"""
    if isinstance(result, str):
        return sanitize_text(result)

    if isinstance(result, dict):
        messages = result.get("messages", [])
        for message in reversed(messages):
            if isinstance(message, dict):
                role = message.get("type") or message.get("role")
                content = message.get("content")
            else:
                role = getattr(message, "type", None) or getattr(message, "role", None)
                content = getattr(message, "content", None)

            if role in {"ai", "assistant"}:
                text = content_to_text(
                    getattr(message, "content_blocks", None)
                ) or content_to_text(content)
                if text:
                    return sanitize_text(text)

                # content 为空时兜底 reasoning_content（思考模型 output 全进推理通道的场景）
                reasoning = _message_reasoning_text(message)
                if reasoning:
                    return sanitize_text(reasoning)

    # 模型返回空回答时给出明确说明，而不是把原始 messages 历史倾倒给用户。
    return "（模型未返回可见回答内容——可能是思考输出耗尽未产出正文，请重试或切换模型）"


def _stream_message_text(message: Any) -> str:
    """提取流式消息事件中的文本增量。"""
    text_attr = getattr(message, "text", None)
    if isinstance(text_attr, str) and text_attr:
        return sanitize_text(text_attr)

    text = content_to_text(message)  # content_blocks 优先 -> content
    if text:
        return sanitize_text(text)

    return ""


# ============ token usage 提取 ============
#
# LangChain 把各提供商的 ``usage`` 归一成 ``AIMessage.usage_metadata``：
# ``input_tokens`` / ``output_tokens`` / ``total_tokens``，缓存命中在
# ``input_token_details.cache_read``（对应 OpenAI/GLM 的
# ``prompt_tokens_details.cached_tokens``）。DeepSeek 把缓存命中放在非标准的
# ``token_usage.prompt_cache_hit_tokens``，归一层不映射，这里从
# ``response_metadata.token_usage`` 兜底。流式聚合时每轮模型生成的最终 chunk
# 携带完整 usage，逐条累加即得本轮用量。


def _message_usage(message: Any) -> dict[str, int] | None:
    """单条 AI 消息的归一 token 用量；无 ``usage_metadata`` 时返回 ``None``。"""
    um = getattr(message, "usage_metadata", None)
    if not isinstance(um, dict):
        return None
    cached = 0
    details = um.get("input_token_details") or {}
    if isinstance(details, dict):
        cached = int(details.get("cache_read") or 0)
    if not cached:
        response_metadata = getattr(message, "response_metadata", None) or {}
        token_usage = response_metadata.get("token_usage") or {}
        if isinstance(token_usage, dict):
            cached = int(
                token_usage.get("prompt_cache_hit_tokens")
                or token_usage.get("cached_tokens")
                or token_usage.get("cache_read_input_tokens")
                or 0
            )
    return {
        "input_tokens": int(um.get("input_tokens") or 0),
        "output_tokens": int(um.get("output_tokens") or 0),
        "total_tokens": int(um.get("total_tokens") or 0),
        "cached_tokens": cached,
    }


def _empty_usage() -> dict[str, int]:
    return {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "cached_tokens": 0,
    }


def _sum_usage(messages: Any) -> dict[str, int] | None:
    """累加给定消息的 usage；全无 ``usage_metadata`` 时返回 ``None``。"""
    if not isinstance(messages, (list, tuple)):
        return None
    totals = _empty_usage()
    seen = False
    for message in messages:
        usage = _message_usage(message)
        if not usage:
            continue
        seen = True
        for key in totals:
            totals[key] += usage[key]
    return totals if seen else None


def _last_usage(messages: Any) -> dict[str, int] | None:
    """最后一条带 ``usage_metadata`` 的消息用量（兜底：最终回答那一轮）。"""
    if not isinstance(messages, (list, tuple)):
        return None
    for message in reversed(messages):
        usage = _message_usage(message)
        if usage:
            return usage
    return None


def _usage_for_current_turn(messages: Any) -> dict[str, int] | None:
    """累计当前用户消息触发的所有模型调用用量。

    ``invoke`` 的结果会包含 checkpointer 还原的完整会话历史，不能直接求和。
    当前 run 的消息从最后一条真实用户消息之后开始；上下文摘要使用命名的
    ``HumanMessage``，位于该用户消息之前，因此不会混入本轮统计。
    """
    if not isinstance(messages, (list, tuple)):
        return None
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if isinstance(message, dict):
            role = message.get("type") or message.get("role")
        else:
            role = getattr(message, "type", None) or getattr(message, "role", None)
        if role in {"human", "user"}:
            return _sum_usage(messages[index + 1 :])
    return _last_usage(messages)


# ============ 单轮问答执行 ============


def _run_agent_impl(
    runner: AgentRunner,
    user_query: str,
    thread_id: str = "default",
    *,
    clarification_enabled: bool = False,
) -> dict[str, Any]:
    """运行 LangChain agent。

    多轮上下文由 agent 内置 checkpointer 按 ``thread_id`` 自动维护：每次只传当前
    用户消息，历史由 checkpointer 按 thread_id 加载/保存。交互模式用会话级 thread_id；
    批量模式每题传独立 thread_id 隔离。
    """
    agent = runner.agent
    status_sink = AgentStatusSink()
    retrieval_collector, context = runner.new_request_context(
        status_sink, clarification_enabled=clarification_enabled
    )

    route_decision = maybe_handle_non_retrieval_query(user_query)
    if route_decision["route"] == "direct":
        return {
            "answer": route_decision["answer"],
            "retrieved_texts": [],
            "retrieved_snippets": [],
            "tool_calls": [],
            "route": "direct",
            "route_source": route_decision["route_source"],
            "route_reason": route_decision["route_reason"],
            "status_events": list(status_sink.events),
            "usage": None,
        }

    messages = [{"role": "user", "content": user_query}]
    result = agent.invoke(
        {"messages": messages},
        config={"configurable": {"thread_id": thread_id}},
        context=context,
    )
    used_retrieval = (
        retrieval_collector is not None and len(retrieval_collector.tool_calls) > 0
    )
    if retrieval_collector is not None:
        _annotate_tool_call_batches(
            retrieval_collector.tool_calls,
            result.get("messages") if isinstance(result, dict) else None,
        )
    clarification = next(
        (
            event.get("clarification")
            for event in status_sink.events
            if event.get("event") == "clarification_requested"
            and isinstance(event.get("clarification"), dict)
        ),
        None,
    )
    return {
        "answer": (
            str(clarification.get("question", "请补充必要信息。"))
            if clarification is not None
            else _extract_final_answer(result)
        ),
        "retrieved_texts": []
        if retrieval_collector is None
        else list(retrieval_collector.texts),
        "retrieved_snippets": []
        if retrieval_collector is None
        else list(retrieval_collector.snippets),
        "tool_calls": []
        if retrieval_collector is None
        else list(retrieval_collector.tool_calls),
        "route": "retrieval" if used_retrieval else "direct",
        "route_source": "agent",
        "route_reason": "主 agent 判断需要检索并调用了工具。"
        if used_retrieval
        else "主 agent 直接回答，未调用知识库工具。",
        "status_events": list(status_sink.events),
        "usage": _usage_for_current_turn(
            result.get("messages") if isinstance(result, dict) else None
        ),
        "clarification": clarification,
    }


def _run_agent_streaming_impl(
    runner: AgentRunner,
    user_query: str,
    thread_id: str = "default",
) -> dict[str, Any]:
    """运行 LangChain agent，并在交互模式下流式输出最终回答。

    ``thread_id`` 同 ``run_agent``，用于多轮上下文（checkpointer 按 thread_id 维护）。
    """
    agent = runner.agent
    status_sink = AgentStatusSink(print_to_stdout=True)
    retrieval_collector, context = runner.new_request_context(status_sink)

    route_decision = maybe_handle_non_retrieval_query(user_query)
    if route_decision["route"] == "direct":
        print(route_decision["answer"])
        return {
            "answer": route_decision["answer"],
            "retrieved_texts": [],
            "retrieved_snippets": [],
            "tool_calls": [],
            "route": "direct",
            "route_source": route_decision["route_source"],
            "route_reason": route_decision["route_reason"],
            "status_events": list(status_sink.events),
            "usage": None,
        }

    final_state: dict[str, Any] | None = None
    streamed_any_text = False

    messages = [{"role": "user", "content": user_query}]
    for chunk in agent.stream(
        {"messages": messages},
        config={"configurable": {"thread_id": thread_id}},
        context=context,
        stream_mode=["messages", "values"],
        version="v2",
    ):
        chunk_type = chunk.get("type")
        if chunk_type == "messages":
            message, metadata = chunk["data"]
            if metadata.get("langgraph_node") != "model":
                continue
            if CONTEXT_SUMMARIZATION_NO_STREAM_TAG in metadata.get("tags", ()):
                continue

            text = _stream_message_text(message)
            if text:
                print(text, end="", flush=True)
                streamed_any_text = True
        elif chunk_type == "values":
            maybe_state = chunk.get("data")
            if isinstance(maybe_state, dict):
                final_state = maybe_state

    if streamed_any_text:
        print()

    result_payload = final_state if final_state is not None else ""
    answer = _extract_final_answer(result_payload)
    if not streamed_any_text and answer:
        print(answer)
    used_retrieval = (
        retrieval_collector is not None and len(retrieval_collector.tool_calls) > 0
    )
    if retrieval_collector is not None:
        _annotate_tool_call_batches(
            retrieval_collector.tool_calls,
            final_state.get("messages") if isinstance(final_state, dict) else None,
        )

    return {
        "answer": answer,
        "retrieved_texts": []
        if retrieval_collector is None
        else list(retrieval_collector.texts),
        "retrieved_snippets": []
        if retrieval_collector is None
        else list(retrieval_collector.snippets),
        "tool_calls": []
        if retrieval_collector is None
        else list(retrieval_collector.tool_calls),
        "route": "retrieval" if used_retrieval else "direct",
        "route_source": "agent",
        "route_reason": "主 agent 判断需要检索并调用了工具。"
        if used_retrieval
        else "主 agent 直接回答，未调用知识库工具。",
        "status_events": list(status_sink.events),
        "usage": _usage_for_current_turn(
            final_state.get("messages") if isinstance(final_state, dict) else None
        ),
    }


def _elapsed_ms(start: float, end: float | None) -> int | None:
    """Milliseconds between ``start`` and ``end`` (``None`` if ``end`` is unset)."""
    if end is None:
        return None
    return max(0, int(round((end - start) * 1000)))


def _annotate_tool_call_batches(
    tool_calls: list[dict[str, Any]], messages: Any
) -> None:
    """为工具调用记录标注批次号：同一条 AIMessage 发出的 tool_calls 是模型一轮
    响应里的（可能并行）调用，共享同一个从 1 开始的 ``batch``。历史消息里的旧
    tool_call_id 不在本轮记录中，自然被跳过，批次号只按本轮计数。
    """
    if not tool_calls or not isinstance(messages, (list, tuple)):
        return
    recorded = {entry.get("tool_call_id") for entry in tool_calls} - {None}
    batch_by_call_id: dict[str, int] = {}
    batch = 0
    for message in messages:
        calls = getattr(message, "tool_calls", None)
        if not isinstance(calls, (list, tuple)):
            continue
        ids = [
            call["id"]
            for call in calls
            if isinstance(call, dict) and call.get("id") in recorded
        ]
        if not ids:
            continue
        batch += 1
        for call_id in ids:
            batch_by_call_id[call_id] = batch
    for entry in tool_calls:
        tool_call_id = entry.get("tool_call_id")
        entry["batch"] = (
            batch_by_call_id.get(tool_call_id)
            if isinstance(tool_call_id, str)
            else None
        )


def _iter_agent_stream_events_impl(
    runner: AgentRunner,
    user_query: str,
    thread_id: str = "default",
    *,
    clarification_enabled: bool = False,
) -> Iterator[dict[str, Any]]:
    """Yield transport-neutral events for HTTP or other streaming frontends.

    The CLI-specific ``run_agent_streaming`` prints tokens directly.  API callers
    need the same LangGraph stream without stdout side effects, so this iterator
    emits ``message`` deltas and one final ``done`` event containing the normal
    ``run_agent`` result shape.
    """
    agent = runner.agent
    status_sink = AgentStatusSink()
    retrieval_collector, context = runner.new_request_context(
        status_sink, clarification_enabled=clarification_enabled
    )
    # 首 token 时延（TTFT）：从本函数入口计到「最终答案」的首个文本 token 产出。
    # 含路由判定、工具决策轮与检索/工具执行耗时——即用户等到第一个答案字的真实时延。
    # 纯客户端网络往返不计入。注意：工具决策轮也可能吐出文本（前言/思考外显），
    # 遇到 tools 节点必须重置计时，只保留最后一轮工具之后那次模型生成的首 token。
    start = time.monotonic()
    first_token_at: float | None = None
    route_decision = maybe_handle_non_retrieval_query(user_query)
    if route_decision["route"] == "direct":
        first_token_at = time.monotonic()
        result = {
            "answer": route_decision["answer"],
            "retrieved_texts": [],
            "retrieved_snippets": [],
            "tool_calls": [],
            "route": "direct",
            "route_source": route_decision["route_source"],
            "route_reason": route_decision["route_reason"],
            "status_events": list(status_sink.events),
            "usage": None,
            "ttft_ms": _elapsed_ms(start, first_token_at),
        }
        yield {"type": "message", "data": {"delta": result["answer"]}}
        yield {"type": "done", "data": result}
        return

    final_state: dict[str, Any] | None = None
    # 每轮模型生成的最终 chunk 携带完整 usage，逐条收集后累加得本轮用量。
    usage_messages: list[Any] = []
    messages = [{"role": "user", "content": user_query}]
    for chunk in agent.stream(
        {"messages": messages},
        config={"configurable": {"thread_id": thread_id}},
        context=context,
        stream_mode=["messages", "values"],
        version="v2",
    ):
        chunk_type = chunk.get("type")
        if chunk_type == "messages":
            message, metadata = chunk["data"]
            if _message_usage(message):
                usage_messages.append(message)
            node = metadata.get("langgraph_node")
            if node == "tools":
                # 工具执行说明后面还有新一轮模型生成；重置打点，让存活的
                # first_token_at 落在最后一次工具之后那轮（最终答案）的首个文本 token。
                first_token_at = None
                continue
            if node != "model":
                continue
            if CONTEXT_SUMMARIZATION_NO_STREAM_TAG in metadata.get("tags", ()):
                continue
            text = _stream_message_text(message)
            if text:
                if first_token_at is None:
                    first_token_at = time.monotonic()
                yield {"type": "message", "data": {"delta": text}}
        elif chunk_type == "values":
            maybe_state = chunk.get("data")
            if isinstance(maybe_state, dict):
                final_state = maybe_state

    used_retrieval = len(retrieval_collector.tool_calls) > 0
    _annotate_tool_call_batches(
        retrieval_collector.tool_calls,
        final_state.get("messages") if isinstance(final_state, dict) else None,
    )
    result = {
        "answer": _extract_final_answer(final_state if final_state is not None else ""),
        "retrieved_texts": list(retrieval_collector.texts),
        "retrieved_snippets": list(retrieval_collector.snippets),
        "tool_calls": list(retrieval_collector.tool_calls),
        "route": "retrieval" if used_retrieval else "direct",
        "route_source": "agent",
        "route_reason": "主 agent 判断需要检索并调用了工具。"
        if used_retrieval
        else "主 agent 直接回答，未调用知识库工具。",
        "status_events": list(status_sink.events),
        "usage": _sum_usage(usage_messages)
        or _last_usage(
            final_state.get("messages") if isinstance(final_state, dict) else None
        ),
        "ttft_ms": _elapsed_ms(start, first_token_at),
    }
    clarification = next(
        (
            event.get("clarification")
            for event in status_sink.events
            if event.get("event") == "clarification_requested"
            and isinstance(event.get("clarification"), dict)
        ),
        None,
    )
    if clarification is not None:
        result["clarification"] = clarification
        result["answer"] = str(clarification.get("question", "请补充必要信息。"))
        yield {"type": "clarification", "data": clarification}
    yield {"type": "done", "data": result}


__all__ = [
    "AgentRequestContextFactory",
    "AgentRunner",
    "AgentStatusSink",
    "RetrievalCollector",
]
