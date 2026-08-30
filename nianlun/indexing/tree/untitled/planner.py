"""LLM 优先的无标题目录规划器。

LLM 不可用、预算耗尽或局部规划持续不合规时，按 chunk 粒度降级为规则兜底
（``fallback_sections_from_candidate``），已完成的规划保留，不再整篇硬失败。
降级程度通过 ``UntitledTree.structure_mode`` 与 ``diagnostics`` 显式暴露。
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any, Protocol, cast

from json_repair import repair_json

from .blocks import parse_atomic_blocks
from .candidates import build_planning_chunks
from .models import PlanningChunk, PlannerConfig, UntitledStructureError, UntitledTree
from .prompts import (
    boundary_prompt,
    bridge_prompt,
    classification_prompt,
    grouping_prompt,
)
from .sections import (
    apply_groups,
    fallback_sections_from_candidate,
    merge_adjacent_sections,
    sections_from_candidate,
    sections_from_candidate_boundaries,
)
from .source import SourceView
from .validation import (
    validate_boundary_response,
    validate_bridge_response,
    validate_group_response,
    validate_section_tree,
    assign_own_content_spans,
    valid_basis_ids,
    valid_confidence,
    valid_title,
)


class LLMInvoker(Protocol):
    async def ainvoke(self, prompt: Any, **kwargs: Any) -> Any: ...


def _decode(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    text = getattr(value, "content", value)
    if isinstance(text, list):
        text = "".join(
            str(item.get("text", item)) if isinstance(item, dict) else str(item)
            for item in text
        )
    if not isinstance(text, str):
        raise ValueError("LLM 返回不是文本")
    cleaned = text.strip()
    if cleaned.startswith("```"):
        first_newline = cleaned.find("\n")
        if first_newline >= 0:
            cleaned = cleaned[first_newline + 1 :]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3].rstrip()
    data = repair_json(cleaned, return_objects=True)
    if not isinstance(data, dict):
        raise ValueError("LLM 返回 JSON 顶层必须是对象")
    return data


async def _call(
    llm: LLMInvoker,
    system: str,
    user: str,
    config: PlannerConfig,
    chunk_id: str | None = None,
    call_counter: list[int] | None = None,
) -> dict[str, Any]:
    last: Exception | None = None
    received_response = False
    for attempt in range(config.max_retries + 1):
        try:
            if call_counter is not None:
                if call_counter[0] >= config.max_llm_calls:
                    raise UntitledStructureError(
                        "UNTITLED_LLM_REQUIRED",
                        "LLM 调用预算耗尽",
                        chunk_id,
                    )
                call_counter[0] += 1
            response = await asyncio.wait_for(
                llm.ainvoke(system + "\n\n" + user, temperature=0),
                config.timeout_seconds,
            )
            received_response = True
            return _decode(response)
        except UntitledStructureError:
            raise
        except Exception as exc:
            last = exc
            if attempt < config.max_retries:
                await asyncio.sleep(0)
    code = (
        "UNTITLED_LLM_RESPONSE_INVALID"
        if received_response
        else "UNTITLED_LLM_REQUIRED"
    )
    message = "LLM 响应无法解析" if received_response else f"LLM 调用失败: {last}"
    raise UntitledStructureError(code, message, chunk_id) from last


async def _retry_local_plan(operation, config: PlannerConfig) -> dict[str, Any]:
    """重试可调用但语义不合规的局部规划；传输和 JSON 错误已由 _call 重试。"""
    last: UntitledStructureError | None = None
    for attempt in range(config.max_retries + 1):
        try:
            return await operation()
        except UntitledStructureError as exc:
            if exc.code in {"UNTITLED_LLM_REQUIRED", "UNTITLED_LLM_RESPONSE_INVALID"}:
                raise
            last = exc
            if attempt < config.max_retries:
                await asyncio.sleep(0)
    assert last is not None
    raise last


async def classify(
    llm: LLMInvoker,
    chunk: PlanningChunk,
    blocks: list[Any],
    config: PlannerConfig,
    call_counter: list[int] | None = None,
) -> dict[str, Any]:
    async def one() -> dict[str, Any]:
        data = await _call(
            llm,
            *classification_prompt(chunk, blocks),
            config,
            chunk.planning_chunk_id,
            call_counter,
        )
        if data.get("planning_chunk_id") != chunk.planning_chunk_id or data.get(
            "classification"
        ) not in {"single_topic", "mixed_topic"}:
            raise UntitledStructureError(
                "UNTITLED_LLM_PLAN_INVALID",
                "classification schema 非法",
                chunk.planning_chunk_id,
            )
        confidence = data.get("confidence")
        if not valid_confidence(confidence, config.min_confidence):
            raise UntitledStructureError(
                "UNTITLED_LLM_PLAN_INVALID",
                "classification 置信度不足",
                chunk.planning_chunk_id,
            )
        if data["classification"] == "single_topic" and (
            not valid_title(data.get("title"))
            or not valid_basis_ids(
                data.get("title_basis_block_ids"),
                {block.block_id for block in blocks},
            )
        ):
            raise UntitledStructureError(
                "UNTITLED_LLM_PLAN_INVALID",
                "single_topic 标题非法",
                chunk.planning_chunk_id,
            )
        return data

    return await _retry_local_plan(one, config)


async def boundaries(
    llm: LLMInvoker,
    chunk: PlanningChunk,
    blocks: list[Any],
    config: PlannerConfig,
    call_counter: list[int] | None = None,
) -> dict[str, Any]:
    async def one() -> dict[str, Any]:
        data = await _call(
            llm,
            *boundary_prompt(chunk, blocks),
            config,
            chunk.planning_chunk_id,
            call_counter,
        )
        try:
            return validate_boundary_response(data, chunk, config.min_confidence)
        except (TypeError, ValueError, KeyError, IndexError) as exc:
            raise UntitledStructureError(
                "UNTITLED_LLM_PLAN_INVALID", str(exc), chunk.planning_chunk_id
            ) from exc

    return await _retry_local_plan(one, config)


async def bridge(
    llm: LLMInvoker,
    left: PlanningChunk,
    right: PlanningChunk,
    left_blocks: list[Any],
    right_blocks: list[Any],
    config: PlannerConfig,
    call_counter: list[int] | None = None,
) -> dict[str, Any]:
    chunk_id = f"{left.planning_chunk_id}:{right.planning_chunk_id}"

    async def one() -> dict[str, Any]:
        data = await _call(
            llm,
            *bridge_prompt(left, right, left_blocks, right_blocks),
            config,
            chunk_id,
            call_counter,
        )
        try:
            return validate_bridge_response(
                data,
                left,
                right,
                config.min_confidence,
                {block.block_id for block in left_blocks + right_blocks},
            )
        except (TypeError, ValueError, KeyError, IndexError) as exc:
            raise UntitledStructureError(
                "UNTITLED_LLM_PLAN_INVALID", str(exc), chunk_id
            ) from exc

    return await _retry_local_plan(one, config)


async def groups(
    llm: LLMInvoker,
    sections: list[Any],
    depth: int,
    config: PlannerConfig,
    source: SourceView,
    call_counter: list[int] | None = None,
    blocks: list[Any] | None = None,
) -> list[dict[str, Any]]:
    data = await _call(
        llm,
        *grouping_prompt(sections, depth, source, config.model, blocks),
        config,
        call_counter=call_counter,
    )
    try:
        return validate_group_response(
            data, [section.section_id for section in sections], config.min_confidence
        )
    except (TypeError, ValueError, KeyError, IndexError) as exc:
        # Grouping is an optional optimization; existing valid leaf sections remain.
        raise UntitledStructureError("UNTITLED_LLM_PLAN_INVALID", str(exc)) from exc


def _rule_only_tree(
    source: SourceView,
    blocks: list[Any],
    config: PlannerConfig,
    diagnostic: str,
) -> UntitledTree:
    """无 LLM 时的纯规则兜底树：整篇按 target_tokens 切成可追踪的规则 leaf。"""
    chunk = PlanningChunk(
        "pc-rule",
        0,
        tuple(block.block_id for block in blocks),
        blocks[0].block_id,
        blocks[-1].block_id,
        blocks[0].start_char,
        blocks[-1].end_char,
        blocks[0].start_line,
        blocks[-1].end_line,
        source.text(blocks[0].start_char, blocks[-1].end_char),
        sum(block.token_count for block in blocks),
    )
    sections = fallback_sections_from_candidate(
        source, blocks, chunk, 1, config.target_tokens
    )
    try:
        validate_section_tree(sections, blocks, source.document.raw_markdown)
    except ValueError as exc:
        raise UntitledStructureError(
            "UNTITLED_TREE_VALIDATION_FAILED", str(exc)
        ) from exc
    sections = assign_own_content_spans(sections, source.document.raw_markdown)
    return UntitledTree(tuple(sections), (diagnostic,), "rule_fallback")


async def _plan_one_chunk(
    source: SourceView,
    blocks: list[Any],
    llm: LLMInvoker,
    chunk: PlanningChunk,
    config: PlannerConfig,
    call_counter: list[int],
) -> tuple[PlanningChunk, list[Any], list[Any], bool, str | None]:
    """阶段 1：单 chunk 的 classify/boundary 规划。

    任何失败（含 LLM 不可达、预算耗尽、规划持续不合规）都降级为规则兜底，
    不再向上抛出；返回 ``(chunk, chunk_blocks, sections, fell_back, diagnostic)``。
    section_id 为占位值，由阶段 2 结束后统一重排。
    """
    chunk_blocks = [block for block in blocks if block.block_id in chunk.block_ids]
    try:
        classification = await classify(llm, chunk, chunk_blocks, config, call_counter)
        if classification["classification"] == "single_topic":
            local_sections = [
                sections_from_candidate(
                    source,
                    blocks,
                    chunk,
                    classification["title"],
                    classification["confidence"],
                    "s0001",
                )
            ]
        else:
            boundary = await boundaries(llm, chunk, chunk_blocks, config, call_counter)
            first = boundary["first_section"]
            local_sections = sections_from_candidate_boundaries(
                source,
                blocks,
                chunk,
                first["title"],
                first["confidence"],
                boundary["decisions"],
                1,
            )
        return chunk, chunk_blocks, local_sections, False, None
    except UntitledStructureError as exc:
        local_sections = fallback_sections_from_candidate(
            source, blocks, chunk, 1, config.target_tokens
        )
        return (
            chunk,
            chunk_blocks,
            local_sections,
            True,
            f"{chunk.planning_chunk_id}: rule_fallback ({exc.code})",
        )


async def plan_untitled(
    source: SourceView,
    llm: LLMInvoker | None = None,
    config: PlannerConfig | None = None,
) -> UntitledTree:
    """生成无标题文档的 semantic section tree。

    LLM 未配置时整篇规则兜底（``structure_mode="rule_fallback"``）；规划过程中
    单 chunk 失败按 chunk 粒度降级并保留已完成部分（``"mixed"``），不再硬失败。
    仅在最终树校验失败（实现 bug）时抛 ``UntitledStructureError``。
    """
    config = config or PlannerConfig()
    if config.max_llm_calls <= 0:
        raise ValueError("LLM 调用预算配置非法")
    if config.bridge_context_blocks <= 0:
        raise ValueError("bridge context block 配置非法")
    blocks = parse_atomic_blocks(source, config.model, config.max_atomic_tokens)
    if not blocks:
        return UntitledTree(())
    if llm is None:
        return _rule_only_tree(
            source,
            blocks,
            config,
            "UNTITLED_LLM_REQUIRED: 无标题 Markdown 未配置 LLM，整篇规则兜底",
        )
    planning_chunks = build_planning_chunks(
        source, blocks, config.target_tokens, config.max_candidate_tokens, config.model
    )
    # 预算随文档长度动态放大：每 chunk 约 classify+boundary+bridge 3 次调用，
    # 再留 1 次重试余量；config.max_llm_calls 作为短文档的下限。
    config = replace(
        config,
        max_llm_calls=max(config.max_llm_calls, 4 * len(planning_chunks) + 4),
    )
    diagnostics: list[str] = []
    call_counter = [0]
    planning_semaphore = asyncio.Semaphore(8)

    async def plan_chunk(chunk: PlanningChunk):
        async with planning_semaphore:
            return await _plan_one_chunk(
                source, blocks, llm, chunk, config, call_counter
            )

    # 阶段 1：各 chunk 的 classify/boundary 互相独立，并发规划。
    results = await asyncio.gather(*(plan_chunk(chunk) for chunk in planning_chunks))

    # 阶段 2：bridge 依赖相邻 chunk 的规划结果，串行衔接。任一侧已规则兜底时
    # 直接保留边界：弱证据不值得再花调用做合并判定。
    sections: list[Any] = []
    previous: tuple[PlanningChunk, list[Any], bool] | None = None
    for chunk, chunk_blocks, local_sections, fell_back, diagnostic in results:
        if diagnostic:
            diagnostics.append(diagnostic)
        if previous is not None:
            previous_chunk, previous_blocks, previous_fell_back = previous
            if fell_back or previous_fell_back:
                diagnostics.append(
                    f"{previous_chunk.planning_chunk_id}:{chunk.planning_chunk_id}: "
                    "preserve_boundary (rule_fallback_side)"
                )
                decision = {"boundary": True}
            else:
                context_size = config.bridge_context_blocks
                try:
                    decision = await bridge(
                        llm,
                        previous_chunk,
                        chunk,
                        previous_blocks[-context_size:],
                        chunk_blocks[:context_size],
                        config,
                        call_counter,
                    )
                except UntitledStructureError:
                    try:
                        decision = await bridge(
                            llm,
                            previous_chunk,
                            chunk,
                            previous_blocks,
                            chunk_blocks,
                            config,
                            call_counter,
                        )
                    except UntitledStructureError:
                        diagnostics.append(
                            f"{previous_chunk.planning_chunk_id}:{chunk.planning_chunk_id}: "
                            "preserve_boundary (bridge_fallback)"
                        )
                        decision = {"boundary": True}
            if not decision["boundary"]:
                sections[-1] = merge_adjacent_sections(
                    sections[-1],
                    local_sections[0],
                    cast(str, decision["title"]),
                    cast(float, decision["confidence"]),
                )
                local_sections = local_sections[1:]
        sections.extend(local_sections)
        previous = (chunk, chunk_blocks, fell_back)

    # 阶段 1 并发期间 section_id 为占位值，按文档顺序统一重排。
    sections = [
        replace(section, section_id=f"s{index:04d}")
        for index, section in enumerate(sections, 1)
    ]

    if len(sections) >= 2 and config.max_depth > 1:
        try:
            grouped = await groups(
                llm, sections, 2, config, source, call_counter, blocks
            )
            if grouped:
                sections = apply_groups(
                    sections,
                    {"groups": grouped},
                    config.max_depth,
                    config.min_confidence,
                    config.min_group_children,
                    config.max_group_children,
                )
        except (UntitledStructureError, ValueError):
            # Grouping is an optional optimization; existing valid leaf sections remain.
            pass
    try:
        validate_section_tree(sections, blocks, source.document.raw_markdown)
    except ValueError as exc:
        raise UntitledStructureError(
            "UNTITLED_TREE_VALIDATION_FAILED", str(exc)
        ) from exc
    sections = assign_own_content_spans(sections, source.document.raw_markdown)
    fell_back_chunks = sum(1 for result in results if result[3])
    if fell_back_chunks == 0:
        structure_mode = "llm"
    elif fell_back_chunks == len(results):
        structure_mode = "rule_fallback"
    else:
        structure_mode = "mixed"
    return UntitledTree(tuple(sections), tuple(diagnostics), structure_mode)


def plan_untitled_sync(
    source: SourceView,
    llm: LLMInvoker | None = None,
    config: PlannerConfig | None = None,
) -> UntitledTree:
    return asyncio.run(plan_untitled(source, llm, config))
