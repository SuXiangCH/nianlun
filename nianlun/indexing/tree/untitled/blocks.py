"""从 Markdown 原文解析保留精确源码范围的 atomic block。"""

from __future__ import annotations

import re

from markdown_it import MarkdownIt

from nianlun.indexing.tree.llm import count_tokens

from .models import AtomicBlock, BlockType
from .source import SourceView


_SENTENCE_END = re.compile(r"[。！？!?；;]\s*|[.!?]\s+(?=[\u4e00-\u9fffA-Za-z0-9])")


def _split_oversized_span(
    source: SourceView, start: int, end: int, max_tokens: int, model: str | None
) -> list[tuple[int, int]]:
    """按可解释边界拆分超大的可分割源码块。

    这些片段只是 LLM 规划边界的本地参照，不是最终检索分片。
    优先使用行和句子边界，无法找到时才使用 token 预算对应的字符位置。
    """
    raw = source.text(start, end)
    if count_tokens(raw, model) <= max_tokens:
        return [(start, end)]

    boundaries = {end}
    for line_start in source.line_starts:
        if start < line_start < end:
            boundaries.add(line_start)
    for match in _SENTENCE_END.finditer(raw):
        boundary = start + match.end()
        if start < boundary < end:
            boundaries.add(boundary)
    ordered = sorted(boundaries)
    spans: list[tuple[int, int]] = []
    cursor = start
    while cursor < end:
        remaining = source.text(cursor, end)
        if count_tokens(remaining, model) <= max_tokens:
            spans.append((cursor, end))
            break

        low, high = cursor + 1, end
        best = cursor + 1
        while low <= high:
            middle = (low + high) // 2
            if count_tokens(source.text(cursor, middle), model) <= max_tokens:
                best = middle
                low = middle + 1
            else:
                high = middle - 1
        eligible = [boundary for boundary in ordered if cursor < boundary <= best]
        boundary = max(eligible, default=best)
        if boundary <= cursor:
            boundary = min(end, cursor + 1)
        spans.append((cursor, boundary))
        cursor = boundary
    return spans


def _kind(token_type: str) -> tuple[BlockType, bool]:
    if token_type in {"fence", "code_block"}:
        return "code", False
    if token_type in {"bullet_list_open", "ordered_list_open", "list_item_open"}:
        return "list", True
    if token_type == "table_open":
        return "table", False
    if token_type in {"blockquote_open"}:
        return "blockquote", True
    if token_type in {"hr"}:
        return "thematic_break", False
    if token_type in {"html_block"}:
        return "html", False
    if token_type == "paragraph_open":
        return "paragraph", True
    return "paragraph", True


def parse_atomic_blocks(
    source: SourceView, model: str | None = None, max_tokens: int = 1200
) -> list[AtomicBlock]:
    if max_tokens <= 0:
        raise ValueError("atomic block token 配置非法")
    text = source.document.raw_markdown
    if not text.strip():
        return []
    tokens = MarkdownIt("commonmark").parse(text)
    spans: list[tuple[int, int, BlockType, bool]] = []
    for token in tokens:
        # Only top-level block tokens are planning units. Child tokens would
        # duplicate the container's source range (especially list/table items).
        if token.level != 0:
            continue
        if not token.map or token.type in {
            "inline",
            "paragraph_close",
            "list_item_close",
            "blockquote_close",
            "table_close",
        }:
            continue
        kind, splittable = _kind(token.type)
        start_line, end_line = token.map
        # markdown-it maps are source line indices; include the mapped lines.
        starts = source.line_starts
        start = starts[start_line]
        end = starts[end_line] if end_line < len(starts) else len(text)
        if end <= start or not text[start:end].strip():
            continue
        if splittable:
            for child_start, child_end in _split_oversized_span(
                source, start, end, max_tokens, model
            ):
                spans.append((child_start, child_end, kind, splittable))
        else:
            spans.append((start, end, kind, splittable))
    result: list[AtomicBlock] = []
    for ordinal, (start, end, kind, splittable) in enumerate(spans):
        line_start, line_end = source.span_lines(start, end)
        raw = text[start:end]
        result.append(
            AtomicBlock(
                f"b{ordinal + 1:04d}",
                ordinal,
                kind,
                start,
                end,
                line_start,
                line_end,
                raw,
                count_tokens(raw, model),
                splittable,
                __import__("hashlib").sha256(raw.encode()).hexdigest(),
            )
        )
    return result
