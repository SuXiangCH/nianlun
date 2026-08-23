"""通用的证据感知会话摘要 middleware。

底层复用 LangChain 的 ``SummarizationMiddleware``，由 LangChain 负责 token
阈值、保留窗口和 AI/Tool 消息成对保护；本层只补充知识库来源索引、摘要提示词
和摘要失败时的确定性兜底。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from langchain.agents.middleware import (
    SummarizationMiddleware as LangChainSummarizationMiddleware,
)
from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    HumanMessage,
    RemoveMessage,
    ToolMessage,
    get_buffer_string,
)
from langgraph.graph.message import REMOVE_ALL_MESSAGES

from nianlun.agent.token_estimation import estimate_tokens

DEFAULT_SUMMARIZATION_TOKEN_TRIGGER = ("tokens", 64_000)
"""默认在会话历史达到约 64K token 时触发摘要。"""

DEFAULT_SUMMARIZATION_CONVERSATION_TURN_LIMIT = 16
"""默认在累计 16 轮用户-Agent 对话时触发摘要。"""

DEFAULT_SUMMARIZATION_TRIGGER = [
    DEFAULT_SUMMARIZATION_TOKEN_TRIGGER,
]
"""默认按 64K token 触发；真实用户轮次由 middleware 单独统计。"""

DEFAULT_SUMMARIZATION_HARD_LIMIT = 80_000
"""达到该 token 数时跳过摘要模型，直接使用确定性来源索引兜底。"""

DEFAULT_SUMMARIZATION_KEEP_POLICY = ("messages", 16)
"""默认保留最近 16 条消息，工具调用组由底层 middleware 额外保护。"""

DEFAULT_EVIDENCE_REFERENCE_LIMIT = 128
"""摘要中最多保留的来源定位条数。"""

DEFAULT_EVIDENCE_INDEX_TOKEN_LIMIT = 4_000
"""证据索引给摘要模型预留的 token 上限。"""

CONTEXT_SUMMARIZATION_NO_STREAM_TAG = "context_summarization"
"""摘要模型调用使用的流式事件标签。"""


def _emit_context_status(
    runtime: Any,
    event: str,
    message: str,
    **details: Any,
) -> None:
    """向应用层状态 sink 推送事件；没有 sink 时保持 middleware 静默。"""
    context = getattr(runtime, "context", None) or {}
    sink = context.get("status_sink") if isinstance(context, Mapping) else None
    emit = getattr(sink, "emit", None)
    if not callable(emit):
        return
    try:
        emit(event, message, **details)
    except Exception:
        # UI/telemetry sink 不能影响上下文压缩主流程。
        return


CONTEXT_SUMMARY_PROMPT = """你是文档研究会话的上下文摘要器。

请把下面的文档研究会话压缩成可供后续 Agent 继续工作的上下文。只输出摘要，
不要添加说明或 markdown 代码围栏。

摘要必须包含以下部分：

## SESSION_INTENT
用户当前的目标、问题范围和重要约束。首先完整保留最近一条尚未完成的用户请求，
不得省略其中的主体、条件、范围或输出要求，也不得只概括成宽泛主题。

## CONFIRMED_FACTS
已经从正文中确认的事实。每条事实都必须保留对应的文档名、doc_id、node_id、
line_num 或字符窗口。搜索命中、目录标题和模型推测不能写成已确认事实。

## EVIDENCE_INDEX
保留后续回答引用所需的来源定位。区分“正文内容”和“搜索命中”；不要把搜索命中
当作正文事实。不要复制长正文，只保留定位字段。

## OPEN_QUESTIONS
尚未解决的范围、冲突或需要继续检索的问题；没有则写 None。

## NEXT_STEPS
后续 Agent 可以继续执行的检索或回答步骤；没有则写 None。

规则：
- 不编造知识库中没有的事实。
- 保留 doc_id、doc_name、node_id、line_num、line_spec、char_offset 等来源字段。
- 保留用户已经做出的选择和约束。
- 摘要中的工具调用不得脱离对应的工具结果。

以下是从工具结果中提取的结构化来源索引：
<evidence_index>
{evidence_index}
</evidence_index>

