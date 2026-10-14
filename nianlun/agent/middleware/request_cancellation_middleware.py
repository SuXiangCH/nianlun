"""Cooperative cancellation checks for nested Agent runs."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware import (
    AgentMiddleware,
    ModelRequest,
    ModelResponse,
    ToolCallRequest,
)
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.errors import GraphBubbleUp
from langgraph.types import Command


class AgentRequestCancelled(GraphBubbleUp):
    """Raised at an Agent boundary after its request stream has been closed."""


def raise_if_cancelled(runtime: Any) -> None:
    context = getattr(runtime, "context", None)
    cancellation_event = (
        context.get("cancellation_event") if isinstance(context, dict) else None
    )
    if cancellation_event is not None and cancellation_event.is_set():
        raise AgentRequestCancelled("Agent request was cancelled")


class RequestCancellationMiddleware(AgentMiddleware):
    """Stop nested model and tool work once the outer response is closed."""

    tools = ()

    def before_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        raise_if_cancelled(runtime)
        return None

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse | AIMessage:
        raise_if_cancelled(request.runtime)
        return handler(request)

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse | AIMessage:
        raise_if_cancelled(request.runtime)
        return await handler(request)

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command[Any]],
    ) -> ToolMessage | Command[Any]:
        raise_if_cancelled(request.runtime)
        return handler(request)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        raise_if_cancelled(request.runtime)
        return await handler(request)


__all__ = [
    "AgentRequestCancelled",
    "RequestCancellationMiddleware",
    "raise_if_cancelled",
]
