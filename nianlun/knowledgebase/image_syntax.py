"""Markdown image ranges in original Python-character source coordinates."""

from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Literal, SupportsIndex

from markdown_it import MarkdownIt
from markdown_it.parser_block import ParserBlock
from markdown_it.rules_block.state_block import StateBlock
from markdown_it.rules_core.state_core import StateCore
from markdown_it.rules_inline import image as image_rule
from markdown_it.rules_inline import html_inline as html_rule
from markdown_it.rules_inline.state_inline import StateInline
from markdown_it.token import Token
from markdown_it.utils import EnvType


@dataclass(frozen=True, slots=True)
class SourceSpan:
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class ImageSyntax(SourceSpan):
    source: str
    caption: str
    kind: Literal["markdown", "html"] = "markdown"


@dataclass(frozen=True, slots=True)
class ImageSourceSyntax:
    images: tuple[ImageSyntax, ...]
    hidden: tuple[SourceSpan, ...]


def _image(state: StateInline, silent: bool) -> bool:
    start = state.pos
    matched = image_rule(state, silent)
    if matched and not silent:
        state.tokens[-1].meta["source_range"] = (start, state.pos)
    return matched


def _html(state: StateInline, silent: bool) -> bool:
    start = state.pos
    matched = html_rule(state, silent)
    if matched and not silent:
        state.tokens[-1].meta["source_range"] = (start, state.pos)
    return matched


class _MappedText(str):
    """Parser text whose characters retain their original, possibly shared spans."""

    spans: tuple[SourceSpan, ...]

    def __new__(cls, value: str, spans: tuple[SourceSpan, ...]) -> _MappedText:
        text = super().__new__(cls, value)
        text.spans = spans
        return text

    def __getitem__(self, key: SupportsIndex | slice) -> str:
        value = super().__getitem__(key)
        return _MappedText(value, self.spans[key]) if isinstance(key, slice) else value

    def __add__(self, value: str, /) -> str:
        if isinstance(value, _MappedText):
            return _MappedText(super().__add__(value), self.spans + value.spans)
        return self if value == "" else super().__add__(value)

    def __radd__(self, value: str, /) -> str:
        return self if value == "" else value + str(self)

    def strip(self, chars: str | None = None) -> _MappedText:
        start = len(self) - len(self.lstrip(chars))
        end = len(self.rstrip(chars))
        return _MappedText(str(self)[start:end], self.spans[start:end])


def _normalize(state: StateCore) -> None:
    source = state.src
    chars: list[str] = []
    spans: list[SourceSpan] = []
    index = 0
    while index < len(source):
        start = index
        char = source[index]
        index += 1
        if char == "\r":
            if index < len(source) and source[index] == "\n":
                index += 1
            char = "\n"
        elif char == "\x00":
            char = "\ufffd"
        chars.append(char)
        spans.append(SourceSpan(start, index))
    state.src = _MappedText("".join(chars), tuple(spans))


class _MappedBlockState(StateBlock):
    def getLines(self, begin: int, end: int, indent: int, keepLastLF: bool) -> str:
        # Mirror MarkdownIt's block extraction while preserving tab expansion and
        # the live block offsets (nested list/quote rules temporarily change them).
        source = self.src
        assert isinstance(source, _MappedText)
        pieces: list[str] = []
        spans: list[SourceSpan] = []
        for line in range(begin, end):
            first = line_start = self.bMarks[line]
            last = self.eMarks[line] + (line + 1 < end or keepLastLF)
            line_indent = 0
            while first < last and line_indent < indent:
                char = source[first]
                if char in (" ", "\t"):
                    line_indent += (
                        4 - (line_indent + self.bsCount[line]) % 4
                        if char == "\t"
                        else 1
                    )
                elif first - line_start < self.tShift[line]:
                    line_indent += 1
                else:
                    break
                first += 1
            if line_indent > indent:
                count = line_indent - indent
                pieces.append(" " * count)
                spans.extend([source.spans[first - 1]] * count)
            pieces.append(source[first:last])
            spans.extend(source.spans[first:last])
        return _MappedText("".join(pieces), tuple(spans))


class _MappedBlockParser(ParserBlock):
    def parse(
        self, src: str, md: MarkdownIt, env: EnvType, outTokens: list[Token]
    ) -> list[Token] | None:
        if not src:
            return None
        state = _MappedBlockState(src, md, env, outTokens)
        self.tokenize(state, state.line, state.lineMax)
        return state.tokens


def _line_offsets(source: str) -> tuple[list[str], list[int]]:
    # HTMLParser and MarkdownIt count LF only; splitlines also splits Unicode
    # separators that are ordinary characters to both parsers.
    parts = source.split("\n")
    lines = [part + "\n" for part in parts[:-1]] + [parts[-1]]
    starts = [0]
    for line in lines:
        starts.append(starts[-1] + len(line))
    return lines, starts


