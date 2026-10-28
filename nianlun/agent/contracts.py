"""Agent 与知识库、请求上下文之间的稳定端口。"""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Literal, NotRequired, Protocol, TypedDict
from nianlun.agent.image_history import TrustedHistory

AGENT_TOOL_SCHEMA_VERSION = 5
AgentVersion = Literal["v0", "v1"]


@dataclass(frozen=True, slots=True)
class AnswerImagePolicy:
    """Immutable per-request limits shared by execution and response projection."""

    max_image_bytes: int = 8 * 1024 * 1024
    max_image_pixels: int = 40_000_000
    max_candidates: int = 64
    max_answer_images: int = 32
    max_answer_bytes: int = 128 * 1024 * 1024
    max_preview_images: int = 8
    max_preview_bytes: int = 32 * 1024 * 1024
    max_occurrences: int = 128
    max_candidate_line_bytes: int = 4 * 1024
    max_canonical_bytes: int = 1024 * 1024
    inline_max_bytes: int = 1024 * 1024
    inline_total_bytes: int = 2 * 1024 * 1024
    inline_response_max_bytes: int = 8 * 1024 * 1024
    inline_budget_enabled: bool = False

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            value = getattr(self, name)
            if name == "inline_budget_enabled":
                if type(value) is not bool:
                    raise ValueError("inline_budget_enabled must be a boolean")
            elif type(value) is not int or value < 1:
                raise ValueError(f"{name} must be a positive integer")

    def archive_budget_reason(
        self, size: int, pixels: int, count: int, total: int
    ) -> str | None:
        if size > self.max_image_bytes:
            return "image_too_large"
        if pixels > self.max_image_pixels:
            return "pixel_limit_exceeded"
        if count >= self.max_answer_images or total + size > self.max_answer_bytes:
            return "budget_exceeded"
        if self.inline_budget_enabled and (
            size > self.inline_max_bytes or total + size > self.inline_total_bytes
        ):
            return "inline_budget_exceeded"
        return None

    def model_message(self) -> dict[str, str]:
        limits = {
            name: getattr(self, name)
            for name in self.__dataclass_fields__
            if not name.startswith("max_preview")
        }
        return {
            "role": "system",
            "content": "Current request image resource limits (bytes, pixels, unique images and marker occurrences):\n"
            + json.dumps(limits, sort_keys=True, separators=(",", ":")),
        }


class RetrievedSnippet(TypedDict):
    """现有 collector、运行结果与回答节点共享的检索片段格式。"""

    citation_id: int
    doc_id: Any
    doc_name: Any
    line_spec: Any
    node_id: Any
    title: str
    line_num: Any
    text: str
    char_offset: Any
    char_limit: Any
    total_chars: Any
    text_truncated: bool
    images: NotRequired[list[dict[str, Any]]]
    model_text: NotRequired[str]
    model_images: NotRequired[list[dict[str, Any]]]
    image_spans: NotRequired[list[dict[str, Any]]]


class KnowledgeBasePort(Protocol):
    """Agent 工具和提示词实际依赖的最小知识库接口。"""

    @property
    def has_fts(self) -> bool: ...

    @property
    def has_vector(self) -> bool: ...

    @property
    def meta(self) -> dict[str, Any]: ...

    def list_documents(self, detailed: bool = True) -> str: ...

    def search_document_nodes(
        self, query: str, doc_ids: list[str] | None = None
    ) -> str: ...

    def find_semantic_documents(self, query: str, top_k: int = 5) -> list[dict]: ...

    def get_document(self, doc_id: str) -> str: ...

    def get_structure_outline(self, doc_id: str) -> str: ...

    def get_line_content(
        self,
        doc_id: str,
        line_spec: str,
        char_offset: int = 0,
        char_limit: int | None = None,
    ) -> str: ...


class RetrievalCollectorPort(Protocol):
    def record_tool_call(
        self,
        name: str,
        args: dict[str, Any],
        elapsed_ms: int | None = None,
        tool_call_id: str | None = None,
    ) -> None: ...

    def add_line_content_result(self, result: str) -> str: ...


class AgentStatusSinkPort(Protocol):
    def emit(self, event: str, message: str, **details: Any) -> None: ...


class AgentRequestContext(TypedDict):
    """每次 Agent 执行注入 ToolRuntime 的请求级依赖。"""

    # LangChain 会让 Pydantic解析 ToolRuntime.context；Protocol 不能作为
    # Pydantic 的运行时 isinstance 校验目标，因此载荷保持 Any，具体依赖边界
    # 由 ContextFactory 和工具访问器上的 Protocol 类型约束。
    knowledge_base: Any
    retrieval_collector: Any
    tool_logging: bool
    clarification_enabled: bool
    retrieval_deduplication_state: Any
    loop_guard_state: NotRequired[Any]
    status_sink: NotRequired[Any]
    cancellation_event: NotRequired[Any]
    input_messages: NotRequired[list[dict[str, str]]]
    trusted_history: NotRequired[TrustedHistory]


__all__ = [
    "AGENT_TOOL_SCHEMA_VERSION",
    "AnswerImagePolicy",
    "AgentVersion",
    "AgentRequestContext",
    "AgentStatusSinkPort",
    "KnowledgeBasePort",
    "RetrievedSnippet",
    "RetrievalCollectorPort",
]
