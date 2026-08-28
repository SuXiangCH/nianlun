"""Agent 与知识库、请求上下文之间的稳定端口。"""

from __future__ import annotations

from typing import Any, NotRequired, Protocol, TypedDict

AGENT_TOOL_SCHEMA_VERSION = 4


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
    status_sink: NotRequired[Any]


__all__ = [
    "AGENT_TOOL_SCHEMA_VERSION",
    "AgentRequestContext",
    "AgentStatusSinkPort",
    "KnowledgeBasePort",
    "RetrievalCollectorPort",
]
