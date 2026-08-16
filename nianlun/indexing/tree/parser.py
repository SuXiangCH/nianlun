"""markdown-it-py 结构抽取：标题列表 (title, level, line_num)。

用 ``Token.map`` 拿精确源码行号；``ATX_ONLY`` 默认开启，仅提取 ATX 标题、
过滤 setext 与空标题（见 ``docs/architecture/tree_index_design.md`` §5.2）。
"""

from __future__ import annotations

import re

from markdown_it import MarkdownIt

# strip 后源码行匹配此式才算 ATX 标题：
# - 在 stripped 行上匹配 -> 接受 1~3 空格缩进的 ATX 标题（markdown-it 同样识别）
# - 要求 \s+\S -> 排除 "## " 这类空标题（markdown-it 视为合法空标题，此处刻意不提取）
_ATX_ONLY_RE = re.compile(r"^#{1,6}\s+\S")


def extract_headings(md_text: str, atx_only: bool = True) -> list[dict]:
    """返回 ``[{title, level, line_num}]``，``line_num`` 为 1 基。

    atx_only=True（默认）：仅接受 strip 后源码行匹配 ``^#{1,6}\\s+\\S`` 的标题 token，
    过滤 setext 与空标题。
    atx_only=False：接受所有 heading token（含 setext、空标题），CommonMark 完整能力。
    """
    md = MarkdownIt("commonmark")
    tokens = md.parse(md_text)
    lines = md_text.split("\n")
    headings: list[dict] = []
    for i, tok in enumerate(tokens):
        if tok.type != "heading_open":
            continue
        level = int(tok.tag[1])  # h1..h6 -> 1..6
        line_num = tok.map[0] + 1  # 0 基 -> 1 基
        inline = tokens[i + 1] if i + 1 < len(tokens) else None
        title = inline.content.strip() if inline and inline.type == "inline" else ""
        if atx_only:
            src = lines[line_num - 1].strip() if 0 < line_num <= len(lines) else ""
            if not _ATX_ONLY_RE.match(src):
                continue  # setext（下划行）或空标题 -> 跳过
        headings.append({"title": title, "level": level, "line_num": line_num})
    return headings
