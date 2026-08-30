"""问题澄清 middleware。

``ask_clarification`` 是一个给模型提供 schema 的占位工具，不能靠普通工具
返回值实现“等待用户输入”。本 middleware 在工具执行前拦截该调用，写入一条
带结构化等待信息的 ``ToolMessage``，再通过 ``Command(goto=END)`` 结束当前
Agent run。上层 API 后续可以用相同 thread 恢复并追加用户回答。

本模块不依赖 KnowledgeBase，也不负责把 middleware 接入 Agent runtime。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

from langchain.agents.middleware import AgentMiddleware, ToolCallRequest
from langchain_core.messages import ToolMessage
from langgraph.graph import END
from langgraph.types import Command

CLARIFICATION_TOOL_NAME = "ask_clarification"
CLARIFICATION_STATUS_WAITING = "waiting_for_input"
CLARIFICATION_EVENT_REQUESTED = "clarification_requested"
CLARIFICATION_EVENT_DUPLICATE = "clarification_duplicate"

CLARIFICATION_TYPES = (
    "missing_info",
    "ambiguous_requirement",
    "approach_choice",
    "risk_confirmation",
    "suggestion",
)

DEFAULT_CLARIFICATION_MAX_OPTIONS = 8
DEFAULT_CLARIFICATION_MAX_QUESTION_CHARS = 2_000
DEFAULT_CLARIFICATION_MAX_CONTEXT_CHARS = 4_000
DEFAULT_CLARIFICATION_MAX_OPTION_CHARS = 500


class ClarificationArgumentError(ValueError):
    """澄清调用参数不符合固定协议。"""

    def __init__(self, message: str, *, field: str) -> None:
        super().__init__(message)
        self.field = field


def _tool_call_field(tool_call: Any, field: str, default: Any = None) -> Any:
    if isinstance(tool_call, Mapping):
        return tool_call.get(field, default)
    return getattr(tool_call, field, default)


def _tool_call_name(request: ToolCallRequest) -> str:
    return str(_tool_call_field(request.tool_call, "name", "") or "")


def _tool_call_id(request: ToolCallRequest) -> str:
    value = _tool_call_field(request.tool_call, "id")
    if value is None or not str(value).strip():
        return "unknown-clarification-call"
    return str(value).strip()


def _tool_call_args(request: ToolCallRequest) -> dict[str, Any]:
    args = _tool_call_field(request.tool_call, "args", {})
    if not isinstance(args, Mapping):
        raise ClarificationArgumentError(
            "ask_clarification 的 args 必须是对象。", field="args"
        )
    return dict(args)


def _normalize_text(value: Any, *, field: str, max_chars: int) -> str:
    if not isinstance(value, str):
        raise ClarificationArgumentError(f"{field} 必须是字符串。", field=field)
    text = value.strip()
    if not text:
        raise ClarificationArgumentError(f"{field} 不能为空。", field=field)
    if len(text) > max_chars:
        raise ClarificationArgumentError(
            f"{field} 不能超过 {max_chars} 个字符。", field=field
        )
    return text


def _normalize_optional_text(value: Any, *, field: str, max_chars: int) -> str | None:
    if value is None:
        return None
    return _normalize_text(value, field=field, max_chars=max_chars)


def _normalize_options(value: Any, *, max_options: int, max_chars: int) -> list[str]:
    if value is None:
        return []

    # Some providers serialize an array argument as a JSON string. Accept it,
    # but never iterate over the characters of a plain string.
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (TypeError, json.JSONDecodeError):
            decoded = [value]
        value = decoded if isinstance(decoded, list) else [decoded]

    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ClarificationArgumentError("options 必须是字符串列表。", field="options")
    if len(value) > max_options:
        raise ClarificationArgumentError(
            f"options 不能超过 {max_options} 个选项。", field="options"
        )

    normalized: list[str] = []
    for index, option in enumerate(value):
        if not isinstance(option, str):
            raise ClarificationArgumentError(
                f"options[{index}] 必须是字符串。", field="options"
            )
        option_text = option.strip()
        if not option_text:
            raise ClarificationArgumentError(
                f"options[{index}] 不能为空。", field="options"
            )
        if len(option_text) > max_chars:
            raise ClarificationArgumentError(
                f"options[{index}] 不能超过 {max_chars} 个字符。", field="options"
            )
        normalized.append(option_text)
    return normalized


def _canonical_payload(
    args: Mapping[str, Any], middleware: ClarificationMiddleware
) -> dict[str, Any]:
    clarification_type = args.get("clarification_type")
    if clarification_type not in CLARIFICATION_TYPES:
        allowed = ", ".join(CLARIFICATION_TYPES)
        raise ClarificationArgumentError(
            f"clarification_type 必须是以下值之一：{allowed}。",
            field="clarification_type",
        )

    question = _normalize_text(
        args.get("question"),
        field="question",
        max_chars=middleware.max_question_chars,
    )
    context = _normalize_optional_text(
        args.get("context"),
        field="context",
        max_chars=middleware.max_context_chars,
    )
    options = _normalize_options(
        args.get("options"),
        max_options=middleware.max_options,
        max_chars=middleware.max_option_chars,
    )
    return {
        "question": question,
        "clarification_type": clarification_type,
        "context": context,
        "options": options,
    }


def _payload_fingerprint(payload: Mapping[str, Any]) -> str:
    serialized = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16]


def _message_field(message: Any, field: str, default: Any = None) -> Any:
    if isinstance(message, Mapping):
        return message.get(field, default)
    return getattr(message, field, default)


def _state_messages(state: Any) -> list[Any]:
    if isinstance(state, Mapping):
        messages = state.get("messages", [])
    else:
        messages = getattr(state, "messages", [])
    if not isinstance(messages, Sequence) or isinstance(messages, (str, bytes)):
        return []
    return list(messages)


def _is_human_message(message: Any) -> bool:
    message_type = _message_field(message, "type") or _message_field(message, "role")
    return message_type in {"human", "user"}


def _current_run_messages(messages: Sequence[Any]) -> list[Any]:
    """只保留最近一条用户消息之后的当前 run 消息。

    澄清完成后，用户回答会追加到同一个 thread。旧澄清不能影响恢复后的
    新一轮决策，否则相同问题会被错误地永久判定为重复。
    """
    last_human_index = next(
        (
            index
            for index in range(len(messages) - 1, -1, -1)
            if _is_human_message(messages[index])
        ),
        None,
    )
    if last_human_index is None:
        return list(messages)
    return list(messages[last_human_index + 1 :])


def _message_clarification_metadata(message: Any) -> Mapping[str, Any] | None:
    if _message_field(message, "name") != CLARIFICATION_TOOL_NAME:
        return None
    additional_kwargs = _message_field(message, "additional_kwargs", {})
    if not isinstance(additional_kwargs, Mapping):
        return None
    metadata = additional_kwargs.get("clarification")
    return metadata if isinstance(metadata, Mapping) else None


def _find_existing_clarification(
    messages: Sequence[Any], *, call_id: str, fingerprint: str
) -> tuple[Any, bool] | None:
    """查找同一 tool call 或同一问题的历史澄清消息。"""
    same_fingerprint: tuple[Any, bool] | None = None
    for message in messages:
        metadata = _message_clarification_metadata(message)
        if metadata is None:
            continue
        if str(metadata.get("tool_call_id", "")) == call_id:
            return message, False
        if str(metadata.get("fingerprint", "")) == fingerprint:
            same_fingerprint = (message, True)
    return same_fingerprint


def _stable_message_id(call_id: str, fingerprint: str) -> str:
    if call_id != "unknown-clarification-call":
        return f"clarification:{call_id}"
    return f"clarification:{fingerprint}"


def _format_clarification_message(payload: Mapping[str, Any]) -> str:
    parts: list[str] = []
    context = payload.get("context")
    if context:
        parts.append(f"背景：{context}")
    parts.append(str(payload["question"]))
    options = payload.get("options") or []
    if options:
        parts.append("")
        parts.extend(f"{index}. {option}" for index, option in enumerate(options, 1))
    return "\n".join(parts)


def _status_sink(runtime: Any) -> Any:
    context = getattr(runtime, "context", None) or {}
    return context.get("status_sink") if isinstance(context, Mapping) else None


def _clarification_enabled(runtime: Any) -> bool:
    context = getattr(runtime, "context", None) or {}
    return (
        bool(context.get("clarification_enabled", False))
        if isinstance(context, Mapping)
        else False
    )


def _emit_status(
    runtime: Any,
    event: str,
    message: str,
    clarification: Mapping[str, Any],
    *,
    duplicate: bool = False,
) -> None:
    sink = _status_sink(runtime)
    emit = getattr(sink, "emit", None)
    if not callable(emit):
        return
    try:
        emit(
            event,
            message,
            status=CLARIFICATION_STATUS_WAITING,
            clarification=dict(clarification),
            duplicate=duplicate,
        )
    except Exception:
        # Status reporting must never prevent a clarification from reaching the UI.
        return


def _build_validation_error_message(
    request: ToolCallRequest, error: ClarificationArgumentError
) -> ToolMessage:
    call_id = _tool_call_id(request)
    tool_name = _tool_call_name(request) or CLARIFICATION_TOOL_NAME
    content = json.dumps(
        {
            "error": {
                "code": "invalid_argument",
                "tool": tool_name,
                "tool_call_id": call_id,
                "field": error.field,
                "message": str(error),
                "retryable": False,
                "suggested_action": "修正澄清参数后重试。",
            }
        },
        ensure_ascii=False,
    )
    return ToolMessage(
        content=content,
        tool_call_id=call_id,
        name=tool_name,
        status="error",
    )


class ClarificationMiddleware(AgentMiddleware):
    """拦截 ``ask_clarification`` 并结束当前 run，等待用户回答。

    该类只实现工具调用边界，不保存跨请求可变状态。重复检测基于当前
    ``request.state["messages"]``，因此不同 thread/应用之间不会互相污染。
    """

    tools = ()

    def __init__(
        self,
        *,
        max_options: int = DEFAULT_CLARIFICATION_MAX_OPTIONS,
        max_question_chars: int = DEFAULT_CLARIFICATION_MAX_QUESTION_CHARS,
        max_context_chars: int = DEFAULT_CLARIFICATION_MAX_CONTEXT_CHARS,
        max_option_chars: int = DEFAULT_CLARIFICATION_MAX_OPTION_CHARS,
    ) -> None:
        if max_options < 0:
            raise ValueError("max_options 必须是非负整数。")
        if max_question_chars <= 0:
            raise ValueError("max_question_chars 必须是正整数。")
        if max_context_chars <= 0:
            raise ValueError("max_context_chars 必须是正整数。")
        if max_option_chars <= 0:
            raise ValueError("max_option_chars 必须是正整数。")
        self.max_options = max_options
        self.max_question_chars = max_question_chars
        self.max_context_chars = max_context_chars
        self.max_option_chars = max_option_chars

    def _build_clarification_command(
        self, request: ToolCallRequest
    ) -> ToolMessage | Command[Any]:
        try:
            payload = _canonical_payload(_tool_call_args(request), self)
        except ClarificationArgumentError as error:
            return _build_validation_error_message(request, error)

        call_id = _tool_call_id(request)
        fingerprint = _payload_fingerprint(payload)
        existing = _find_existing_clarification(
            _current_run_messages(_state_messages(request.state)),
            call_id=call_id,
            fingerprint=fingerprint,
        )
        duplicate = existing is not None and existing[1]
        message_id = _stable_message_id(call_id, fingerprint)

        # A repeated call with the same id should replace the old message. A new
        # call asking the same question gets a new tool_call_id for protocol
        # correctness, but is marked duplicate so the UI need not show it twice.
        if existing is not None and not duplicate:
            previous_id = _message_field(existing[0], "id")
            if previous_id:
                message_id = str(previous_id)

        clarification = {
            **payload,
            "status": CLARIFICATION_STATUS_WAITING,
            "tool_call_id": call_id,
            "clarification_id": message_id,
            "fingerprint": fingerprint,
            "duplicate": duplicate,
        }
        message = ToolMessage(
            id=message_id,
            content=_format_clarification_message(payload),
            tool_call_id=call_id,
            name=CLARIFICATION_TOOL_NAME,
            additional_kwargs={
                "status": CLARIFICATION_STATUS_WAITING,
                "clarification": clarification,
            },
        )
        _emit_status(
            request.runtime,
            CLARIFICATION_EVENT_DUPLICATE
            if duplicate
            else CLARIFICATION_EVENT_REQUESTED,
            "相同的澄清问题已存在，等待用户回答。"
            if duplicate
            else "等待用户补充信息。",
            clarification,
            duplicate=duplicate,
        )
        return Command(update={"messages": [message]}, goto=END)

    @staticmethod
    def _build_disabled_message(request: ToolCallRequest) -> ToolMessage:
        """Keep the tool schema stable while preventing a disabled request from stopping."""
        return ToolMessage(
            content=json.dumps(
                {
                    "error": {
                        "code": "clarification_disabled",
                        "tool": CLARIFICATION_TOOL_NAME,
                        "tool_call_id": _tool_call_id(request),
                        "message": "当前请求未启用问题澄清，请基于已有信息继续回答。",
                        "retryable": False,
                    }
                },
                ensure_ascii=False,
            ),
            tool_call_id=_tool_call_id(request),
            name=CLARIFICATION_TOOL_NAME,
            status="error",
        )

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command[Any]],
    ) -> ToolMessage | Command[Any]:
        if _tool_call_name(request) != CLARIFICATION_TOOL_NAME:
            return handler(request)
        if not _clarification_enabled(request.runtime):
            return self._build_disabled_message(request)
        return self._build_clarification_command(request)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        if _tool_call_name(request) != CLARIFICATION_TOOL_NAME:
            return await handler(request)
        if not _clarification_enabled(request.runtime):
            return self._build_disabled_message(request)
        return self._build_clarification_command(request)


__all__ = [
    "CLARIFICATION_EVENT_DUPLICATE",
    "CLARIFICATION_EVENT_REQUESTED",
    "CLARIFICATION_STATUS_WAITING",
    "CLARIFICATION_TOOL_NAME",
    "CLARIFICATION_TYPES",
    "DEFAULT_CLARIFICATION_MAX_CONTEXT_CHARS",
    "DEFAULT_CLARIFICATION_MAX_OPTIONS",
    "DEFAULT_CLARIFICATION_MAX_OPTION_CHARS",
    "DEFAULT_CLARIFICATION_MAX_QUESTION_CHARS",
    "ClarificationArgumentError",
    "ClarificationMiddleware",
]
