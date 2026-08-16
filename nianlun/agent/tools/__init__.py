"""Nianlun 的模块级 LangChain 工具。

工具实现不通过闭包绑定知识库或请求状态。LangGraph 在执行工具时注入隐藏的
``ToolRuntime``，工具从 ``runtime.context`` 读取当前应用绑定的依赖。
"""

from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING, Any, cast

from langchain.tools import ToolRuntime, tool

from nianlun.agent.contracts import AgentRequestContext, KnowledgeBasePort
from nianlun.agent.tools.clarification_tool import (
    ask_clarification_tool as ask_clarification_tool,
)

if TYPE_CHECKING:
    from nianlun.agent.lead_agent.runner import RetrievalCollector

logger = logging.getLogger(__name__)


def _wrap_tool_result(
    name: str, args: dict[str, Any], result: str, log_tools: bool = True
) -> str:
    """记录工具调用日志并返回原始结果。"""
    if log_tools:
        logger.info("🔧 [调用 %s(%s)]", name, args)
        preview = result[:400] + "..." if len(result) > 400 else result
        suffix = (
            "（日志仅显示前 400 字符，完整结果已返回）" if len(result) > 400 else ""
        )
        logger.info("📄 %s%s", preview, suffix)
    return result


def _run_tool(name: str, func, log_tools: bool = True, **kwargs) -> str:
    """统一执行工具和日志；异常交给 Agent middleware 处理。"""
    result = func(**kwargs)
    return _wrap_tool_result(name, kwargs, result, log_tools=log_tools)


def _context_value(runtime: Any, key: str, default: Any = None) -> Any:
    context = getattr(runtime, "context", None) or {}
    return context.get(key, default)


def _knowledge_base(runtime: Any) -> KnowledgeBasePort:
    kb = _context_value(runtime, "knowledge_base")
    if kb is None:
        raise RuntimeError("工具运行时缺少应用绑定的 knowledge_base。")
    return cast(KnowledgeBasePort, kb)


def _record_tool(
    runtime: Any, name: str, elapsed_ms: int | None = None, **kwargs: Any
) -> None:
    collector: RetrievalCollector | None = _context_value(
        runtime, "retrieval_collector"
    )
    if collector is not None:
        collector.record_tool_call(
            name,
            kwargs,
            elapsed_ms=elapsed_ms,
            tool_call_id=getattr(runtime, "tool_call_id", None),
        )


def _elapsed_since(start: float) -> int:
    return max(0, int(round((time.monotonic() - start) * 1000)))


def _tool_logging(runtime: Any) -> bool:
    return bool(_context_value(runtime, "tool_logging", True))


Runtime = ToolRuntime[AgentRequestContext, Any]

DEFAULT_GET_LINE_CONTENT_CHAR_LIMIT = 4000
MAX_GET_LINE_CONTENT_CHAR_LIMIT = 8000


def _effective_get_line_content_char_limit(char_limit: int | None) -> int:
    """Apply the model-facing default and hard maximum for content windows."""
    if char_limit is None:
        return DEFAULT_GET_LINE_CONTENT_CHAR_LIMIT
    if isinstance(char_limit, int) and char_limit > MAX_GET_LINE_CONTENT_CHAR_LIMIT:
        return MAX_GET_LINE_CONTENT_CHAR_LIMIT
    return char_limit


@tool("search_document_nodes")
def search_document_nodes_tool(
    runtime: Runtime, query: str, doc_ids: list[str] | None = None
) -> str:
    """全文检索文档节点，并返回可用于 get_line_content 的节点提示。

    ``doc_ids`` 可选，用于在语义文档路由返回的候选文档范围内继续检索。
    """
    start = time.monotonic()
    try:
        return _run_tool(
            "search_document_nodes",
            _knowledge_base(runtime).search_document_nodes,
            log_tools=_tool_logging(runtime),
            query=query,
            doc_ids=doc_ids,
        )
    finally:
        _record_tool(
            runtime,
            "search_document_nodes",
            elapsed_ms=_elapsed_since(start),
            query=query,
            doc_ids=doc_ids,
        )


