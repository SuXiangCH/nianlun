"""无标题语义目录规划器的结构化响应回归测试。"""

from __future__ import annotations

import asyncio
import re

import pytest

from nianlun.indexing.tree.untitled.blocks import parse_atomic_blocks
from nianlun.indexing.tree.untitled.candidates import build_planning_chunks
from nianlun.indexing.tree.untitled.models import PlannerConfig, SectionPlan
from nianlun.indexing.tree.untitled.planner import _decode, plan_untitled
from nianlun.indexing.tree.untitled.prompts import grouping_prompt
from nianlun.indexing.tree.untitled.source import make_source
from nianlun.indexing.tree.untitled.validation import (
    valid_confidence,
    validate_boundary_response,
    validate_group_response,
)


def _chunk():
    source = make_source("untitled-test", "第一部分内容。\n\n第二部分内容。")
    blocks = parse_atomic_blocks(source)
    return source, blocks, build_planning_chunks(source, blocks)[0]


def test_decode_repairs_json_response():
    assert _decode("```json\n{'planning_chunk_id': 'pc-0001',}\n```") == {
        "planning_chunk_id": "pc-0001"
    }


def test_mixed_topic_rejects_empty_boundary_decisions():
    _source, blocks, chunk = _chunk()
    with pytest.raises(ValueError, match="至少包含一个主题边界"):
        validate_boundary_response(
            {
                "planning_chunk_id": chunk.planning_chunk_id,
                "valid": True,
                "first_section": {
                    "title": "第一部分",
                    "title_basis_block_ids": [blocks[0].block_id],
                    "confidence": 0.9,
                },
                "decisions": [],
            },
            chunk,
        )


def test_boundary_requires_typed_confidence_and_evidence():
    _source, blocks, chunk = _chunk()
    with pytest.raises(ValueError):
        validate_boundary_response(
            {
                "planning_chunk_id": chunk.planning_chunk_id,
                "valid": True,
                "first_section": {
                    "title": "第一部分",
                    "title_basis_block_ids": [blocks[0].block_id],
                    "confidence": "high",
                },
                "decisions": [
                    {
                        "after_block": blocks[0].block_id,
                        "boundary": True,
                        "title": "第二部分",
                        "title_basis_block_ids": [blocks[1].block_id],
                        "confidence": 0.9,
                    }
                ],
            },
            chunk,
        )


def test_confidence_rejects_bool_and_non_finite_values():
    assert not valid_confidence(True, 0.7)
    assert not valid_confidence(float("nan"), 0.7)
    assert not valid_confidence(float("inf"), 0.7)
    assert not valid_confidence(1.1, 0.7)
    assert valid_confidence(0.7, 0.7)


def test_group_requires_in_range_evidence_and_receives_source_text():
    source, blocks, _chunk_value = _chunk()
    sections = [
        SectionPlan(
            "s0001",
            "第一部分",
            start_block_ordinal=0,
            end_block_ordinal=0,
            start_char=blocks[0].start_char,
            end_char=blocks[0].end_char,
            start_line=blocks[0].start_line,
            end_line=blocks[0].end_line,
        ),
        SectionPlan(
            "s0002",
            "第二部分",
            start_block_ordinal=1,
            end_block_ordinal=1,
            start_char=blocks[1].start_char,
            end_char=blocks[1].end_char,
            start_line=blocks[1].start_line,
            end_line=blocks[1].end_line,
        ),
    ]
    _system, prompt = grouping_prompt(sections, 2, source)
    assert "第一部分内容" in prompt
    with pytest.raises(ValueError, match="证据"):
        validate_group_response(
            {
                "groups": [
                    {
                        "start_section": "s0001",
                        "end_section": "s0002",
                        "title": "全文",
                        "basis_section_ids": ["s9999"],
                        "confidence": 0.9,
                    }
                ]
            },
            ["s0001", "s0002"],
        )


class _MalformedBoundaryLLM:
    async def ainvoke(self, prompt, **_kwargs):
        if "候选原子块：" in prompt:
            return {
                "planning_chunk_id": "pc-0001",
                "valid": True,
                "first_section": {
                    "title": "第一部分",
                    "title_basis_block_ids": ["b0001"],
                    "confidence": "high",
                },
                "decisions": [],
            }
        return {
            "planning_chunk_id": "pc-0001",
            "classification": "mixed_topic",
            "confidence": 0.9,
        }


def test_malformed_boundary_falls_back_instead_of_raising_type_error():
    tree = asyncio.run(
        plan_untitled(
            make_source("untitled-test", "第一部分内容。\n\n第二部分内容。"),
            _MalformedBoundaryLLM(),
            PlannerConfig(max_retries=0),
        )
    )
    assert tree.sections[0].title_source == "rule_fallback"
    assert tree.diagnostics == ("pc-0001: rule_fallback (UNTITLED_LLM_PLAN_INVALID)",)


# ============ 降级路径（rule_fallback / mixed / 动态预算 / temperature） ============


def _prompt_block_ids(prompt: str) -> list[str]:
    return re.findall(r'"block_id":"(b\d+)"', prompt)


