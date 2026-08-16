"""在单次 Agent 请求内去重检索工具返回的文档节点。"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from langchain.agents.middleware import AgentMiddleware, ToolCallRequest
from langchain_core.messages import ToolMessage
from langgraph.types import Command

RETRIEVAL_DEDUPLICATION_TOOL_NAMES = frozenset(
    {"search_document_nodes", "find_semantic_documents"}
)


def _tool_call_field(tool_call: Any, field: str, default: Any = None) -> Any:
    if isinstance(tool_call, Mapping):
        return tool_call.get(field, default)
    return getattr(tool_call, field, default)


def _tool_name(request: ToolCallRequest) -> str:
    return str(_tool_call_field(request.tool_call, "name", "") or "")


def _deduplication_state(runtime: Any) -> dict[str, set[Any]]:
    context = getattr(runtime, "context", None)
    if not isinstance(context, dict):
        return {"documents": set(), "nodes": set()}
    state = context.get("retrieval_deduplication_state")
    if not isinstance(state, dict):
        state = {"documents": set(), "nodes": set()}
        context["retrieval_deduplication_state"] = state
    state.setdefault("documents", set())
    state.setdefault("nodes", set())
    return state


def _node_key(doc_id: str, hint: Mapping[str, Any]) -> tuple[str, str, Any]:
    node_id = hint.get("node_id")
    if node_id is not None and str(node_id):
        return (doc_id, "node_id", str(node_id))
    return (
        doc_id,
        "location",
        (hint.get("line_num"), str(hint.get("title") or "")),
    )


def deduplicate_retrieval_result(result: str, state: dict[str, set[Any]]) -> str:
    """移除已在同一请求中返回过的 document/node hint，保持原有协议字段。"""
    try:
        payload = json.loads(result)
    except (TypeError, json.JSONDecodeError):
        return result
    if not isinstance(payload, dict) or payload.get("error"):
        return result
    documents = payload.get("documents")
    if not isinstance(documents, list):
        return result

    # TODO: Add a request-scoped lock so sync parallel tool calls claim results
    # atomically. Until then, concurrent deduplication remains best-effort.
    seen_documents = state["documents"]
    seen_nodes = state["nodes"]
    filtered_documents: list[dict[str, Any]] = []
    removed_documents = 0
    removed_node_hints = 0
    for document in documents:
        if not isinstance(document, dict):
            continue
        doc_id = document.get("doc_id")
        if not isinstance(doc_id, str) or not doc_id:
            filtered_documents.append(document)
            continue

        node_hints = document.get("node_hints")
        if isinstance(node_hints, list) and node_hints:
            fresh_hints = []
            for hint in node_hints:
                if not isinstance(hint, dict):
                    continue
                key = _node_key(doc_id, hint)
                if key in seen_nodes:
                    removed_node_hints += 1
                    continue
                seen_nodes.add(key)
                fresh_hints.append(hint)
            if not fresh_hints:
                removed_documents += 1
                continue
            filtered_document = dict(document)
            filtered_document["node_hints"] = fresh_hints
            filtered_documents.append(filtered_document)
            seen_documents.add(doc_id)
        elif doc_id not in seen_documents:
            filtered_documents.append(document)
            seen_documents.add(doc_id)
        else:
            removed_documents += 1

    payload["documents"] = filtered_documents
    if removed_documents or removed_node_hints:
        payload["deduplication"] = {
            "applied": True,
            "reason": (
                "本次检索命中存在重复的文档或节点。"
                "系统已排除与当前请求内已处理的检索结果重合的部分，"
                "仅返回尚未提供的新内容，以减少重复上下文。"
            ),
            "removed_documents": removed_documents,
            "removed_node_hints": removed_node_hints,
        }
    return json.dumps(payload, ensure_ascii=False, indent=2)


class RetrievalDeduplicationMiddleware(AgentMiddleware):
    """过滤当前请求内重复的节点检索结果，避免重复占用模型上下文。"""

    tools = ()

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command[Any]],
    ) -> ToolMessage | Command[Any]:
        result = handler(request)
        return self._deduplicate(request, result)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        result = await handler(request)
        return self._deduplicate(request, result)

    def _deduplicate(
        self, request: ToolCallRequest, result: ToolMessage | Command[Any]
    ) -> ToolMessage | Command[Any]:
        if _tool_name(request) not in RETRIEVAL_DEDUPLICATION_TOOL_NAMES:
            return result
        if not isinstance(result, ToolMessage) or result.status == "error":
            return result
        if not isinstance(result.content, str):
            return result
        content = deduplicate_retrieval_result(
            result.content, _deduplication_state(request.runtime)
        )
        return result.model_copy(update={"content": content})


__all__ = [
    "RETRIEVAL_DEDUPLICATION_TOOL_NAMES",
    "RetrievalDeduplicationMiddleware",
    "deduplicate_retrieval_result",
]
