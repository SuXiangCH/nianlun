"""工具执行错误处理中间件。

该模块不负责重试，也不修改具体工具。它把工具执行阶段的普通异常转换为
模型可以继续处理的结构化 ``ToolMessage``，同时透传 LangGraph 控制流。
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware, ToolCallRequest
from langchain_core.messages import ToolMessage
from langgraph.errors import GraphBubbleUp
from langgraph.types import Command


def _extract_tool_call_field(tool_call: Any, field: str, default: Any = None) -> Any:
    if isinstance(tool_call, dict):
        return tool_call.get(field, default)
    return getattr(tool_call, field, default)


def _extract_requested_tool_name(request: ToolCallRequest) -> str:
    name = _extract_tool_call_field(request.tool_call, "name", "unknown_tool")
    return str(name or "unknown_tool")


def _extract_requested_tool_call_id(request: ToolCallRequest) -> str:
    call_id = _extract_tool_call_field(request.tool_call, "id")
    return str(call_id) if call_id is not None and str(call_id) else "unknown-tool-call"


def _format_tool_execution_exception_message(exc: Exception) -> str:
    message = str(exc).strip()
    if not message:
        return exc.__class__.__name__
    # Keep useful validation/backend details while preventing an exception from
    # injecting an unbounded path, query, or provider response into the context.
    return message[:500]


def classify_tool_execution_exception(exc: Exception) -> dict[str, Any]:
    """将工具异常归类为稳定的模型可见错误信息。

    ``retryable`` 只表达错误性质，不会触发自动重试。本阶段的默认策略是
    只转换错误，让后续 Agent 接入层决定是否根据该字段重试。
    """
    if isinstance(exc, (ValueError, TypeError)):
        return {
            "code": "invalid_argument",
            "retryable": False,
            "suggested_action": "Fix the tool arguments and try again.",
        }
    if isinstance(exc, (KeyError, FileNotFoundError)):
        return {
            "code": "not_found",
            "retryable": False,
            "suggested_action": "Use a valid identifier returned by the knowledge-base tools.",
        }
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return {
            "code": "transient_backend",
            "retryable": True,
            "suggested_action": "Retry the read-only tool once or report the backend failure.",
        }
    return {
        "code": "internal_error",
        "retryable": False,
        "suggested_action": "Do not repeat the same call without changing the input.",
    }


def _build_structured_tool_error_message(
    request: ToolCallRequest, exc: Exception
) -> ToolMessage:
    error = classify_tool_execution_exception(exc)
    payload = {
        "error": {
            "code": error["code"],
            "tool": _extract_requested_tool_name(request),
            "tool_call_id": _extract_requested_tool_call_id(request),
            "message": _format_tool_execution_exception_message(exc),
            "retryable": error["retryable"],
            "suggested_action": error["suggested_action"],
        }
    }
    return ToolMessage(
        content=json.dumps(payload, ensure_ascii=False),
        tool_call_id=_extract_requested_tool_call_id(request),
        name=_extract_requested_tool_name(request),
        status="error",
    )


class ToolErrorHandlingMiddleware(AgentMiddleware):
    """将工具执行异常转换为 ``status=error`` 的 ``ToolMessage``。

    只捕获普通 ``Exception``。LangGraph 的 ``GraphBubbleUp``（包括中断）必须
    继续向上抛出；handler 返回的 ``Command`` 也原样返回。
    """

    # AgentMiddleware 只提供类型声明，没有默认值；显式声明避免未来传入
    # create_agent 时被当作携带额外工具的 middleware。
    tools = ()

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command[Any]],
    ) -> ToolMessage | Command[Any]:
        return self._execute_tool_call_with_structured_error_handling(request, handler)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        return await self._execute_async_tool_call_with_structured_error_handling(
            request, handler
        )

    def _execute_tool_call_with_structured_error_handling(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command[Any]],
    ) -> ToolMessage | Command[Any]:
        try:
            return handler(request)
        except GraphBubbleUp:
            raise
        except Exception as exc:
            return _build_structured_tool_error_message(request, exc)

    async def _execute_async_tool_call_with_structured_error_handling(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        try:
            return await handler(request)
        except GraphBubbleUp:
            raise
        except Exception as exc:
            return _build_structured_tool_error_message(request, exc)
