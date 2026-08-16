"""原始 Markdown 的只读视图和 newline-aware 源码范围工具。"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from .models import SourceDocument


@dataclass(frozen=True, slots=True)
class SourceView:
    document: SourceDocument
    line_starts: tuple[int, ...]

    def line_for_offset(self, offset: int) -> int:
        if not 0 <= offset <= len(self.document.raw_markdown):
            raise ValueError("字符 offset 超出文档范围")
        import bisect

        return bisect.bisect_right(self.line_starts, offset)

    def span_lines(self, start: int, end: int) -> tuple[int, int]:
        if not 0 <= start < end <= len(self.document.raw_markdown):
            raise ValueError("非法源码 span")
        return self.line_for_offset(start), self.line_for_offset(end - 1)

    def text(self, start: int, end: int) -> str:
        if not 0 <= start <= end <= len(self.document.raw_markdown):
            raise ValueError("非法源码 span")
        return self.document.raw_markdown[start:end]


def _line_starts(text: str) -> tuple[int, ...]:
    starts = [0]
    i = 0
    while i < len(text):
        if text[i] == "\r":
            i += 2 if i + 1 < len(text) and text[i + 1] == "\n" else 1
            starts.append(i)
        elif text[i] == "\n":
            i += 1
            starts.append(i)
        else:
            i += 1
    return tuple(starts)


def make_source(
    document_id: str, raw_markdown: str, source_path: str | None = None
) -> SourceView:
    digest = hashlib.sha256(raw_markdown.encode("utf-8")).hexdigest()
    starts = _line_starts(raw_markdown)
    return SourceView(
        SourceDocument(
            document_id, raw_markdown, f"sha256:{digest}", len(starts), source_path
        ),
        starts,
    )


def read_source(path: str, document_id: str | None = None) -> SourceView:
    with open(path, "r", encoding="utf-8", newline="") as handle:
        text = handle.read()
    return make_source(document_id or path, text, path)
