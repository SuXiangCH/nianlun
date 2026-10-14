"""Annotate recorded tool calls with model-round batch numbers."""

from __future__ import annotations

from typing import Any


def annotate_tool_call_batches(tool_calls: list[dict[str, Any]], messages: Any) -> None:
    """Assign one batch number to tool calls emitted by the same AI message."""
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


__all__ = ["annotate_tool_call_batches"]
