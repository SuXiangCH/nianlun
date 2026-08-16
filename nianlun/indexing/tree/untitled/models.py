"""无标题 Markdown 结构恢复的数据模型。

这些模型刻意使用标准库 dataclass，避免把内部规划结果绑定到某个 LLM SDK。
所有源码范围均为 Python 字符的半开区间，行号为 1 基闭区间。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

BlockType = Literal[
    "paragraph",
    "list",
    "table",
    "blockquote",
    "code",
    "image",
    "formula",
    "html",
    "thematic_break",
]


@dataclass(frozen=True, slots=True)
class SourceDocument:
    document_id: str
    raw_markdown: str
    content_hash: str
    line_count: int
    source_path: str | None = None
    encoding: str = "utf-8"


@dataclass(frozen=True, slots=True)
class AtomicBlock:
    block_id: str
    ordinal: int
    block_type: BlockType
    start_char: int
    end_char: int
    start_line: int
    end_line: int
    text: str
    token_count: int
    semantic_splittable: bool = True
    source_hash: str = ""


@dataclass(frozen=True, slots=True)
class PlanningChunk:
    """仅用于 LLM 目录规划的临时连续 block 窗口。"""

    planning_chunk_id: str
    ordinal: int
    block_ids: tuple[str, ...]
    start_block_id: str
    end_block_id: str
    start_char: int
    end_char: int
    start_line: int
    end_line: int
    text: str
    token_count: int
    parent_planning_chunk_id: str | None = None
    split_depth: int = 0


@dataclass(frozen=True, slots=True)
class SectionPlan:
    section_id: str
    title: str
    title_source: Literal["llm", "rule_fallback"] = "llm"
    start_block_ordinal: int = 0
    end_block_ordinal: int = 0
    start_char: int = 0
    end_char: int = 0
    start_line: int = 1
    end_line: int = 1
    summary: str = ""
    parent_id: str | None = None
    depth: int = 1
    confidence: float = 0.0
    child_ids: tuple[str, ...] = ()
    # 源码范围中不属于任何后代 section 的连续片段。
    # 叶节点通常等于自身完整范围；父节点可能为空，也可能包含自身正文。
    own_content_spans: tuple[tuple[int, int], ...] = ()


@dataclass(frozen=True, slots=True)
class UntitledTree:
    sections: tuple[SectionPlan, ...]
    diagnostics: tuple[str, ...] = ()
    # llm：全部 chunk 由 LLM 规划；mixed：部分 chunk 降级为规则兜底；
    # rule_fallback：未配置 LLM 或 LLM 全程不可用，整篇规则兜底。
    structure_mode: Literal["llm", "mixed", "rule_fallback"] = "llm"


@dataclass(frozen=True, slots=True)
class PlannerConfig:
    target_tokens: int = 3000
    max_candidate_tokens: int = 6000
    max_depth: int = 3
    min_confidence: float = 0.70
    min_group_children: int = 2
    max_group_children: int = 8
    # 首次调用之外的重试次数；模型不可达时耗尽后该 chunk 降级为规则兜底。
    max_retries: int = 3
    timeout_seconds: float = 60.0
    # 全流程 LLM 调用下限，包含重试调用。实际预算随文档长度动态放大：
    # effective = max(max_llm_calls, 4 * planning_chunk 数 + 4)，长文档不再被
    # 常量预算卡死；本字段仍作为短文档的兜底下限。
    max_llm_calls: int = 30
    model: str | None = None
    max_atomic_tokens: int = 1200
    bridge_context_blocks: int = 2


class UntitledStructureError(RuntimeError):
    """无标题文档无法获得完整、可信 LLM 目录时抛出的构建错误。"""

    def __init__(self, code: str, message: str, planning_chunk_id: str | None = None):
        super().__init__(message)
        self.code = code
        self.planning_chunk_id = planning_chunk_id