以下是需要压缩的会话消息：
<messages>
{messages}
</messages>"""


def _is_tool_message(message: Any) -> bool:
    if isinstance(message, ToolMessage):
        return True
    if isinstance(message, Mapping):
        return message.get("type") == "tool" or message.get("role") == "tool"
    return False


def _message_content_text(message: Any) -> str:
    if isinstance(message, Mapping):
        content = message.get("content")
    else:
        content = getattr(message, "content", None)
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""

    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, Mapping):
            text = block.get("text")
            if not isinstance(text, str):
                text = block.get("content")
            if isinstance(text, str):
                parts.append(text)
    return "".join(parts)


def _tool_message_content_text(tool_message: Any) -> str:
    return _message_content_text(tool_message)


def _latest_user_message(messages: Iterable[AnyMessage]) -> HumanMessage | None:
    latest: HumanMessage | None = None
    for message in messages:
        if (
            isinstance(message, HumanMessage)
            and getattr(message, "name", None) != "context_summary"
        ):
            latest = message
    return latest


def _parse_structured_tool_result(tool_message: Any) -> dict[str, Any] | None:
    if not _is_tool_message(tool_message):
        return None
    content = _tool_message_content_text(tool_message)
    if not isinstance(content, str):
        return None
    try:
        payload = json.loads(content)
    except (TypeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _append_unique_evidence_reference(
    references: list[dict[str, Any]],
    seen_references: set[str],
    reference: dict[str, Any],
) -> str | None:
    compact_reference = {
        key: value
        for key, value in reference.items()
        if value is not None and value != ""
    }
    identity = json.dumps(compact_reference, ensure_ascii=False, sort_keys=True)
    if identity in seen_references:
        return None
    seen_references.add(identity)
    references.append(compact_reference)
    return identity


def _render_evidence_index(
    references: list[dict[str, Any]],
    *,
    truncated: bool = False,
    include_truncated: bool = False,
) -> str:
    if not references:
        return "None"
    payload: dict[str, Any] = {"references": references}
    if include_truncated:
        payload["truncated"] = truncated
    return json.dumps(payload, ensure_ascii=False, indent=2)


def build_evidence_reference_index(
    messages: Iterable[AnyMessage],
    *,
    max_references: int | None = None,
    max_tokens: int | None = None,
    token_counter: Callable[[Iterable[Any]], int] | None = None,
) -> str:
    """从可信工具消息提取紧凑来源索引，不复制长正文。

    ``max_references`` 和 ``max_tokens`` 用于摘要路径，避免来源索引反过来
    撑爆摘要请求。默认不限制，保持该辅助函数原有的完整索引行为。
    """
    if max_references is not None and max_references <= 0:
        raise ValueError("max_references must be greater than zero or None")
    if max_tokens is not None and max_tokens <= 0:
        raise ValueError("max_tokens must be greater than zero or None")
    if max_tokens is not None and token_counter is None:
        raise ValueError("token_counter is required when max_tokens is set")

    references: list[dict[str, Any]] = []
    seen_references: set[str] = set()
    truncated = False
    limited = max_references is not None or max_tokens is not None

    def append_reference(reference: dict[str, Any]) -> bool:
        nonlocal truncated
        identity = _append_unique_evidence_reference(
            references, seen_references, reference
        )
        if identity is None:
            return True

        if max_references is not None and len(references) > max_references:
            references.pop()
            seen_references.remove(identity)
            truncated = True
            return False

        if max_tokens is not None and token_counter is not None:
            candidate = _render_evidence_index(references)
            if token_counter([HumanMessage(content=candidate)]) > max_tokens:
                references.pop()
                seen_references.remove(identity)
                truncated = True
                return False
        return True

    for message in messages:
        payload = _parse_structured_tool_result(message)
        if payload is None:
            continue

        parent_fields = {
            "doc_id": payload.get("doc_id"),
            "doc_name": payload.get("doc_name"),
            "line_spec": payload.get("line_spec"),
        }
        content_items = payload.get("content")
        if isinstance(content_items, list):
            for item in content_items:
                if not isinstance(item, Mapping):
                    continue
                if not append_reference(
                    {
                        "source_kind": "document_content",
                        **parent_fields,
                        "node_id": item.get("node_id"),
                        "title": item.get("title"),
                        "line_num": item.get("line_num"),
                        "char_offset": item.get("char_offset"),
                        "char_limit": item.get("char_limit"),
                        "next_char_offset": item.get("next_char_offset"),
                        "total_chars": item.get("total_chars"),
                    },
                ):
                    break

        documents = payload.get("documents")
        if isinstance(documents, list) and not truncated:
            for item in documents:
                if not isinstance(item, Mapping):
                    continue
                for hint in item.get("node_hints", []):
                    if not isinstance(hint, Mapping):
                        continue
                    if not append_reference(
                        {
                            "source_kind": "search_match",
                            "doc_id": item.get("doc_id"),
                            "doc_name": item.get("doc_name"),
                            "node_id": hint.get("node_id"),
                            "title": hint.get("title"),
                            "line_num": hint.get("line_num"),
                        },
                    ):
                        break
                if truncated:
                    break
                if not append_reference(
                    {
                        "source_kind": "document_search_match",
                        "doc_id": item.get("doc_id"),
                        "doc_name": item.get("doc_name"),
                    },
                ):
                    break

        if truncated:
            break

    return _render_evidence_index(
        references,
        truncated=truncated,
        include_truncated=limited,
    )


def _extract_summary_text(model_response: Any) -> str:
    if isinstance(model_response, str):
        return model_response.strip()
    if isinstance(model_response, list):
        parts: list[str] = []
        for block in model_response:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, Mapping):
                text = block.get("text") or block.get("content")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts).strip()
    text = getattr(model_response, "text", None)
    if isinstance(text, str) and text.strip():
        return text.strip()
    content = getattr(model_response, "content", None)
    if isinstance(content, str) and content.strip():
        return content.strip()
    return ""


class ContextSummarizationMiddleware(LangChainSummarizationMiddleware):
    """通用的上下文预算和证据感知摘要 middleware。

    ``before_model`` / ``abefore_model`` 是 LangChain 要求的 hook 名称，实际逻辑
    放在语义更明确的 ``_build_*`` / ``_generate_*`` 方法中，便于后续接入和测试。
    """

    tools = ()

    def __init__(
        self,
        model: Any,
        *,
        trigger: Any = DEFAULT_SUMMARIZATION_TRIGGER,
        keep: Any = DEFAULT_SUMMARIZATION_KEEP_POLICY,
        hard_limit: int | None = DEFAULT_SUMMARIZATION_HARD_LIMIT,
        token_counter: Callable[[Iterable[Any]], int] | None = None,
        summary_prompt: str = CONTEXT_SUMMARY_PROMPT,
        trim_tokens_to_summarize: int | None = 4_000,
        evidence_reference_limit: int = DEFAULT_EVIDENCE_REFERENCE_LIMIT,
        evidence_index_token_limit: int | None = DEFAULT_EVIDENCE_INDEX_TOKEN_LIMIT,
        context_overhead_tokens: int = 0,
        model_context_limit: int | None = None,
        conversation_turn_limit: int
        | None = DEFAULT_SUMMARIZATION_CONVERSATION_TURN_LIMIT,
    ) -> None:
        if hard_limit is not None and hard_limit <= 0:
            raise ValueError("hard_limit must be greater than zero or None")
        if evidence_reference_limit <= 0:
            raise ValueError("evidence_reference_limit must be greater than zero")
        if evidence_index_token_limit is not None and evidence_index_token_limit <= 0:
            raise ValueError(
                "evidence_index_token_limit must be greater than zero or None"
            )
        if context_overhead_tokens < 0:
            raise ValueError("context_overhead_tokens must not be negative")
        if model_context_limit is not None and model_context_limit <= 0:
            raise ValueError("model_context_limit must be greater than zero or None")
        if conversation_turn_limit is not None and conversation_turn_limit <= 0:
            raise ValueError(
                "conversation_turn_limit must be greater than zero or None"
            )
        self.hard_limit = hard_limit
        self.evidence_reference_limit = evidence_reference_limit
        self.evidence_index_token_limit = evidence_index_token_limit
        self.context_overhead_tokens = context_overhead_tokens
        self.conversation_turn_limit = conversation_turn_limit
        super_kwargs: dict[str, Any] = {
            "trigger": trigger,
            "keep": keep,
            "summary_prompt": summary_prompt,
            "trim_tokens_to_summarize": trim_tokens_to_summarize,
        }
        super_kwargs["token_counter"] = token_counter or estimate_tokens
        super().__init__(model, **super_kwargs)
        self.model_context_limit = (
            model_context_limit or self._read_model_context_limit()
        )
        if self.model_context_limit is not None:
            # A catalog-provided model window is authoritative. The fixed 80K
            # fallback only exists for legacy clients that do not know their
            # model's actual limit.
            self.hard_limit = self.model_context_limit

    def _read_model_context_limit(self) -> int | None:
        profile = getattr(self.model, "profile", None)
        if not isinstance(profile, Mapping):
            return None
        value = profile.get("max_input_tokens")
        return value if isinstance(value, int) and value > 0 else None

    def _should_summarize(self, messages: list[AnyMessage], total_tokens: int) -> bool:
        if (
            self.conversation_turn_limit is not None
            and self._count_conversation_turns(messages) >= self.conversation_turn_limit
        ):
            return True
        if self.model_context_limit is not None:
            early_trigger = int(self.model_context_limit * 0.8)
            threshold = max(1, early_trigger)
            return (
                total_tokens + self.context_overhead_tokens >= threshold
                or self._should_summarize_based_on_reported_tokens(
                    messages, float(threshold)
                )
            )
        return super()._should_summarize(messages, total_tokens)

    @staticmethod
    def _count_conversation_turns(messages: Iterable[AnyMessage]) -> int:
        return sum(
            isinstance(message, HumanMessage)
            and getattr(message, "name", None) != "context_summary"
            for message in messages
        )

    def before_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        return self._build_summarization_state_update(state, runtime)

    async def abefore_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        return await self._build_async_summarization_state_update(state, runtime)

    def _build_summarization_state_update(
        self, state: Any, runtime: Any
    ) -> dict[str, Any] | None:
        messages = state["messages"]
        self._ensure_message_ids(messages)
        total_tokens = self.token_counter(messages)
        if not self._should_summarize(messages, total_tokens):
            return None

        cutoff_index = self._determine_cutoff_index(messages)
        if cutoff_index <= 0:
            if self._has_reached_hard_context_token_limit(total_tokens):
                _emit_context_status(
                    runtime,
                    "context_compaction_started",
                    "正在整理历史上下文...",
                    total_tokens=total_tokens,
                    mode="hard_limit",
                )
                update = self._build_hard_limit_only_state_update(messages)
                _emit_context_status(
                    runtime,
                    "context_compaction_completed",
                    "历史上下文整理完成。",
                    total_tokens=total_tokens,
                    mode="hard_limit",
                )
                return update
            return None

        messages_to_summarize, preserved_messages = self._partition_messages(
            messages, cutoff_index
        )
        hard_limit = self._has_reached_hard_context_token_limit(total_tokens)
        _emit_context_status(
            runtime,
            "context_compaction_started",
            "正在整理历史上下文...",
            total_tokens=total_tokens,
            mode="hard_limit" if hard_limit else "summary_model",
        )
        try:
            summary = (
                None
                if hard_limit
                else self._generate_context_summary(messages_to_summarize)
            )
            mode = "summary_model" if summary is not None else "deterministic_fallback"
            if summary is None:
                summary = self._build_deterministic_evidence_summary(
                    messages_to_summarize
                )
            update = self._build_summarized_message_state_update(
                summary, messages_to_summarize, preserved_messages
            )
        except Exception as exc:
            _emit_context_status(
                runtime,
                "context_compaction_failed",
                "历史上下文整理失败。",
                total_tokens=total_tokens,
                error_type=type(exc).__name__,
            )
            raise
        _emit_context_status(
            runtime,
            "context_compaction_completed",
            "历史上下文整理完成。",
            total_tokens=total_tokens,
            mode=mode,
        )
        return update

    async def _build_async_summarization_state_update(
        self, state: Any, runtime: Any
    ) -> dict[str, Any] | None:
        messages = state["messages"]
        self._ensure_message_ids(messages)
        total_tokens = self.token_counter(messages)
        if not self._should_summarize(messages, total_tokens):
            return None

        cutoff_index = self._determine_cutoff_index(messages)
        if cutoff_index <= 0:
            if self._has_reached_hard_context_token_limit(total_tokens):
                _emit_context_status(
                    runtime,
                    "context_compaction_started",
                    "正在整理历史上下文...",
                    total_tokens=total_tokens,
                    mode="hard_limit",
                )
                update = self._build_hard_limit_only_state_update(messages)
                _emit_context_status(
                    runtime,
                    "context_compaction_completed",
                    "历史上下文整理完成。",
                    total_tokens=total_tokens,
                    mode="hard_limit",
                )
                return update
            return None

        messages_to_summarize, preserved_messages = self._partition_messages(
            messages, cutoff_index
        )
        hard_limit = self._has_reached_hard_context_token_limit(total_tokens)
        _emit_context_status(
            runtime,
            "context_compaction_started",
            "正在整理历史上下文...",
            total_tokens=total_tokens,
            mode="hard_limit" if hard_limit else "summary_model",
        )
        try:
            summary = (
                None
                if hard_limit
                else await self._generate_async_context_summary(messages_to_summarize)
            )
            mode = "summary_model" if summary is not None else "deterministic_fallback"
            if summary is None:
                summary = self._build_deterministic_evidence_summary(
                    messages_to_summarize
                )
            update = self._build_summarized_message_state_update(
                summary, messages_to_summarize, preserved_messages
            )
        except Exception as exc:
            _emit_context_status(
                runtime,
                "context_compaction_failed",
                "历史上下文整理失败。",
                total_tokens=total_tokens,
                error_type=type(exc).__name__,
            )
            raise
        _emit_context_status(
            runtime,
            "context_compaction_completed",
            "历史上下文整理完成。",
            total_tokens=total_tokens,
            mode=mode,
        )
        return update

    def _build_summarized_message_state_update(
        self,
        summary: str,
        summarized_messages: list[AnyMessage],
        preserved_messages: list[AnyMessage],
    ) -> dict[str, Any]:
        active_user_request = self._active_user_request_for_summary(
            summarized_messages, preserved_messages
        )
        new_messages = self._fit_messages_to_hard_limit(
            [
                self._build_context_summary_message(summary, active_user_request),
                *preserved_messages,
            ]
        )
        return {
            "messages": [
                RemoveMessage(id=REMOVE_ALL_MESSAGES),
                *new_messages,
            ]
        }

    def _build_hard_limit_only_state_update(
        self, messages: list[AnyMessage]
    ) -> dict[str, Any] | None:
        compacted_messages = self._fit_messages_to_hard_limit(messages)
        if compacted_messages == messages:
            return None
        return {
            "messages": [
                RemoveMessage(id=REMOVE_ALL_MESSAGES),
                *compacted_messages,
            ]
        }

    def _has_reached_hard_context_token_limit(self, total_tokens: int) -> bool:
        return (
            self.hard_limit is not None
            and total_tokens + self.context_overhead_tokens >= self.hard_limit
        )

    def _message_hard_limit(self) -> int | None:
        if self.hard_limit is None:
            return None
        return max(1, self.hard_limit - self.context_overhead_tokens)

    def _copy_message_with_content(
        self, message: AnyMessage, content: str
    ) -> AnyMessage:
        return message.model_copy(update={"content": content})

    def _truncate_message_to_token_limit(
        self, message: AnyMessage, token_limit: int
    ) -> AnyMessage:
        if self.token_counter([message]) <= token_limit:
            return message

        content = message.content
        if isinstance(content, str):
            text = content
        else:
            text = json.dumps(content, ensure_ascii=False, default=str)

        low, high = 0, len(text)
        best = ""
        while low <= high:
            middle = (low + high) // 2
            candidate = text[:middle]
            candidate_message = self._copy_message_with_content(message, candidate)
            if self.token_counter([candidate_message]) <= token_limit:
                best = candidate
                low = middle + 1
            else:
                high = middle - 1
        return self._copy_message_with_content(message, best)

    def _fit_suffix_messages(
        self, messages: list[AnyMessage], token_limit: int
    ) -> list[AnyMessage]:
        if not messages or token_limit <= 0:
            return []
        if self.token_counter(messages) <= token_limit:
            return list(messages)

        low, high = 0, len(messages)
        while low < high:
            middle = (low + high) // 2
            if self.token_counter(messages[middle:]) <= token_limit:
                high = middle
            else:
                low = middle + 1
        candidate: list[AnyMessage] = list(messages[low:])
        if candidate and isinstance(candidate[0], ToolMessage):
            safe_low = self._find_safe_cutoff_point(messages, low)
            paired_candidate = list(messages[safe_low:])
            if safe_low < low and self.token_counter(paired_candidate) <= token_limit:
                return paired_candidate
            while candidate and isinstance(candidate[0], ToolMessage):
                candidate.pop(0)
        if candidate and self.token_counter(candidate) <= token_limit:
            return candidate

        # An individual tool result can be larger than the remaining budget. Keep
        # the latest tool pair when possible, then compact the latest content.
        if not candidate:
            if (
                len(messages) >= 2
                and isinstance(messages[-1], ToolMessage)
                and isinstance(messages[-2], AIMessage)
            ):
                candidate = messages[-2:]
            elif isinstance(messages[-1], ToolMessage):
                return []
            else:
                candidate = [messages[-1]]
        elif (
            len(candidate) >= 2
            and isinstance(candidate[-1], ToolMessage)
            and isinstance(candidate[-2], AIMessage)
        ):
            candidate = candidate[-2:]
        else:
            candidate = candidate[-1:]
        if self.token_counter(candidate) <= token_limit:
            return candidate

        if (
            len(candidate) == 2
            and isinstance(candidate[0], AIMessage)
            and isinstance(candidate[1], ToolMessage)
        ):
            ai_tokens = self.token_counter([candidate[0]])
            if ai_tokens < token_limit:
                compact_tool = self._truncate_message_to_token_limit(
                    candidate[1], token_limit - ai_tokens
                )
                compact_pair = [candidate[0], compact_tool]
                if self.token_counter(compact_pair) <= token_limit:
                    return compact_pair

        if isinstance(candidate[-1], ToolMessage):
            return []
        latest = self._truncate_message_to_token_limit(candidate[-1], token_limit)
        return [latest] if self.token_counter([latest]) <= token_limit else []

    def _fit_messages_to_hard_limit(
        self, messages: list[AnyMessage]
    ) -> list[AnyMessage]:
        token_limit = self._message_hard_limit()
        if token_limit is None or self.token_counter(messages) <= token_limit:
            return messages

        latest_user = _latest_user_message(messages)
        if latest_user is not None:
            latest_user_index = next(
                index
                for index in range(len(messages) - 1, -1, -1)
                if messages[index] is latest_user
            )
            user_tokens = self.token_counter([latest_user])
            if user_tokens > token_limit:
                return [self._truncate_message_to_token_limit(latest_user, token_limit)]
            if user_tokens == token_limit:
                return [latest_user]

            recent_messages = self._fit_suffix_messages(
                messages[latest_user_index + 1 :], token_limit - user_tokens
            )
            protected_messages: list[AnyMessage] = [latest_user, *recent_messages]
            protected_tokens = self.token_counter(protected_messages)
            if protected_tokens > token_limit:
                protected_messages = [latest_user]
                protected_tokens = user_tokens

            remaining_tokens = token_limit - protected_tokens
            if (
                remaining_tokens > 0
                and latest_user_index > 0
                and isinstance(messages[0], HumanMessage)
                and messages[0].name == "context_summary"
            ):
                compact_summary = self._truncate_message_to_token_limit(
                    messages[0], remaining_tokens
                )
                candidate = [compact_summary, *protected_messages]
                if (
                    _message_content_text(compact_summary).strip()
                    and self.token_counter(candidate) <= token_limit
                ):
                    return candidate
            elif remaining_tokens > 0 and latest_user_index > 0:
                older_messages = self._fit_suffix_messages(
                    messages[:latest_user_index], remaining_tokens
                )
                candidate = [*older_messages, *protected_messages]
                if self.token_counter(candidate) <= token_limit:
                    return candidate
            return protected_messages

        if (
            messages
            and isinstance(messages[0], HumanMessage)
            and messages[0].name == ("context_summary")
        ):
            summary_message = messages[0]
            summary_tokens = self.token_counter([summary_message])
            if summary_tokens >= token_limit:
                return [
                    self._truncate_message_to_token_limit(summary_message, token_limit)
                ]
            preserved = self._fit_suffix_messages(
                messages[1:], token_limit - summary_tokens
            )
            result = [summary_message, *preserved]
            if self.token_counter(result) <= token_limit:
                return result

        return self._fit_suffix_messages(messages, token_limit)

    @staticmethod
    def _active_user_request_for_summary(
        summarized_messages: list[AnyMessage],
        preserved_messages: list[AnyMessage],
    ) -> str | None:
        latest_user = _latest_user_message([*summarized_messages, *preserved_messages])
        if latest_user is None or any(
            message is latest_user for message in preserved_messages
        ):
            return None
        text = _message_content_text(latest_user)
        return text if text.strip() else None

    @staticmethod
    def _build_context_summary_message(
        summary: str, active_user_request: str | None = None
    ) -> HumanMessage:
        active_request_block = ""
        if active_user_request is not None:
            active_request_block = (
                "ACTIVE_USER_REQUEST (preserved verbatim; continue this task):\n"
                f"{active_user_request}\n"
                "END_ACTIVE_USER_REQUEST\n\n"
            )
        return HumanMessage(
            content=(
                f"{active_request_block}"
                "Here is a summary of the document research conversation to date:\n\n"
                f"{summary}"
            ),
            name="context_summary",
            additional_kwargs={"lc_source": "context_summarization"},
        )

    def _build_context_summary_prompt(
        self, messages_to_summarize: list[AnyMessage]
    ) -> str | None:
        trimmed_messages = self._trim_messages_for_summary(messages_to_summarize)
        if not trimmed_messages:
            return None
        formatted_messages = get_buffer_string(trimmed_messages, format="xml")
        evidence_index = build_evidence_reference_index(
            messages_to_summarize,
            max_references=self.evidence_reference_limit,
            max_tokens=self.evidence_index_token_limit,
            token_counter=self.token_counter,
        )
        return self.summary_prompt.format(
            messages=formatted_messages,
            evidence_index=evidence_index,
        ).rstrip()

    def _generate_context_summary(
        self, messages_to_summarize: list[AnyMessage]
    ) -> str | None:
        try:
            prompt = self._build_context_summary_prompt(messages_to_summarize)
        except Exception:
            return None
        if prompt is None:
            return None
        try:
            response = self.model.invoke(
                prompt,
                config={
                    "metadata": {"lc_source": "context_summarization"},
                    "tags": [CONTEXT_SUMMARIZATION_NO_STREAM_TAG],
                },
            )
        except Exception:
            return None
        return _extract_summary_text(response) or None

    async def _generate_async_context_summary(
        self, messages_to_summarize: list[AnyMessage]
    ) -> str | None:
        try:
            prompt = self._build_context_summary_prompt(messages_to_summarize)
        except Exception:
            return None
        if prompt is None:
            return None
        try:
            response = await self.model.ainvoke(
                prompt,
                config={
                    "metadata": {"lc_source": "context_summarization"},
                    "tags": [CONTEXT_SUMMARIZATION_NO_STREAM_TAG],
                },
            )
        except Exception:
            return None
        return _extract_summary_text(response) or None

    def _build_deterministic_evidence_summary(
        self,
        messages_to_summarize: list[AnyMessage],
    ) -> str:
        evidence_index = build_evidence_reference_index(
            messages_to_summarize,
            max_references=self.evidence_reference_limit,
            max_tokens=self.evidence_index_token_limit,
            token_counter=self.token_counter,
        )
        return (
            "Summary model unavailable. The older conversation was compressed using "
            "the deterministic source index below. Re-query the knowledge base if a "
            "fact is not present in the preserved messages.\n\n"
            f"EVIDENCE_INDEX\n{evidence_index}"
        )
