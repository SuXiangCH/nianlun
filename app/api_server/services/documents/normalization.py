"""Source-preserving normalization of local resources in MinerU Markdown."""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from markdown_it import MarkdownIt
from markdown_it.token import Token

from app.api_server.services.documents.pipeline_contracts import PageMapEntry


class NormalizationResourceError(ValueError):
    pass


@dataclass(frozen=True)
class NormalizedChunk:
    markdown: str
    assets: dict[str, bytes]


class _HtmlDestinationCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.destinations: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del tag
        self._collect(attrs)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del tag
        self._collect(attrs)

    def _collect(self, attrs: list[tuple[str, str | None]]) -> None:
        for name, value in attrs:
            if name.lower() in {"src", "href"} and value:
                self.destinations.add(value)


def _is_local_destination(destination: str) -> bool:
    split = urlsplit(destination)
    return bool(
        not split.scheme
        and not split.netloc
        and split.path
        and not split.path.startswith("/")
    )


def _collect_destinations(markdown: str) -> tuple[set[str], list[str]]:
    destinations: set[str] = set()
    html_fragments: list[str] = []
    tokens = MarkdownIt("commonmark").parse(markdown)

    def visit(token: Token) -> None:
        if token.type == "image":
            source = token.attrGet("src")
            if isinstance(source, str) and source:
                destinations.add(source)
        elif token.type == "link_open":
            target = token.attrGet("href")
            if isinstance(target, str) and target:
                destinations.add(target)
        elif token.type in {"html_block", "html_inline"}:
            html_fragments.append(token.content)
            collector = _HtmlDestinationCollector()
            collector.feed(token.content)
            destinations.update(collector.destinations)
        for child in token.children or []:
            visit(child)

    for token in tokens:
        visit(token)
    return {
        item for item in destinations if _is_local_destination(item)
    }, html_fragments


def _resolve_resource(chunk_root: Path, destination: str) -> Path:
    split = urlsplit(destination)
    decoded = unquote(split.path).replace("\\", "/")
    relative = PurePosixPath(decoded)
    if relative.is_absolute() or ".." in relative.parts:
        raise NormalizationResourceError(f"本地资源路径越界: {destination}")
    root = chunk_root.resolve()
    direct = (root / Path(*relative.parts)).resolve()
    try:
        direct.relative_to(root)
    except ValueError as exc:
        raise NormalizationResourceError(f"本地资源路径越界: {destination}") from exc
    if direct.is_file():
        return direct

    matches: list[Path] = []
    suffix = relative.parts
    for candidate in root.rglob("*"):
        if not candidate.is_file():
            continue
        candidate_relative = candidate.relative_to(root)
        if len(candidate_relative.parts) >= len(suffix) and (
            candidate_relative.parts[-len(suffix) :] == suffix
        ):
            matches.append(candidate)
    if not matches:
        raise NormalizationResourceError(f"本地资源不存在: {destination}")
    if len(matches) > 1:
        raise NormalizationResourceError(f"本地资源路径不唯一: {destination}")
    return matches[0]


def _rewritten_destination(asset_name: str, original: str) -> str:
    split = urlsplit(original)
    encoded_path = quote(asset_name, safe="/@:+")
    return urlunsplit(("", "", encoded_path, split.query, split.fragment))


_INLINE_LINK_RE = re.compile(
    r"(?P<prefix>!?\[[^\]\n]*\]\(\s*)(?P<angle><)?"
    r"(?P<destination>[^\s)>]+)(?(angle)>)(?P<suffix>(?:\s+[^\n)]*)?\))"
)
_REFERENCE_LINK_RE = re.compile(
    r"(?m)^(?P<prefix> {0,3}\[[^\]\n]+\]:\s*)(?P<angle><)?"
    r"(?P<destination>[^\s>]+)(?(angle)>)"
)
_HTML_ATTRIBUTE_RE = re.compile(
    r"(?P<prefix>\b(?:src|href)\s*=\s*)"
    r"(?:(?P<double>\")(?:[^\"]*)(?P=double)|"
    r"(?P<single>')(?:[^']*)(?P=single)|(?P<bare>[^\s\"'=<>`]+))",
    re.IGNORECASE,
)


def _rewrite_link_match(match: re.Match[str], replacements: dict[str, str]) -> str:
    destination = match.group("destination")
    replacement = replacements.get(html.unescape(destination))
    if replacement is None:
        return match.group(0)
    angle = "<" if match.group("angle") else ""
    close_angle = ">" if angle else ""
    suffix = match.groupdict().get("suffix") or ""
    return f"{match.group('prefix')}{angle}{replacement}{close_angle}{suffix}"