@tool("find_semantic_documents")
def find_semantic_documents_tool(runtime: Runtime, query: str, top_k: int = 15) -> str:
    """向量检索（备选）：将 query 转换为向量后做语义相似度检索。

    耗时较高，仅推荐在 search_document_nodes 多次改写 query 仍检索不到相关内容时，
    作为最后手段使用；query 尽量保持完整，不要拆分成关键词。
    """
    if top_k < 1 or top_k > 20:
        raise ValueError("top_k 必须在 1 到 20 之间")
    start = time.monotonic()
    try:
        result = json.dumps(
            {
                "query": query,
                "documents": _knowledge_base(runtime).find_semantic_documents(
                    query, top_k=top_k
                ),
            },
            ensure_ascii=False,
            indent=2,
        )
        return _wrap_tool_result(
            "find_semantic_documents",
            {"query": query, "top_k": top_k},
            result,
            log_tools=_tool_logging(runtime),
        )
    finally:
        _record_tool(
            runtime,
            "find_semantic_documents",
            elapsed_ms=_elapsed_since(start),
            query=query,
            top_k=top_k,
        )


@tool("get_document")
def get_document_tool(runtime: Runtime, doc_id: str) -> str:
    """返回指定文档的元信息（类型、完整描述和行数）。"""
    start = time.monotonic()
    try:
        return _run_tool(
            "get_document",
            _knowledge_base(runtime).get_document,
            log_tools=_tool_logging(runtime),
            doc_id=doc_id,
        )
    finally:
        _record_tool(
            runtime,
            "get_document",
            elapsed_ms=_elapsed_since(start),
            doc_id=doc_id,
        )


@tool("get_structure_outline")
def get_structure_outline_tool(runtime: Runtime, doc_id: str) -> str:
    """返回文档目录结构（节点 ID、标题、行号），不含正文。命中正文不足、需查找相关章节或核对结构时调用。"""
    start = time.monotonic()
    try:
        return _run_tool(
            "get_structure_outline",
            _knowledge_base(runtime).get_structure_outline,
            log_tools=_tool_logging(runtime),
            doc_id=doc_id,
        )
    finally:
        _record_tool(
            runtime,
            "get_structure_outline",
            elapsed_ms=_elapsed_since(start),
            doc_id=doc_id,
        )


@tool("get_line_content")
def get_line_content_tool(
    runtime: Runtime,
    doc_id: str,
    line_spec: str,
    char_offset: int = 0,
    char_limit: int | None = DEFAULT_GET_LINE_CONTENT_CHAR_LIMIT,
) -> str:
    """获取指定文档中指定行号范围的正文。

    line_spec 支持 "5-7"、"3,8,12" 和 "1-10,50-60"。长节点可用
    char_offset 和 char_limit 按字符窗口读取。默认每个命中节点最多返回 4000
    个字符，每个命中节点单次最多允许请求 8000 个字符；如果返回 text_truncated=true，
    请使用 next_char_offset 继续读取同一节点，直到 text_truncated=false。
    """
    start = time.monotonic()
    effective_char_limit = _effective_get_line_content_char_limit(char_limit)
    try:
        result = _run_tool(
            "get_line_content",
            _knowledge_base(runtime).get_line_content,
            log_tools=_tool_logging(runtime),
            doc_id=doc_id,
            line_spec=line_spec,
            char_offset=char_offset,
            char_limit=effective_char_limit,
        )
    finally:
        _record_tool(
            runtime,
            "get_line_content",
            elapsed_ms=_elapsed_since(start),
            doc_id=doc_id,
            line_spec=line_spec,
            char_offset=char_offset,
            char_limit=effective_char_limit,
        )
    collector: RetrievalCollector | None = _context_value(
        runtime, "retrieval_collector"
    )
    if collector is not None:
        result = collector.add_line_content_result(result)
    return result


# ``ask_clarification_tool`` is exported as a separate optional capability. It
# is intentionally not included in ``build_tools`` until the Agent integration
# phase, so importing this module does not expose it to the current model.


def build_tools(
    *, include_vector: bool = False, include_clarification: bool = False
) -> list:
    """返回基于全文检索和可选向量检索的工具集合。"""
    tools = [
        search_document_nodes_tool,
        get_document_tool,
        get_structure_outline_tool,
        get_line_content_tool,
    ]
    if include_vector:
        tools.insert(1, find_semantic_documents_tool)
    if include_clarification:
        tools.append(ask_clarification_tool)
    return tools