class _HTMLImages(HTMLParser):
    _SAFE_TAGS = frozenset(
        "a abbr b bdi bdo blockquote br caption cite code col colgroup dd del details dfn div dl dt em figcaption figure h1 h2 h3 h4 h5 h6 hr i ins kbd li mark ol p pre q rp rt ruby s samp section small span strong sub summary sup table tbody td tfoot th thead time tr u ul var wbr".split()
    )
    _SAFE_ATTRIBUTES = frozenset(
        "align border cellpadding cellspacing class colspan dir height id lang open rowspan scope title width".split()
    )

    def __init__(self, source: str, *, safety_mode: bool = False) -> None:
        super().__init__(convert_charrefs=True)
        self.safety_mode = safety_mode
        self.starts = _line_offsets(source)[1]
        self.images: list[ImageSyntax] = []
        self.feed(source)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        safe = tag in self._SAFE_TAGS and all(
            name in self._SAFE_ATTRIBUTES or (tag == "a" and name == "href")
            for name, _ in attrs
        )
        if tag != "img" and (not self.safety_mode or safe):
            return
        values = dict(attrs)
        source = values.get("src") or ""
        line, column = self.getpos()
        start = self.starts[line - 1] + column
        raw = self.get_starttag_text()
        if raw is None:
            raise ValueError("HTML image has no source tag")
        self.images.append(
            ImageSyntax(
                start, start + len(raw), source, values.get("alt") or "", "html"
            )
        )

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)


def parse_image_syntax(
    source: str,
    *,
    reference_markdown: str | None = None,
    parser: MarkdownIt | None = None,
    allow_unsafe_targets: bool = False,
) -> ImageSourceSyntax:
    parser = parser or MarkdownIt("commonmark", {"html": True}).enable("table")
    parser.options.update(store_labels=True, inline_definitions=True)
    if allow_unsafe_targets:
        # Safety consumers must see every target so they can remove it, including
        # schemes the normal Markdown renderer would reject before tokenization.
        parser.validateLink = lambda url: True
    parser.core.ruler.at("normalize", _normalize)
    mapped_block = _MappedBlockParser()
    mapped_block.ruler = parser.block.ruler
    parser.block = mapped_block
    parser.inline.ruler.at("image", _image)
    parser.inline.ruler.at("html_inline", _html)
    env = {}
    if reference_markdown is not None:
        reference_env = {}
        parser.parse(reference_markdown, reference_env)
        env["references"] = dict(reference_env.get("references", {}))
    state = StateCore(source, parser, env)
    parser.core.process(state)
    tokens = state.tokens
    normalized = state.src
    assert isinstance(normalized, _MappedText)
    _, starts = _line_offsets(normalized)
    original_starts = [
        normalized.spans[index].start if index < len(normalized) else len(source)
        for index in starts
    ]
    images: list[ImageSyntax] = []
    hidden: list[SourceSpan] = []
    for token in tokens:
        if token.map is None:
            continue
        first, last = token.map
        if token.type == "definition":
            hidden.append(SourceSpan(original_starts[first], original_starts[last]))
            continue
        if token.type == "html_block":
            raw = token.content
            assert isinstance(raw, _MappedText)
            for image in _HTMLImages(raw, safety_mode=allow_unsafe_targets).images:
                item = ImageSyntax(
                    raw.spans[image.start].start,
                    raw.spans[image.end - 1].end,
                    image.source,
                    image.caption,
                    "html",
                )
                images.append(item)
                hidden.append(SourceSpan(item.start, item.end))
            continue
        if token.type != "inline" or not token.content:
            continue
        assert isinstance(token.content, _MappedText)
        positions = token.content.spans
        for child in token.children or ():
            span = child.meta.get("source_range")
            if not isinstance(span, tuple) or len(span) != 2:
                continue
            start, end = span
            if not 0 <= start < end <= len(positions):
                raise ValueError("Markdown image source coordinates are invalid")
            source_start, source_end = positions[start].start, positions[end - 1].end
            if child.type == "image":
                images.append(
                    ImageSyntax(
                        source_start,
                        source_end,
                        str(child.attrGet("src") or ""),
                        child.content,
                    )
                )
            elif child.type == "html_inline":
                for image in _HTMLImages(
                    child.content, safety_mode=allow_unsafe_targets
                ).images:
                    item = ImageSyntax(
                        positions[start + image.start].start,
                        positions[start + image.end - 1].end,
                        image.source,
                        image.caption,
                        "html",
                    )
                    images.append(item)
                    hidden.append(SourceSpan(item.start, item.end))
    return ImageSourceSyntax(
        tuple(sorted(images, key=lambda image: image.start)),
        tuple(sorted(hidden, key=lambda span: span.start)),
    )


__all__ = ["ImageSourceSyntax", "ImageSyntax", "SourceSpan", "parse_image_syntax"]
