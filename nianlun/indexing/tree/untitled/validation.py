"""LLM 响应、源码范围和最终 section 树的确定性校验。"""

from __future__ import annotations

import math
import re
from dataclasses import replace
from typing import Any

from .models import AtomicBlock, PlanningChunk, SectionPlan


def valid_title(title: Any, max_chars: int = 30) -> bool:
    return (
        isinstance(title, str)
        and bool(title.strip())
        and not any(char in title for char in "\r\n")
        and len(title.strip()) <= max_chars
        and not re.fullmatch(
            r"(无标题|其他内容|补充说明|未分类)(片段)?[\s\d-]*", title.strip()
        )
    )


def valid_confidence(value: Any, minimum: float = 0.0) -> bool:
    """Validate a model confidence without accepting bool/NaN as numbers."""
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and minimum <= value <= 1.0
    )


def valid_basis_ids(value: Any, allowed: set[str]) -> bool:
    return (
        isinstance(value, list)
        and bool(value)
        and all(isinstance(item, str) and item in allowed for item in value)
        and len(value) == len(set(value))
    )


def validate_boundary_response(
    data: Any, chunk: PlanningChunk, min_confidence: float = 0.7
) -> dict[str, Any]:
    if (
        not isinstance(data, dict)
        or data.get("planning_chunk_id") != chunk.planning_chunk_id
        or data.get("valid") is not True
        or not isinstance(data.get("decisions"), list)
    ):
        raise ValueError("非法 boundary response")
    first = data.get("first_section")
    if (
        not isinstance(first, dict)
        or not valid_title(first.get("title"))
        or not valid_confidence(first.get("confidence"), min_confidence)
    ):
        raise ValueError("boundary 缺少有效首 section")
    block_order = {block_id: index for index, block_id in enumerate(chunk.block_ids)}
    seen: set[str] = set()
    accepted: list[dict[str, Any]] = []
    for item in data["decisions"]:
        if not isinstance(item, dict) or item.get("boundary") is not True:
            raise ValueError("boundary decision 必须明确声明 boundary=true")
        after, confidence = item.get("after_block"), item.get("confidence", 0)
        if (
            not isinstance(after, str)
            or after not in set(chunk.block_ids)
            or after in seen
            or block_order[after] >= len(chunk.block_ids) - 1
            or not valid_confidence(confidence, min_confidence)
            or not valid_title(item.get("title"))
        ):
            raise ValueError("非法 boundary decision")
        basis = item.get("title_basis_block_ids")
        if not valid_basis_ids(
            basis,
            set(chunk.block_ids[block_order[after] + 1 :]),
        ):
            raise ValueError("boundary 标题缺少当前 section 的证据 block")
        seen.add(after)
        accepted.append(item)
    if not accepted:
        raise ValueError("mixed_topic 必须至少包含一个主题边界")
    first_cut = min(block_order[item["after_block"]] for item in accepted)
    if not valid_basis_ids(
        first.get("title_basis_block_ids"), set(chunk.block_ids[: first_cut + 1])
    ):
        raise ValueError("首 section 标题缺少当前 section 的证据 block")
    return {"first_section": first, "decisions": accepted}


def validate_bridge_response(
    data: Any,
    left: PlanningChunk,
    right: PlanningChunk,
    min_confidence: float = 0.7,
    basis_block_ids: set[str] | None = None,
) -> dict[str, Any]:
    if (
        not isinstance(data, dict)
        or data.get("left_planning_chunk_id") != left.planning_chunk_id
        or data.get("right_planning_chunk_id") != right.planning_chunk_id
        or not isinstance(data.get("boundary"), bool)
        or not valid_confidence(data.get("confidence"), min_confidence)
    ):
        raise ValueError("非法 bridge response")
    if not data["boundary"]:
        if not valid_title(data.get("title")):
            raise ValueError("连续 section 缺少有效标题")
        allowed = basis_block_ids or set(left.block_ids) | set(right.block_ids)
        if not valid_basis_ids(data.get("title_basis_block_ids"), allowed):
            raise ValueError("连续 section 标题缺少证据 block")
    return data


