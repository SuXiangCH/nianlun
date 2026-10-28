"""Strict request-local pure-image selection tool."""

from __future__ import annotations

import json
import time
from typing import Annotated, Any, Literal

from langchain.tools import ToolRuntime, tool
from pydantic import BaseModel, ConfigDict, Field, field_validator

from nianlun.agent.image_context import RequestImageContext


class AnswerImageSelectionArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, arbitrary_types_allowed=True)
    runtime: ToolRuntime[Any, Any] = Field(default=None)
    mode: Literal["images"]
    image_refs: list[Annotated[str, Field(pattern=r"^img_[0-9]{2,}$")]] = Field(
        min_length=1
    )

    @field_validator("image_refs")
    @classmethod
    def unique_refs(cls, refs: list[str]) -> list[str]:
        if len(refs) != len(set(refs)):
            raise ValueError("Image refs must be unique")
        return refs


def request_image_context(runtime: Any) -> RequestImageContext | None:
    context = getattr(runtime, "context", None)
    collector = (
        context.get("retrieval_collector") if isinstance(context, dict) else None
    )
    images = getattr(collector, "image_context", None)
    return images if isinstance(images, RequestImageContext) else None


@tool("select_answer_images", args_schema=AnswerImageSelectionArgs)
def select_answer_images_tool(
    mode: Literal["images"], image_refs: list[str], runtime: ToolRuntime[Any, Any]
) -> str:
    """Select previously read image refs only when the user requests images alone."""
    started = time.monotonic()
    images = request_image_context(runtime)
    if images is None:
        raise ValueError("Image selection is unavailable for this request")
    images.select_answer_images(runtime.tool_call_id or "", image_refs)
    collector = runtime.context["retrieval_collector"]
    collector.record_tool_call(
        "select_answer_images",
        {"mode": mode, "image_refs": image_refs},
        elapsed_ms=max(0, round((time.monotonic() - started) * 1000)),
        tool_call_id=runtime.tool_call_id,
    )
    return json.dumps({"mode": "images", "image_refs": image_refs})


__all__ = ["AnswerImageSelectionArgs", "select_answer_images_tool"]