class ScriptedPlannerLLM:
    """可按脚本响应四类规划 prompt 的 mock；``fail_after`` 之后所有调用抛错。"""

    def __init__(self, fail_after: int | None = None, groups: list | None = None):
        self.calls = 0
        self.fail_after = fail_after
        self.groups = groups or []
        self.kwargs: list[dict] = []

    async def ainvoke(self, prompt, **kwargs):
        self.calls += 1
        self.kwargs.append(kwargs)
        if self.fail_after is not None and self.calls > self.fail_after:
            raise ConnectionError("LLM unreachable")
        if "候选原子块：" in prompt:
            raise AssertionError("测试不应触发 boundary 规划")
        if "左规划块 ID：" in prompt:
            left = re.search(r"左规划块 ID：(pc-\d+)", prompt).group(1)
            right = re.search(r"右规划块 ID：(pc-\d+)", prompt).group(1)
            return {
                "left_planning_chunk_id": left,
                "right_planning_chunk_id": right,
                "boundary": True,
                "confidence": 0.9,
            }
        if "输入章节" in prompt:
            return {"groups": self.groups}
        chunk_id = re.search(r"规划块 ID：(pc-\d+)", prompt).group(1)
        return {
            "planning_chunk_id": chunk_id,
            "classification": "single_topic",
            "title": f"语义标题-{chunk_id}",
            "title_basis_block_ids": _prompt_block_ids(prompt)[:1],
            "confidence": 0.9,
        }


def _multi_chunk_source(paragraphs: int = 3) -> tuple:
    raw = "\n\n".join(f"第{i}段正文内容。" for i in range(1, paragraphs + 1))
    source = make_source("untitled-test", raw)
    return source, raw


def test_plan_untitled_without_llm_returns_rule_only_tree():
    source, raw = _multi_chunk_source()
    tree = asyncio.run(plan_untitled(source, None, PlannerConfig(max_retries=0)))
    assert tree.structure_mode == "rule_fallback"
    assert any("UNTITLED_LLM_REQUIRED" in d for d in tree.diagnostics)
    assert tree.sections
    assert all(s.title_source == "rule_fallback" for s in tree.sections)
    # 规则兜底树仍连续覆盖全部正文
    assert raw[tree.sections[0].start_char : tree.sections[-1].end_char] == raw


def test_llm_outage_falls_back_per_chunk_and_preserves_completed():
    source, _raw = _multi_chunk_source(paragraphs=3)
    config = PlannerConfig(max_retries=0, target_tokens=5, max_candidate_tokens=10)
    llm = ScriptedPlannerLLM(fail_after=1)  # 仅第一个 chunk 的 classify 成功
    tree = asyncio.run(plan_untitled(source, llm, config))
    assert tree.structure_mode == "mixed"
    assert tree.sections[0].title_source == "llm"
    assert tree.sections[0].title.startswith("语义标题-")
    assert any(s.title_source == "rule_fallback" for s in tree.sections[1:])
    assert any("rule_fallback" in d for d in tree.diagnostics)


def test_llm_fully_unreachable_returns_rule_fallback_mode():
    source, raw = _multi_chunk_source(paragraphs=2)
    config = PlannerConfig(max_retries=0, target_tokens=5, max_candidate_tokens=10)
    llm = ScriptedPlannerLLM(fail_after=0)
    tree = asyncio.run(plan_untitled(source, llm, config))
    assert tree.structure_mode == "rule_fallback"
    assert all(s.title_source == "rule_fallback" for s in tree.sections)
    assert raw[tree.sections[0].start_char : tree.sections[-1].end_char] == raw


def test_llm_budget_scales_with_chunk_count():
    # max_llm_calls=1 仅是短文档预算下限；动态预算随 chunk 数放大，3 个 chunk 应全部成功
    source, _raw = _multi_chunk_source(paragraphs=3)
    config = PlannerConfig(
        max_retries=0, target_tokens=5, max_candidate_tokens=10, max_llm_calls=1
    )
    tree = asyncio.run(plan_untitled(source, ScriptedPlannerLLM(), config))
    assert tree.structure_mode == "llm"
    assert all(s.title_source == "llm" for s in tree.sections)


def test_grouping_creates_parent_with_empty_own_content_spans():
    source, _raw = _multi_chunk_source(paragraphs=2)
    config = PlannerConfig(max_retries=0, target_tokens=5, max_candidate_tokens=10)
    groups = [
        {
            "start_section": "s0001",
            "end_section": "s0002",
            "title": "全文",
            "basis_section_ids": ["s0001"],
            "confidence": 0.9,
        }
    ]
    tree = asyncio.run(plan_untitled(source, ScriptedPlannerLLM(groups=groups), config))
    assert tree.structure_mode == "llm"
    parents = [s for s in tree.sections if s.child_ids]
    assert len(parents) == 1
    assert parents[0].title == "全文"
    # 两个子 section 之间只有空白，父节点无自有正文
    assert parents[0].own_content_spans == ()


def test_planner_calls_use_zero_temperature():
    source, _raw = _multi_chunk_source(paragraphs=2)
    config = PlannerConfig(max_retries=0, target_tokens=5, max_candidate_tokens=10)
    llm = ScriptedPlannerLLM()
    asyncio.run(plan_untitled(source, llm, config))
    assert llm.kwargs, "mock 应至少收到一次调用"
    assert all(kwargs.get("temperature") == 0 for kwargs in llm.kwargs)
