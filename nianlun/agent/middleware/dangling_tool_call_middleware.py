"""悬空工具调用修复。

模型调用前，如果历史中存在没有对应 ``ToolMessage`` 的
``AIMessage.tool_calls``，向模型请求中补充短小的错误结果，避免 provider 因
消息协议不完整而拒绝整个请求。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain_core.messages import AIMessage, ToolMessage


@dataclass(frozen=True)
class DanglingToolCall:
    """一条被发现但没有工具结果的调用。"""

    tool_call_id: str
    tool_name: str


def _read_message_or_tool_call_field(
    value: Any, field: str, default: Any = None
) -> Any:
    if isinstance(value, Mapping):
        return value.get(field, default)
    return getattr(value, field, default)


def _extract_message_type(message: Any) -> str | None:
    if isinstance(message, Mapping):
        return message.get("type") or message.get("role")
    return getattr(message, "type", None)


def _is_ai_or_assistant_message(message: Any) -> bool:
    return isinstance(message, AIMessage) or _extract_message_type(message) in {
        "ai",
        "assistant",
    }


def _is_tool_result_message(message: Any) -> bool:
    return isinstance(message, ToolMessage) or _extract_message_type(message) == "tool"


def _extract_model_tool_calls(message: Any) -> list[Any]:
    if not _is_ai_or_assistant_message(message):
        return []
    calls = _read_message_or_tool_call_field(message, "tool_calls", ())
    return (
        list(calls)
        if isinstance(calls, Iterable) and not isinstance(calls, (str, bytes, Mapping))
        else []
    )


def _extract_valid_tool_call_id(call: Any) -> str | None:
    call_id = _read_message_or_tool_call_field(call, "id")
    if call_id is None:
        return None
    call_id = str(call_id).strip()
    return call_id or None


def _extract_tool_call_name(call: Any) -> str:
    name = _read_message_or_tool_call_field(call, "name", "unknown_tool")
    return str(name or "unknown_tool")


def _extract_tool_result_call_id(message: Any) -> str | None:
    result_id = _read_message_or_tool_call_field(message, "tool_call_id")
    if result_id is None:
        return None
    result_id = str(result_id).strip()
    return result_id or None


def _identify_missing_tool_call_results(
    calls: Sequence[Any], results: Sequence[Any]
) -> tuple[DanglingToolCall, ...]:
    expected: list[DanglingToolCall] = []
    seen_call_ids: set[str] = set()
    for call in calls:
        call_id = _extract_valid_tool_call_id(call)
        if call_id is None or call_id in seen_call_ids:
            continue
        seen_call_ids.add(call_id)
        expected.append(DanglingToolCall(call_id, _extract_tool_call_name(call)))

    completed = {
        result_id
        for result in results
        if (result_id := _extract_tool_result_call_id(result)) is not None
    }
    return tuple(item for item in expected if item.tool_call_id not in completed)


def _collect_consecutive_tool_result_messages(
    messages: Sequence[Any], start: int
) -> tuple[list[Any], int]:
    """返回 AIMessage 后连续的 ToolMessage，以及下一个未消费位置。"""
    results: list[Any] = []
    index = start
    while index < len(messages) and _is_tool_result_message(messages[index]):
        results.append(messages[index])
        index += 1
    return results, index


def find_missing_tool_results_for_model_tool_calls(
    messages: Sequence[Any],
) -> tuple[DanglingToolCall, ...]:
    """查找历史中缺少结果的工具调用，不修改输入消息。"""
    found: list[DanglingToolCall] = []
    index = 0
    while index < len(messages):
        calls = _extract_model_tool_calls(messages[index])
        if not calls:
            index += 1
            continue
        results, next_index = _collect_consecutive_tool_result_messages(
            messages, index + 1
        )
        found.extend(_identify_missing_tool_call_results(calls, results))
        index = next_index
    return tuple(found)


def _build_interrupted_tool_result_message(item: DanglingToolCall) -> ToolMessage:
    return ToolMessage(
        content=(
            "The tool call was interrupted before it returned a result. "
            "Retry the tool call if the information is still needed."
        ),
        tool_call_id=item.tool_call_id,
        name=item.tool_name,
        status="error",
    )


def repair_missing_tool_results_for_model_tool_calls(
    messages: Sequence[Any],
) -> list[Any]:
    """补齐悬空调用，返回新列表并保持原消息对象不变。"""
    repaired: list[Any] = []
    index = 0
    while index < len(messages):
        message = messages[index]
        repaired.append(message)
        calls = _extract_model_tool_calls(message)
        if not calls:
            index += 1
            continue

        results, next_index = _collect_consecutive_tool_result_messages(
            messages, index + 1
        )
        repaired.extend(results)
        for item in _identify_missing_tool_call_results(calls, results):
            repaired.append(_build_interrupted_tool_result_message(item))
        index = next_index
    return repaired


class DanglingToolCallMiddleware(AgentMiddleware):
    """在模型调用前修复缺少 ``ToolMessage`` 的工具调用。"""

    tools = ()

    def __init__(
        self,
        on_repair: Callable[[tuple[DanglingToolCall, ...]], None] | None = None,
    ) -> None:
        self.on_repair = on_repair

    def _notify_repair(self, repairs: tuple[DanglingToolCall, ...]) -> None:
        if self.on_repair is None:
            return
        try:
            self.on_repair(repairs)
        except Exception:
            # Observability callbacks must not prevent the model from receiving a
            # repaired history.
            return

    def _build_model_request_with_repaired_tool_history(
        self, request: ModelRequest
    ) -> ModelRequest:
        repairs = find_missing_tool_results_for_model_tool_calls(request.messages)
        if repairs:
            self._notify_repair(repairs)
        if not repairs:
            return request
        return request.override(
            messages=repair_missing_tool_results_for_model_tool_calls(request.messages)
        )

    def _build_model_state_update_with_repaired_tool_history(
        self, state: Any
    ) -> dict[str, Any] | None:
        messages = state.get("messages", []) if isinstance(state, Mapping) else []
        repairs = find_missing_tool_results_for_model_tool_calls(messages)
        if not repairs:
            return None
        self._notify_repair(repairs)
        return {"messages": repair_missing_tool_results_for_model_tool_calls(messages)}

    def before_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        """把修复结果写回 Agent 状态，避免每次模型调用重复补齐。"""
        return self._build_model_state_update_with_repaired_tool_history(state)

    async def abefore_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        return self._build_model_state_update_with_repaired_tool_history(state)

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        return self._execute_model_call_with_repaired_tool_history(request, handler)

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Any],
    ) -> Any:
        return await self._execute_async_model_call_with_repaired_tool_history(
            request, handler
        )

    def _execute_model_call_with_repaired_tool_history(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        return handler(self._build_model_request_with_repaired_tool_history(request))

    async def _execute_async_model_call_with_repaired_tool_history(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Any],
    ) -> Any:
        return await handler(
            self._build_model_request_with_repaired_tool_history(request)
        )
