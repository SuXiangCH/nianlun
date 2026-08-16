"""轻量 Agent middleware。

本包提供可独立测试的横切能力；生产 Agent 由运行时组装层显式接入。
"""

from __future__ import annotations

from nianlun.agent.middleware.clarification_middleware import (
    CLARIFICATION_EVENT_DUPLICATE,
    CLARIFICATION_EVENT_REQUESTED,
    CLARIFICATION_STATUS_WAITING,
    CLARIFICATION_TOOL_NAME,
    CLARIFICATION_TYPES,
    DEFAULT_CLARIFICATION_MAX_CONTEXT_CHARS,
    DEFAULT_CLARIFICATION_MAX_OPTION_CHARS,
    DEFAULT_CLARIFICATION_MAX_OPTIONS,
    DEFAULT_CLARIFICATION_MAX_QUESTION_CHARS,
    ClarificationArgumentError,
    ClarificationMiddleware,
)
from nianlun.agent.middleware.context_summarization_middleware import (
    CONTEXT_SUMMARIZATION_NO_STREAM_TAG,
    CONTEXT_SUMMARY_PROMPT,
    DEFAULT_EVIDENCE_INDEX_TOKEN_LIMIT,
    DEFAULT_EVIDENCE_REFERENCE_LIMIT,
    DEFAULT_SUMMARIZATION_CONVERSATION_TURN_LIMIT,
    DEFAULT_SUMMARIZATION_HARD_LIMIT,
    DEFAULT_SUMMARIZATION_KEEP_POLICY,
    DEFAULT_SUMMARIZATION_TOKEN_TRIGGER,
    DEFAULT_SUMMARIZATION_TRIGGER,
    ContextSummarizationMiddleware,
    build_evidence_reference_index,
)
from nianlun.agent.middleware.dangling_tool_call_middleware import (
    DanglingToolCall,
    DanglingToolCallMiddleware,
    find_missing_tool_results_for_model_tool_calls,
    repair_missing_tool_results_for_model_tool_calls,
)
from nianlun.agent.middleware.retrieval_deduplication_middleware import (
    RETRIEVAL_DEDUPLICATION_TOOL_NAMES,
    RetrievalDeduplicationMiddleware,
    deduplicate_retrieval_result,
)
from nianlun.agent.middleware.tool_error_handling_middleware import (
    ToolErrorHandlingMiddleware,
    classify_tool_execution_exception,
)

__all__ = [
    "CLARIFICATION_EVENT_DUPLICATE",
    "CLARIFICATION_EVENT_REQUESTED",
    "CLARIFICATION_STATUS_WAITING",
    "CLARIFICATION_TOOL_NAME",
    "CLARIFICATION_TYPES",
    "CONTEXT_SUMMARIZATION_NO_STREAM_TAG",
    "CONTEXT_SUMMARY_PROMPT",
    "DEFAULT_CLARIFICATION_MAX_CONTEXT_CHARS",
    "DEFAULT_CLARIFICATION_MAX_OPTIONS",
    "DEFAULT_CLARIFICATION_MAX_OPTION_CHARS",
    "DEFAULT_CLARIFICATION_MAX_QUESTION_CHARS",
    "DEFAULT_EVIDENCE_INDEX_TOKEN_LIMIT",
    "DEFAULT_EVIDENCE_REFERENCE_LIMIT",
    "DEFAULT_SUMMARIZATION_CONVERSATION_TURN_LIMIT",
    "DEFAULT_SUMMARIZATION_HARD_LIMIT",
    "DEFAULT_SUMMARIZATION_KEEP_POLICY",
    "DEFAULT_SUMMARIZATION_TOKEN_TRIGGER",
    "DEFAULT_SUMMARIZATION_TRIGGER",
    "ClarificationArgumentError",
    "ClarificationMiddleware",
    "ContextSummarizationMiddleware",
    "DanglingToolCall",
    "DanglingToolCallMiddleware",
    "RETRIEVAL_DEDUPLICATION_TOOL_NAMES",
    "RetrievalDeduplicationMiddleware",
    "ToolErrorHandlingMiddleware",
    "build_evidence_reference_index",
    "classify_tool_execution_exception",
    "deduplicate_retrieval_result",
    "find_missing_tool_results_for_model_tool_calls",
    "repair_missing_tool_results_for_model_tool_calls",
]
