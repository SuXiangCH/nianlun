"""Knowledge-base tools allowed in the deep-search Agent."""

from __future__ import annotations

from nianlun.agent.tools import build_tools as build_knowledge_tools

DEEP_SEARCH_TOOL_NAMES = frozenset(
    {
        "search_document_nodes",
        "find_semantic_documents",
        "get_structure_outline",
        "get_line_content",
    }
)


def build_deep_search_tools(
    *,
    include_vector: bool = False,
    tool_schema_version: int = 4,
    image_capability: bool = False,
) -> list:
    """Build the allowlisted read-only tools for a child Agent."""
    tools = build_knowledge_tools(
        include_vector=include_vector,
        tool_schema_version=tool_schema_version,
        image_capability=image_capability,
    )
    return [tool for tool in tools if tool.name in DEEP_SEARCH_TOOL_NAMES]


__all__ = ["DEEP_SEARCH_TOOL_NAMES", "build_deep_search_tools"]
