"""v1 的单向检索子图与回答节点。"""

from __future__ import annotations

from typing import Any, cast

from langgraph.graph import END, START, StateGraph
from langgraph.checkpoint.memory import MemorySaver
from langgraph.runtime import Runtime

from nianlun.agent.contracts import AgentRequestContext, RetrievedSnippet
from nianlun.agent.citations import extract_citation_ids
from nianlun.agent.middleware.request_cancellation_middleware import (
    raise_if_cancelled,
)
from nianlun.models.llm import content_to_text
from nianlun.agent.retrieval_answer.prompt import (
    build_answer_messages,
    build_retrieval_messages,
)
from nianlun.agent.retrieval_answer.state import RetrievalAnswerGraphState
from nianlun.agent.tool_call_batches import annotate_tool_call_batches
from nianlun.agent.usage import message_usage

_RETRIEVAL_TOOL_NAMES = {
    "ask_clarification",
    "find_semantic_documents",
    "get_document",
    "get_line_content",
    "get_structure_outline",
    "search_document_nodes",
}


def _stream_message_tool_names(message: Any) -> list[str]:
    containers = [
        getattr(message, "tool_call_chunks", None),
        getattr(message, "tool_calls", None),
    ]
    additional_kwargs = getattr(message, "additional_kwargs", None)
    if isinstance(additional_kwargs, dict):
        containers.append(additional_kwargs.get("tool_calls"))

    names: list[str] = []
    for calls in containers:
        if not isinstance(calls, (list, tuple)):
            continue
        for call in calls:
            name = (
                call.get("name")
                if isinstance(call, dict)
                else getattr(call, "name", None)
            )
            if isinstance(name, str) and name.strip() and name.strip() not in names:
                names.append(name.strip())
    return names


def _clarification(context: AgentRequestContext) -> dict[str, Any] | None:
    sink = context.get("status_sink")
    events = getattr(sink, "events", ())
    for event in events:
        value = event.get("clarification") if isinstance(event, dict) else None
        if isinstance(value, dict):
            return value
    return None


def _raise_for_empty_answer(response: Any, answer_text: str) -> None:
    if answer_text.strip():
        return

    response_metadata = getattr(response, "response_metadata", None)
    finish_reason = (
        response_metadata.get("finish_reason")
        if isinstance(response_metadata, dict)
        else None
    )
    additional_kwargs = getattr(response, "additional_kwargs", None)
    reasoning = (
        additional_kwargs.get("reasoning_content")
        if isinstance(additional_kwargs, dict)
        else None
    )
    details: list[str] = []
    if finish_reason is not None:
        details.append(f"finish_reason={finish_reason}")
    if isinstance(reasoning, str) and reasoning.strip():
        details.append("仅返回 reasoning_content")
    suffix = f"（{', '.join(details)}）" if details else ""
    raise RuntimeError(f"回答模型未返回可见正文{suffix}，请重试或切换模型。")


def _format_visible_clarification(clarification: dict[str, Any]) -> str:
    parts: list[str] = []
    context = clarification.get("context")
    if context:
        parts.append(str(context))
    parts.append(str(clarification.get("question", "请补充必要信息。")))
    options = clarification.get("options")
    if isinstance(options, (list, tuple)) and options:
        parts.append(
            "\n".join(f"{index}. {option}" for index, option in enumerate(options, 1))
        )
    return "\n\n".join(parts)