def validate_group_response(
    data: Any, section_ids: list[str], min_confidence: float = 0.7
) -> list[dict[str, Any]]:
    if not isinstance(data, dict) or not isinstance(data.get("groups"), list):
        raise ValueError("非法 group response")
    order = {x: i for i, x in enumerate(section_ids)}
    accepted = []
    intervals: list[tuple[int, int]] = []
    for group in data["groups"]:
        if (
            not isinstance(group, dict)
            or not valid_title(group.get("title"))
            or not isinstance(group.get("start_section"), str)
            or not isinstance(group.get("end_section"), str)
        ):
            raise ValueError("非法 group")
        start_section = group["start_section"]
        end_section = group["end_section"]
        if start_section not in order or end_section not in order:
            raise ValueError("非法 group section 引用")
        if (
            order[start_section] > order[end_section]
            or not isinstance(group.get("basis_section_ids"), list)
            or not valid_confidence(group.get("confidence"), min_confidence)
        ):
            raise ValueError("group 非连续或置信度不足")
        interval = (order[start_section], order[end_section])
        basis = group["basis_section_ids"]
        if (
            not basis
            or any(not isinstance(item, str) or item not in order for item in basis)
            or len(basis) != len(set(basis))
            or any(not interval[0] <= order[item] <= interval[1] for item in basis)
        ):
            raise ValueError("group 标题缺少范围内的证据 section")
        if any(
            not (interval[1] < start or interval[0] > end) for start, end in intervals
        ):
            raise ValueError("group 范围重叠")
        intervals.append(interval)
        accepted.append(group)
    return accepted


def validate_section_tree(
    sections: list[SectionPlan], blocks: list[AtomicBlock], raw: str
) -> None:
    leaves = sorted(
        (s for s in sections if not s.child_ids), key=lambda s: s.start_char
    )
    if not leaves and raw.strip():
        raise ValueError("没有 leaf section")
    previous_end = leaves[0].start_char if leaves else 0
    for section in leaves:
        gap = raw[previous_end : section.start_char]
        if (
            not valid_title(section.title)
            or section.title_source not in {"llm", "rule_fallback"}
            or section.start_char < previous_end
            or gap.strip()
            or section.end_char <= section.start_char
            or raw[section.start_char : section.end_char] == ""
        ):
            raise ValueError("leaf section 标题或范围非法")
        previous_end = section.end_char
    if raw[previous_end:].strip():
        raise ValueError("leaf section 未覆盖全部正文")
    for section in sections:
        if section.child_ids:
            children = [s for s in sections if s.section_id in section.child_ids]
            if (
                len(children) != len(section.child_ids)
                or not children
                or min(s.start_char for s in children) < section.start_char
                or max(s.end_char for s in children) > section.end_char
            ):
                raise ValueError("parent child 范围非法")


def assign_own_content_spans(
    sections: list[SectionPlan], raw: str
) -> list[SectionPlan]:
    """为每个 section 计算不属于其后代的源码片段。

    父节点的完整 span 仍然保留，用于导航和范围定位；该函数只额外标记
    parent span 中未被直接子节点覆盖的非空白片段。
    """
    by_id = {section.section_id: section for section in sections}
    result: list[SectionPlan] = []
    for section in sections:
        if not section.child_ids:
            spans = ((section.start_char, section.end_char),)
        else:
            children = sorted(
                (by_id[child_id] for child_id in section.child_ids),
                key=lambda child: child.start_char,
            )
            gaps: list[tuple[int, int]] = []
            cursor = section.start_char
            for child in children:
                if cursor < child.start_char:
                    gap_start, gap_end = cursor, child.start_char
                    while gap_start < gap_end and raw[gap_start].isspace():
                        gap_start += 1
                    while gap_end > gap_start and raw[gap_end - 1].isspace():
                        gap_end -= 1
                    if gap_start < gap_end:
                        gaps.append((gap_start, gap_end))
                cursor = max(cursor, child.end_char)
            if cursor < section.end_char:
                gap_start, gap_end = cursor, section.end_char
                while gap_start < gap_end and raw[gap_start].isspace():
                    gap_start += 1
                while gap_end > gap_start and raw[gap_end - 1].isspace():
                    gap_end -= 1
                if gap_start < gap_end:
                    gaps.append((gap_start, gap_end))
            spans = tuple(gaps)
        result.append(replace(section, own_content_spans=spans))
    return result


def validate_blocks(blocks: list[AtomicBlock], raw: str) -> None:
    previous_end = 0
    for block in blocks:
        if (
            block.start_char < previous_end
            or block.text != raw[block.start_char : block.end_char]
        ):
            raise ValueError(f"非法 block: {block.block_id}")
        previous_end = block.end_char
