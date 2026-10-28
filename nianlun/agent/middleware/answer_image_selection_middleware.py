"""Freeze image selection authorization before executing each model tool batch."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware, AgentState, ToolCallRequest
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.runtime import Runtime
from langgraph.types import Command

from nianlun.agent.tools.answer_images import (
    AnswerImageSelectionArgs,
    request_image_context,
)


class AnswerImageSelectionMiddleware(AgentMiddleware):
    tools = ()

    def after_model(self, state: AgentState, runtime: Runtime) -> None:
        images = request_image_context(runtime)
        messages = state["messages"]
        if images is not None and messages and isinstance(messages[-1], AIMessage):
            images.freeze_tool_batch([dict(call) for call in messages[-1].tool_calls])

    async def aafter_model(self, state: AgentState, runtime: Runtime) -> None:
        self.after_model(state, runtime)

    def _error(self, request: ToolCallRequest) -> ToolMessage | None:
        call = request.tool_call
        if call["name"] != "select_answer_images":
            return None
        images = request_image_context(request.runtime)
        error = (
            images.selection_preflight_error(call["id"] or "")
            if images
            else "Image selection is unavailable for this request"
        )
        if error is None:
            try:
                args = AnswerImageSelectionArgs.model_validate(call["args"])
                # Validate against the immutable batch plan before the handler can run.
                # Selection itself remains atomic inside the request-local context.
                if images is not None:
                    images.validate_answer_images(call["id"] or "", args.image_refs)
            except ValueError:
                error = "Invalid image selection; use previously read image refs within the request budget"
        if error is None:
            return None
        return ToolMessage(
            content=json.dumps(
                {"code": "answer_image_selection_invalid", "error": error}
            ),
            tool_call_id=call["id"] or "",
            name=call["name"],
            status="error",
        )

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command[Any]],
    ) -> ToolMessage | Command[Any]:
        error = self._error(request)
        return error if error is not None else handler(request)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        error = self._error(request)
        return error if error is not None else await handler(request)


__all__ = ["AnswerImageSelectionMiddleware"]