def build_retrieval_answer_graph(
    *,
    retrieval_agent: Any,
    answer_model: Any,
    recursion_limit: int,
    context_window_tokens: int | None,
) -> Any:
    """构建固定为 retrieval -> answer 的外层图。"""

    def retrieve(
        state: RetrievalAnswerGraphState,
        runtime: Runtime[AgentRequestContext],
    ) -> dict[str, Any]:
        retrieval_messages = build_retrieval_messages(
            state["question"],
            context_window_tokens=context_window_tokens,
            conversation_context=state.get("conversation_context", []),
        )
        final_result: dict[str, Any] | None = None
        usage_messages: list[Any] = []
        tool_trace_emitted = False
        in_tools_node = False
        for chunk in retrieval_agent.stream(
            {"messages": retrieval_messages},
            config={
                "configurable": {"thread_id": state["run_id"]},
                "recursion_limit": recursion_limit,
            },
            context=runtime.context,
            stream_mode=["messages", "values"],
            version="v2",
        ):
            chunk_type = chunk.get("type")
            if chunk_type == "messages":
                message, metadata = chunk["data"]
                if message_usage(message):
                    usage_messages.append(message)
                node = metadata.get("langgraph_node")
                if node == "tools":
                    if not in_tools_node and not tool_trace_emitted:
                        runtime.stream_writer(
                            {
                                "event": "retrieval_tool_call_started",
                                "tool_names": [],
                            }
                        )
                    in_tools_node = True
                    continue
                if node == "model":
                    if in_tools_node:
                        tool_trace_emitted = False
                        in_tools_node = False
                    tool_names = [
                        name
                        for name in _stream_message_tool_names(message)
                        if name in _RETRIEVAL_TOOL_NAMES
                    ]
                    if tool_names and not tool_trace_emitted:
                        runtime.stream_writer(
                            {
                                "event": "retrieval_tool_call_started",
                                "tool_names": tool_names,
                            }
                        )
                        tool_trace_emitted = True
            elif chunk_type == "values" and isinstance(chunk.get("data"), dict):
                final_result = chunk["data"]

        collector = runtime.context["retrieval_collector"]
        snippets = cast(list[RetrievedSnippet], list(collector.snippets))
        messages = final_result.get("messages") if final_result is not None else None
        annotate_tool_call_batches(collector.tool_calls, messages)
        if not usage_messages and isinstance(messages, (list, tuple)):
            usage_messages = list(messages)
        usage = None
        if usage_messages:
            usages = [message_usage(message) for message in usage_messages]
            known = [item for item in usages if item]
            if known:
                usage = {
                    key: sum(item.get(key, 0) for item in known)
                    for key in {key for item in known for key in item}
                }
        clarification = _clarification(runtime.context)
        used_retrieval = bool(collector.tool_calls or snippets)
        update: dict[str, Any] = {
            "retrieved_snippets": snippets,
            "clarification": clarification,
            "retrieval_usage": usage,
            "answer": "",
        }
        if clarification is not None:
            update.update(
                {
                    "answer_usage": None,
                    "answer_input_citation_ids": [],
                    "omitted_citation_ids": [],
                    "conversation_context": [
                        {"role": "user", "content": state["question"]},
                        {
                            "role": "assistant",
                            "content": _format_visible_clarification(clarification),
                        },
                    ],
                }
            )
        elif not used_retrieval:
            final_message = (
                messages[-1]
                if isinstance(messages, (list, tuple)) and messages
                else None
            )
            direct_answer = (
                content_to_text(final_message) if final_message is not None else ""
            )
            _raise_for_empty_answer(final_message, direct_answer)
            update.update(
                {
                    "answer": direct_answer,
                    "answer_usage": None,
                    "answer_input_citation_ids": [],
                    "omitted_citation_ids": [],
                    "conversation_context": [
                        {"role": "user", "content": state["question"]},
                        {"role": "assistant", "content": direct_answer},
                    ],
                }
            )
        return update

    def answer(
        state: RetrievalAnswerGraphState,
        runtime: Runtime[AgentRequestContext],
    ) -> dict[str, Any]:
        raise_if_cancelled(runtime)
        messages, citation_ids, omitted_ids = build_answer_messages(
            state["question"],
            state.get("retrieved_snippets", []),
            context_window_tokens=context_window_tokens,
            conversation_context=state.get("conversation_context", []),
        )
        response = answer_model.invoke(messages)
        answer_text = content_to_text(response)
        _raise_for_empty_answer(response, answer_text)
        cited_ids = extract_citation_ids(answer_text)
        invalid_ids = cited_ids - set(citation_ids)
        if invalid_ids:
            raise ValueError(f"回答引用了未进入上下文的证据: {sorted(invalid_ids)}")
        return {
            "answer": answer_text,
            "answer_input_citation_ids": citation_ids,
            "omitted_citation_ids": omitted_ids,
            "answer_usage": message_usage(response),
            "conversation_context": [
                {"role": "user", "content": state["question"]},
                {"role": "assistant", "content": answer_text},
            ],
        }

    def after_retrieval(state: RetrievalAnswerGraphState) -> str:
        if state.get("clarification") is not None or state.get("answer"):
            return "done"
        return "answer"

    builder = StateGraph(RetrievalAnswerGraphState, context_schema=AgentRequestContext)
    builder.add_node("retrieval", retrieve)
    builder.add_node("answer", answer)
    builder.add_edge(START, "retrieval")
    builder.add_conditional_edges(
        "retrieval", after_retrieval, {"answer": "answer", "done": END}
    )
    builder.add_edge("answer", END)
    return builder.compile(checkpointer=MemorySaver())


__all__ = ["build_retrieval_answer_graph"]
