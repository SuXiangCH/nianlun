"""无标题目录生成所需的结构化 LLM 提示词。"""

from __future__ import annotations

import json
from typing import Any

from nianlun.indexing.tree.llm import count_tokens

CLASSIFY_SYSTEM = """你是长文档结构分析器。

原始 Markdown 是只读证据，不能改写正文、行号或创建 ID。
判断候选片段是 single_topic 或 mixed_topic。
只有主题确实切换才选择 mixed_topic；格式变化不是充分理由。
若局部证据不足以证明存在主题切换，选择 single_topic，不要输出第三种状态。
single_topic 必须给出不超过 30 字的描述性标题。
标题必须由 title_basis_block_ids 指向的输入 block 支持。
只返回合法 JSON。"""

BOUNDARY_SYSTEM = """你负责分析无标题 Markdown 的主题边界。

只能在输入 block 之间判断边界，只能引用已有 block_id；
不能生成正文、行号或新 ID。
同一主题的解释、例子、数据和结论应保持在一起。
只输出真正的边界，每个 decision 必须 boundary=true。
boundary=true 必须给出不超过 30 字的描述性标题，并通过 title_basis_block_ids 指向新 section 的输入 block。
首 section 必须通过 title_basis_block_ids 指向首 section 的输入 block。
只返回合法 JSON。"""

BRIDGE_SYSTEM = """你负责确认相邻两个规划窗口交界处的主题连续性。

输入内容是同一原始文档中连续的前后上下文。规划窗口不是最终章节边界。
只有主题确实切换时才返回 boundary=true；格式变化、窗口结束或段落结束都不是充分理由。
boundary=false 表示前后内容属于同一个 section，必须给出覆盖两侧内容的不超过 30 字标题。
合并标题必须通过 title_basis_block_ids 指向输入的证据 block。
不能改写正文、行号或创建 ID。只返回合法 JSON。"""

GROUP_SYSTEM = """你是文档目录设计器。

将按源码顺序排列的 section 合并为连续导航分组。
不能重排、遗漏或重复 section；不要为了增加层级而分组。
title 必须是输入内容支持的不超过 30 字的描述性标题。
每个分组标题必须通过 basis_section_ids 指向该分组范围内的输入 section。
没有可靠合并关系时返回空 groups。
只返回合法 JSON。"""


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def classification_prompt(candidate: Any, blocks: list[Any]) -> tuple[str, str]:
    payload = [
        {
            "block_id": b.block_id,
            "start_line": b.start_line,
            "end_line": b.end_line,
            "block_type": b.block_type,
            "text": b.text,
        }
        for b in blocks
    ]
    user = f"""规划块 ID：{candidate.planning_chunk_id}

原子块：
{_json(payload)}

请严格返回 JSON：
{{
  "planning_chunk_id": "{candidate.planning_chunk_id}",
  "classification": "single_topic|mixed_topic",
  "title": "",
  "title_basis_block_ids": [],
  "confidence": 0.0
}}"""
    return CLASSIFY_SYSTEM, user


def boundary_prompt(candidate: Any, blocks: list[Any]) -> tuple[str, str]:
    payload = [
        {
            "block_id": b.block_id,
            "start_line": b.start_line,
            "end_line": b.end_line,
            "block_type": b.block_type,
            "text": b.text,
        }
        for b in blocks
    ]
    return (
        BOUNDARY_SYSTEM,
        f"""规划块 ID：{candidate.planning_chunk_id}

候选原子块：
{_json(payload)}

请严格返回 JSON：
{{
  "planning_chunk_id": "{candidate.planning_chunk_id}",
  "valid": true,
  "first_section": {{
    "title": "",
    "title_basis_block_ids": [],
    "confidence": 0.0
  }},
  "decisions": [
    {{
      "after_block": "已有 block_id",
      "boundary": true,
      "title": "",
      "title_basis_block_ids": [],
      "confidence": 0.0
    }}
  ]
}}""",
    )


def bridge_prompt(
    left: Any, right: Any, left_blocks: list[Any], right_blocks: list[Any]
) -> tuple[str, str]:
    def payload(blocks: list[Any]) -> list[dict[str, Any]]:
        return [
            {
                "block_id": block.block_id,
                "start_line": block.start_line,
                "end_line": block.end_line,
                "block_type": block.block_type,
                "text": block.text,
            }
            for block in blocks
        ]

    return (
        BRIDGE_SYSTEM,
        f"""左规划块 ID：{left.planning_chunk_id}
右规划块 ID：{right.planning_chunk_id}

左侧尾部上下文：
{_json(payload(left_blocks))}

右侧首部上下文：
{_json(payload(right_blocks))}

请严格返回 JSON：
{{
  "left_planning_chunk_id": "{left.planning_chunk_id}",
  "right_planning_chunk_id": "{right.planning_chunk_id}",
  "boundary": true,
  "title": "",
  "title_basis_block_ids": [],
  "confidence": 0.0
}}""",
    )


def grouping_prompt(
    sections: list[Any],
    target_depth: int,
    source: Any | None = None,
    model: str | None = None,
    blocks: list[Any] | None = None,
) -> tuple[str, str]:
    def evidence(section: Any) -> str:
        if source is None:
            return section.summary
        raw = source.text(section.start_char, section.end_char)
        if len(raw) <= 1600:
            return raw
        return raw[:800] + "\n...[中间内容省略，仅用于标题证据]...\n" + raw[-800:]

    def token_count(section: Any) -> int | None:
        if source is None:
            return None
        if blocks is not None:
            # 复用 block 自带计数求和，避免对每个 section 重新 tokenize 全文。
            return sum(
                block.token_count
                for block in blocks
                if section.start_block_ordinal
                <= block.ordinal
                <= section.end_block_ordinal
            )
        return count_tokens(source.text(section.start_char, section.end_char), model)

    payload = [
        {
            "section_id": s.section_id,
            "title": s.title,
            "summary": s.summary,
            "start_line": s.start_line,
            "end_line": s.end_line,
            "token_count": token_count(s),
            "evidence": evidence(s),
        }
        for s in sections
    ]
    return (
        GROUP_SYSTEM,
        f"""目标层级：{target_depth}

输入章节：
{_json(payload)}

请严格返回 JSON：
{{
  "groups": [
    {{
      "start_section": "已有 section_id",
      "end_section": "已有 section_id",
      "title": "",
      "basis_section_ids": [],
      "confidence": 0.0
    }}
  ]
}}""",
    )
