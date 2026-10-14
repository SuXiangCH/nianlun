"""LLM token usage normalization shared by agent runtimes."""

from __future__ import annotations

from typing import Any


def message_usage(message: Any) -> dict[str, int] | None:
    """Return normalized token usage for one LangChain AI message."""
    usage_metadata = getattr(message, "usage_metadata", None)
    if not isinstance(usage_metadata, dict):
        return None

    cached_tokens = 0
    input_details = usage_metadata.get("input_token_details") or {}
    if isinstance(input_details, dict):
        cached_tokens = int(input_details.get("cache_read") or 0)

    if not cached_tokens:
        response_metadata = getattr(message, "response_metadata", None) or {}
        token_usage = response_metadata.get("token_usage") or {}
        if isinstance(token_usage, dict):
            cached_tokens = int(
                token_usage.get("prompt_cache_hit_tokens")
                or token_usage.get("cached_tokens")
                or token_usage.get("cache_read_input_tokens")
                or 0
            )

    return {
        "input_tokens": int(usage_metadata.get("input_tokens") or 0),
        "output_tokens": int(usage_metadata.get("output_tokens") or 0),
        "total_tokens": int(usage_metadata.get("total_tokens") or 0),
        "cached_tokens": cached_tokens,
    }


__all__ = ["message_usage"]
