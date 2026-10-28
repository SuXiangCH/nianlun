"""Markdown-aware citation extraction for generated answers."""

from __future__ import annotations

import re
from typing import Any

import html5lib
from markdown_it import MarkdownIt
from mdit_py_plugins.dollarmath import dollarmath_plugin

_CITATION_RE = re.compile(r"\[(\d+)\]")
_HTML_TABLE_RE = re.compile(r"<table\b[^>]*>[\s\S]*?</table>", re.IGNORECASE)


def _unmatched_double_dollar_rule(state: Any, silent: bool) -> bool:
    """Keep an unmatched ``$$`` literal, matching remark-math behavior."""
    if not state.src.startswith("$$", state.pos):
        return False
    if state.src.find("$$", state.pos + 2) >= 0:
        return False
    if not silent:
        token = state.push("text", "", 0)
        token.content = "$$"
    state.pos += 2
    return True


def create_answer_markdown_parser() -> MarkdownIt:
    """Keep citation and image interpretation aligned with the console syntax."""
    parser = (
        MarkdownIt("commonmark")
        .enable("table")
        .use(dollarmath_plugin, double_inline=True)
    )
    parser.inline.ruler.before(
        "math_inline", "remark_math_unmatched_double", _unmatched_double_dollar_rule
    )
    return parser


_MARKDOWN = create_answer_markdown_parser()


def _html_table_cells(html: str) -> list[str]:
    """Return browser-like table cell text using HTML5 tree construction."""
    fragment = html5lib.parseFragment(html, namespaceHTMLElements=False)
    cells: list[str] = []
    for table in fragment.iter("table"):
        for row in table.iter("tr"):
            cells.extend(
                "".join(cell.itertext()) for cell in row if cell.tag in {"td", "th"}
            )
    return cells


def _text_citation_ids(children: list[Any]) -> set[int]:
    citation_ids: set[int] = set()
    link_depth = 0
    for child in children:
        if child.type == "link_open":
            link_depth += 1
        elif child.type == "link_close":
            link_depth = max(0, link_depth - 1)
        elif child.type == "text" and link_depth == 0:
            citation_ids.update(
                int(match.group(1)) for match in _CITATION_RE.finditer(child.content)
            )
    return citation_ids


def _inline_citation_ids(text: str) -> set[int]:
    tokens = _MARKDOWN.parseInline(text)
    children = tokens[0].children if tokens else None
    return _text_citation_ids(children or [])


def _table_citation_ids(tokens: list[Any]) -> set[int]:
    citation_ids: set[int] = set()
    for token in tokens:
        if token.type == "html_block":
            cells = _html_table_cells(token.content)
            remaining = _HTML_TABLE_RE.sub("\n\n", token.content)
            for remaining_token in _MARKDOWN.parse(remaining):
                if remaining_token.type == "inline" and remaining_token.children:
                    citation_ids.update(_text_citation_ids(remaining_token.children))
        elif token.type == "inline" and token.children:
            html_parts: list[str] = []
            link_depth = 0
            for child in token.children:
                if child.type == "link_open":
                    link_depth += 1
                elif child.type == "link_close":
                    link_depth = max(0, link_depth - 1)
                elif link_depth == 0 and child.type in {"html_inline", "text"}:
                    html_parts.append(child.content)
            cells = _html_table_cells("".join(html_parts))
        else:
            continue
        for cell in cells:
            citation_ids.update(_inline_citation_ids(cell))
    return citation_ids


def extract_citation_ids(markdown: str) -> set[int]:
    """Extract citations using the Markdown semantics supported by the chat UI."""
    tokens = _MARKDOWN.parse(markdown)
    citation_ids = _table_citation_ids(tokens)
    for token in tokens:
        if token.type != "inline" or not token.children:
            continue
        citation_ids.update(_text_citation_ids(token.children))
    return citation_ids


__all__ = ["create_answer_markdown_parser", "extract_citation_ids"]
