"""将 LLM 目录规划转换为连续 section 和目录树。"""

from __future__ import annotations

import re
from typing import Any

from .models import AtomicBlock, SectionPlan
from .source import SourceView
from .validation import valid_title, validate_group_response


def _make(
    source: SourceView,
    blocks: list[AtomicBlock],
    sid: str,
    title: str,
    start: int,
    end: int,
    confidence: float,
) -> SectionPlan:
    first, last = blocks[start], blocks[end]
    return SectionPlan(
        sid,
        title.strip(),
        "llm",
        start,
        end,
        first.start_char,
        last.end_char,
        first.start_line,
        last.end_line,
        "",
        None,
        1,
        float(confidence),
    )


def sections_from_candidate_boundaries(
    source: SourceView,
    blocks: list[AtomicBlock],
    candidate: Any,
    first_title: str,
    first_confidence: float,
    decisions: list[dict[str, Any]],
    next_id: int = 1,
) -> list[SectionPlan]:
    """Build every contiguous leaf section in one candidate.

    The first title comes from classification; each later title comes from the
    boundary immediately preceding it. ``after_block`` is never treated as a
    section start, which makes the mapping unambiguous.
    """
    if not valid_title(first_title):
        raise ValueError("首 section 标题非法")
    index = {b.block_id: b.ordinal for b in blocks}
    start = index[candidate.start_block_id]
    end = index[candidate.end_block_id]
    cuts = sorted(
        (index[d["after_block"]] + 1, d["title"], float(d["confidence"]))
        for d in decisions
        if d.get("boundary")
    )
    result: list[SectionPlan] = []
    cursor, title, confidence = start, first_title, first_confidence
    for boundary, next_title, next_conf in cuts + [(end + 1, None, 0.0)]:
        if boundary <= cursor or boundary > end + 1:
            raise ValueError("boundary 不连续")
        result.append(
            _make(
                source,
                blocks,
                f"s{next_id + len(result):04d}",
                title,
                cursor,
                boundary - 1,
                confidence,
            )
        )
        cursor, title, confidence = boundary, next_title, next_conf
        if cursor == end + 1:
            break
    return result


def sections_from_candidate(
    source: SourceView,
    blocks: list[AtomicBlock],
    candidate: Any,
    title: str,
    confidence: float,
    section_id: str,
) -> SectionPlan:
    if not valid_title(title):
        raise ValueError("LLM 标题非法")
    lookup = {b.block_id: b for b in blocks}
    return _make(
        source,
        blocks,
        section_id,
        title,
        lookup[candidate.start_block_id].ordinal,
        lookup[candidate.end_block_id].ordinal,
        confidence,
    )


def merge_adjacent_sections(
    left: SectionPlan, right: SectionPlan, title: str, confidence: float
) -> SectionPlan:
    """合并跨 planning chunk 但语义连续的相邻 leaf section。"""
    if (
        left.child_ids
        or right.child_ids
        or left.end_block_ordinal + 1 != right.start_block_ordinal
        or not valid_title(title)
    ):
        raise ValueError("不能合并非连续 leaf section")
    return SectionPlan(
        left.section_id,
        title.strip(),
        "llm",
        left.start_block_ordinal,
        right.end_block_ordinal,
        left.start_char,
        right.end_char,
        left.start_line,
        right.end_line,
        "",
        None,
        1,
        float(confidence),
    )


def _fallback_title(text: str, start_line: int, end_line: int) -> str:
    """生成稳定、可追踪但不冒充 LLM 语义的兜底标题。"""
    words = re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}", text)
    counts: dict[str, int] = {}
    for word in words:
        normalized = word.lower()
        counts[normalized] = counts.get(normalized, 0) + 1
    keyword = max(counts, key=lambda word: counts[word]) if counts else ""
    prefix = keyword[:16] if keyword else "文档内容"
    return f"{prefix}（第{start_line}-{end_line}行）"


def fallback_sections_from_candidate(
    source: SourceView,
    blocks: list[AtomicBlock],
    candidate: Any,
    next_id: int,
    target_tokens: int,
) -> list[SectionPlan]:
    """在 LLM 规划持续失败后，为一个连续 region 构造可追踪的规则 leaf。"""
    if target_tokens <= 0:
        raise ValueError("fallback token 配置非法")
    index = {block.block_id: block.ordinal for block in blocks}
    start = index[candidate.start_block_id]
    end = index[candidate.end_block_id]
    result: list[SectionPlan] = []
    cursor = start
    while cursor <= end:
        stop = cursor
        tokens = 0
        while stop <= end:
            proposed = tokens + blocks[stop].token_count
            if stop > cursor and proposed > target_tokens:
                break
            tokens = proposed
            stop += 1
        first, last = blocks[cursor], blocks[stop - 1]
        text = source.text(first.start_char, last.end_char)
        result.append(
            SectionPlan(
                f"s{next_id + len(result):04d}",
                _fallback_title(text, first.start_line, last.end_line),
                "rule_fallback",
                cursor,
                stop - 1,
                first.start_char,
                last.end_char,
                first.start_line,
                last.end_line,
                "",
                None,
                1,
                0.0,
            )
        )
        cursor = stop
    return result


def apply_groups(
    sections: list[SectionPlan],
    response: dict[str, Any],
    max_depth: int = 3,
    min_confidence: float = 0.7,
    min_children: int = 2,
    max_children: int = 8,
) -> list[SectionPlan]:
    if not sections:
        return []
    groups = validate_group_response(
        response, [s.section_id for s in sections], min_confidence
    )
    result = list(sections)
    for group in groups:
        first = next(x for x in sections if x.section_id == group["start_section"])
        last = next(x for x in sections if x.section_id == group["end_section"])
        children = tuple(
            s.section_id
            for s in sections
            if first.start_block_ordinal <= s.start_block_ordinal
            and s.end_block_ordinal <= last.end_block_ordinal
        )
        if not min_children <= len(children) <= max_children:
            raise ValueError("group 子节点数超出限制")
        result.append(
            SectionPlan(
                f"s{len(result) + 1:04d}",
                group["title"].strip(),
                "llm",
                first.start_block_ordinal,
                last.end_block_ordinal,
                first.start_char,
                last.end_char,
                first.start_line,
                last.end_line,
                "",
                None,
                min(max_depth, first.depth + 1),
                float(group["confidence"]),
                children,
            )
        )
    parents = {c for s in result if s.child_ids for c in s.child_ids}
    return [
        SectionPlan(
            s.section_id,
            s.title,
            s.title_source,
            s.start_block_ordinal,
            s.end_block_ordinal,
            s.start_char,
            s.end_char,
            s.start_line,
            s.end_line,
            s.summary,
            next((p.section_id for p in result if s.section_id in p.child_ids), None),
            s.depth,
            s.confidence,
            s.child_ids,
        )
        if s.section_id in parents
        else s
        for s in result
    ]
