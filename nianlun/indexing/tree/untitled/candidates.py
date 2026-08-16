"""将连续 atomic block 按软目标合并为仅供 LLM 使用的 planning chunk。"""

from __future__ import annotations

from nianlun.indexing.tree.llm import count_tokens

from .models import AtomicBlock, PlanningChunk
from .source import SourceView


def build_planning_chunks(
    source: SourceView,
    blocks: list[AtomicBlock],
    target_tokens: int = 3000,
    max_candidate_tokens: int = 6000,
    model: str | None = None,
) -> list[PlanningChunk]:
    """按软目标聚合 block。token 计数为近似值：累加 block 自带计数，块间空白
    单独计数（避免每加一个 block 就重算整段累积文本的 O(n²) tokenize）。
    planning chunk 只是 LLM 规划窗口的软目标，允许边界处的 token 近似。
    ``max_candidate_tokens`` 仅作配置校验；超长 atomic block 仍作为单一 chunk。
    """
    if target_tokens <= 0 or max_candidate_tokens < target_tokens:
        raise ValueError("planning chunk token 配置非法")
    groups: list[list[AtomicBlock]] = []
    current: list[AtomicBlock] = []
    current_tokens = 0
    for block in blocks:
        if not current:
            current = [block]
            current_tokens = block.token_count
            continue
        gap_tokens = count_tokens(
            source.text(current[-1].end_char, block.start_char), model
        )
        proposed = current_tokens + gap_tokens + block.token_count
        if proposed <= target_tokens:
            current.append(block)
            current_tokens = proposed
        else:
            groups.append(current)
            current = [block]
            current_tokens = block.token_count
    if current:
        groups.append(current)
    result: list[PlanningChunk] = []
    for i, group in enumerate(groups):
        raw = source.text(group[0].start_char, group[-1].end_char)
        result.append(
            PlanningChunk(
                f"pc-{i + 1:04d}",
                i,
                tuple(b.block_id for b in group),
                group[0].block_id,
                group[-1].block_id,
                group[0].start_char,
                group[-1].end_char,
                group[0].start_line,
                group[-1].end_line,
                raw,
                count_tokens(raw, model),
            )
        )
    return result
