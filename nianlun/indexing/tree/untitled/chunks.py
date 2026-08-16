"""planning chunk 的 LLM 输入序列化工具；不生成最终检索节点。"""

from __future__ import annotations

import json
from typing import Any

from .models import AtomicBlock, PlanningChunk


def planning_chunk_blocks(
    chunk: PlanningChunk, blocks: list[AtomicBlock]
) -> list[dict[str, Any]]:
    allowed = set(chunk.block_ids)
    selected = [block for block in blocks if block.block_id in allowed]
    if tuple(block.block_id for block in selected) != chunk.block_ids:
        raise ValueError("planning chunk 的 block 序列不连续")
    return [
        {
            "block_id": b.block_id,
            "start_line": b.start_line,
            "end_line": b.end_line,
            "block_type": b.block_type,
            "text": b.text,
        }
        for b in selected
    ]


def planning_chunk_json(chunk: PlanningChunk, blocks: list[AtomicBlock]) -> str:
    return json.dumps(
        planning_chunk_blocks(chunk, blocks), ensure_ascii=False, separators=(",", ":")
    )