def _rewrite_markdown_links(markdown: str, replacements: dict[str, str]) -> str:
    output: list[str] = []
    fence: str | None = None
    for line in markdown.splitlines(keepends=True):
        fence_match = re.match(r" {0,3}(`{3,}|~{3,})", line)
        if fence_match:
            marker = fence_match.group(1)
            if fence is None:
                fence = marker[0]
            elif marker[0] == fence:
                fence = None
            output.append(line)
            continue
        if fence is not None:
            output.append(line)
            continue

        pieces: list[str] = []
        cursor = 0
        for code in re.finditer(r"(`+)(.*?)\1", line):
            plain = line[cursor : code.start()]
            plain = _INLINE_LINK_RE.sub(
                lambda match: _rewrite_link_match(match, replacements), plain
            )
            plain = _REFERENCE_LINK_RE.sub(
                lambda match: _rewrite_link_match(match, replacements), plain
            )
            pieces.extend((plain, code.group(0)))
            cursor = code.end()
        plain = line[cursor:]
        plain = _INLINE_LINK_RE.sub(
            lambda match: _rewrite_link_match(match, replacements), plain
        )
        plain = _REFERENCE_LINK_RE.sub(
            lambda match: _rewrite_link_match(match, replacements), plain
        )
        pieces.append(plain)
        output.append("".join(pieces))
    return "".join(output)


def _rewrite_html_fragment(fragment: str, replacements: dict[str, str]) -> str:
    def replace(match: re.Match[str]) -> str:
        raw = match.group(0)[len(match.group("prefix")) :]
        if match.group("double"):
            value = raw[1:-1]
            quote_character = '"'
        elif match.group("single"):
            value = raw[1:-1]
            quote_character = "'"
        else:
            value = match.group("bare")
            quote_character = ""
        replacement = replacements.get(html.unescape(value))
        if replacement is None:
            return match.group(0)
        return f"{match.group('prefix')}{quote_character}{replacement}{quote_character}"

    return _HTML_ATTRIBUTE_RE.sub(replace, fragment)


def normalize_chunk_resources(
    markdown: str, chunk_root: Path, chunk_index: int
) -> NormalizedChunk:
    """Copy every parsed local reference into a chunk namespace and rewrite it."""
    destinations, html_fragments = _collect_destinations(markdown)
    replacements: dict[str, str] = {}
    assets: dict[str, bytes] = {}
    root = chunk_root.resolve()
    for destination in sorted(destinations):
        source = _resolve_resource(root, destination)
        source_relative = source.relative_to(root).as_posix()
        asset_name = f"chunks/{chunk_index:04d}/assets/{source_relative}"
        replacements[destination] = _rewritten_destination(asset_name, destination)
        assets[asset_name] = source.read_bytes()

    rewritten = _rewrite_markdown_links(markdown, replacements)
    cursor = 0
    for fragment in html_fragments:
        start = rewritten.find(fragment, cursor)
        if start < 0:
            continue
        replacement = _rewrite_html_fragment(fragment, replacements)
        rewritten = rewritten[:start] + replacement + rewritten[start + len(fragment) :]
        cursor = start + len(replacement)
    remaining, _ = _collect_destinations(rewritten)
    unresolved = remaining - set(replacements.values())
    if unresolved:
        raise NormalizationResourceError(
            f"规范化后仍有未隔离的本地资源: {sorted(unresolved)[0]}"
        )
    return NormalizedChunk(markdown=rewritten, assets=assets)


def _content_block_anchor(block: dict[str, object]) -> str | None:
    for key in ("text", "table_body", "img_path"):
        value = block.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def build_content_list_page_entries(
    markdown: str,
    content_list_path: Path,
    *,
    chunk_index: int,
    source_page_start: int | None,
    source_page_end: int | None,
    markdown_line_offset: int,
) -> list[PageMapEntry] | None:
    """Return exact page ranges only when every upstream block is unambiguous."""
    if source_page_start is None or source_page_end is None:
        return None
    try:
        payload = json.loads(content_list_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(payload, list) or not payload:
        return None

    located: list[tuple[int, int]] = []
    cursor = 0
    previous_page = -1
    page_count = source_page_end - source_page_start + 1
    for raw_block in payload:
        if not isinstance(raw_block, dict):
            return None
        page_index = raw_block.get("page_idx")
        if (
            not isinstance(page_index, int)
            or isinstance(page_index, bool)
            or page_index < previous_page
            or not 0 <= page_index < page_count
        ):
            return None
        anchor = _content_block_anchor(raw_block)
        if anchor is None:
            return None
        position = markdown.find(anchor, cursor)
        if position < 0 or markdown.find(anchor, position + 1) >= 0:
            return None
        located.append((page_index, position))
        cursor = position + len(anchor)
        previous_page = page_index

    page_starts: list[tuple[int, int]] = []
    for page_index, position in located:
        if not page_starts or page_starts[-1][0] != page_index:
            line = markdown.count("\n", 0, position) + 1
            page_starts.append((page_index, line))
    if not page_starts or page_starts[0][0] != 0:
        return None

    total_lines = markdown.count("\n") + 1
    entries: list[PageMapEntry] = []
    for index, (page_index, line_start) in enumerate(page_starts):
        effective_start = 1 if index == 0 else line_start
        line_end = (
            page_starts[index + 1][1] - 1
            if index + 1 < len(page_starts)
            else total_lines
        )
        if line_end < effective_start:
            return None
        source_page = source_page_start + page_index
        entries.append(
            PageMapEntry(
                markdown_line_start=markdown_line_offset + effective_start - 1,
                markdown_line_end=markdown_line_offset + line_end - 1,
                chunk_index=chunk_index,
                source_page_start=source_page,
                source_page_end=source_page,
                evidence="content_list",
            )
        )
    return entries


__all__ = [
    "NormalizedChunk",
    "NormalizationResourceError",
    "build_content_list_page_entries",
    "normalize_chunk_resources",
]
