"""Interpret logical image nodes without trusting model-provided addresses."""

from __future__ import annotations

import re
import unicodedata
from bisect import bisect_right
from collections.abc import Iterator
from typing import TypedDict

from markdown_it.token import Token

from nianlun.agent.citations import create_answer_markdown_parser, extract_citation_ids
from nianlun.knowledgebase.image_syntax import parse_image_syntax


class AnswerImageOccurrence(TypedDict):
    attachment_id: str
    start: int
    end: int
    kind: str


def escape_image_caption(value: str) -> str:
    return re.sub(r"([\\\[\]])", r"\\\1", " ".join(value.split()))


def _image_blocks(
    answer: str,
    *,
    max_occurrences: int,
    max_candidate_line_bytes: int,
    max_canonical_bytes: int,
    unavailable_text: str,
) -> tuple[list[tuple[int, int, str | None, str]], int]:
    if len(answer.encode("utf-8")) > max_canonical_bytes:
        raise ValueError("答案正文超出限制")
    offsets = [0, *(match.end() for match in re.finditer(r"\r\n|\r|\n", answer))]
    if offsets[-1] != len(answer):
        offsets.append(len(answer))
    parser = create_answer_markdown_parser()
    parser.options.update(store_labels=True, inline_definitions=True)
    parser.validateLink = lambda url: True
    tokens = parser.parse(answer)
    source_parser = create_answer_markdown_parser()
    syntax = parse_image_syntax(answer, parser=source_parser, allow_unsafe_targets=True)
    descendants = [child for token in tokens for child in _descendants(token)]
    count = sum(child.type == "image" for child in descendants) + sum(
        image.kind == "html" for image in syntax.images
    )
    if count > max_occurrences:
        raise ValueError("答案图片引用次数超出限制")
    standalone_ranges: set[tuple[int, int]] = set()
    for index, token in enumerate(tokens):
        if token.type != "inline" or token.map is None:
            continue
        children = token.children or []
        standalone = (
            index > 0
            and tokens[index - 1].type == "paragraph_open"
            and tokens[index - 1].level == 0
            and len(children) == 1
            and children[0].type == "image"
        )
        if standalone:
            standalone_ranges.add((offsets[token.map[0]], offsets[token.map[1]]))
    blocks: list[tuple[int, int, str | None, str]] = []
    for image in syntax.images:
        _check_candidate_lines(
            answer, offsets, image.start, image.end, max_candidate_line_bytes
        )
        standalone = image.kind == "markdown" and any(
            start <= image.start < image.end <= end for start, end in standalone_ranges
        )
        target = image.source if standalone else None
        ref = (
            target.removeprefix("nianlun-image:")
            if isinstance(target, str) and target.startswith("nianlun-image:")
            else None
        )
        blocks.append((image.start, image.end, ref, unavailable_text))
    image_labels = {
        child.meta.get("label") for child in descendants if child.type == "image"
    }
    link_labels = {
        child.meta.get("label") for child in descendants if child.type == "link_open"
    }
    for token in tokens:
        if (
            token.type == "definition"
            and token.map
            and token.meta.get("id") in image_labels
        ):
            start, end = (offsets[line] for line in token.map)
            _check_candidate_lines(
                answer, offsets, start, end, max_candidate_line_bytes
            )
            if token.meta.get("id") not in link_labels:
                blocks.append((start, end, None, ""))
    return sorted(blocks, key=lambda block: block[0]), count


def _descendants(token: Token) -> Iterator[Token]:
    for child in token.children or ():
        yield child
        yield from _descendants(child)


def _check_candidate_lines(
    answer: str, offsets: list[int], start: int, end: int, maximum: int
) -> None:
    first = max(0, bisect_right(offsets, start) - 1)
    last = bisect_right(offsets, end - 1)
    if any(
        len(answer[offsets[line] : offsets[line + 1]].rstrip("\r\n").encode("utf-8"))
        > maximum
        for line in range(first, min(last, len(offsets) - 1))
    ):
        raise ValueError("答案图片候选行超出限制")


def answer_image_references(
    answer: str,
    *,
    max_occurrences: int = 128,
    max_candidate_line_bytes: int = 4 * 1024,
    max_canonical_bytes: int = 1024 * 1024,
) -> list[str]:
    blocks, _ = _image_blocks(
        answer,
        max_occurrences=max_occurrences,
        max_candidate_line_bytes=max_candidate_line_bytes,
        max_canonical_bytes=max_canonical_bytes,
        unavailable_text="图片暂不可用",
    )
    return list(dict.fromkeys(ref for _, _, ref, _ in blocks if ref))


def add_cited_source_images(
    answer: str,
    images_by_citation: dict[int, list[tuple[str, str]]],
    *,
    source_contexts: dict[tuple[int, str], str] | None = None,
    max_occurrences: int = 128,
    max_candidate_line_bytes: int = 4 * 1024,
    max_canonical_bytes: int = 1024 * 1024,
) -> str:
    """Match source context to cited blocks while retaining image order and safe boundaries."""
    if not images_by_citation:
        return answer
    blocks, count = _image_blocks(
        answer,
        max_occurrences=max_occurrences,
        max_candidate_line_bytes=max_candidate_line_bytes,
        max_canonical_bytes=max_canonical_bytes,
        unavailable_text="图片暂不可用",
    )
    seen = {ref for _, _, ref, _ in blocks if ref}
    citations = extract_citation_ids(answer)
    if not citations.intersection(images_by_citation):
        return answer
    offsets = [0, *(match.end() for match in re.finditer(r"\r\n|\r|\n", answer))]
    if offsets[-1] != len(answer):
        offsets.append(len(answer))
    size = len(answer.encode("utf-8"))
    answer_blocks: list[tuple[int, set[int], set[str]]] = []
    for token in create_answer_markdown_parser().parse(answer):
        if token.level != 0 or token.map is None:
            continue
        start, end = (offsets[line] for line in token.map)
        block_citations = extract_citation_ids(answer[start:end]) & citations
        if block_citations:
            answer_blocks.append(
                (end, block_citations, _placement_terms(answer[start:end]))
            )
    insertions: dict[int, list[str]] = {}
    for citation, images in images_by_citation.items():
        eligible = [block for block in answer_blocks if citation in block[1]]
        if not eligible:
            continue
        previous_end = 0
        for ref, caption in images:
            if ref in seen or count >= max_occurrences:
                continue
            context = (source_contexts or {}).get((citation, ref), "")
            terms = _placement_terms(context)
            end = eligible[-1][0]
            best_score = 0.0
            for block_end, _, block_terms in eligible:
                shared = terms & block_terms
                if len(shared) < 3:
                    continue
                score = len(shared) / max(1, len(block_terms)) ** 0.5
                if score >= best_score:
                    end, best_score = block_end, score
            # Images from one source keep their original order even after summarization.
            end = max(previous_end, end)
            marker = f"![{escape_image_caption(caption)}](nianlun-image:{ref})"
            # Archiving replaces request refs with 36-character UUIDs.
            marker_bytes = len(marker.encode("utf-8")) + max(0, 36 - len(ref))
            extra_bytes = marker_bytes + 4
            if (
                marker_bytes > max_candidate_line_bytes
                or size + extra_bytes > max_canonical_bytes
            ):
                continue
            insertions.setdefault(end, []).append(marker)
            previous_end = end
            seen.add(ref)
            count += 1
            size += extra_bytes
    for end, markers in sorted(insertions.items(), reverse=True):
        answer = answer[:end] + "\n\n" + "\n\n".join(markers) + "\n\n" + answer[end:]
    return answer


def _placement_terms(markdown: str) -> set[str]:
    """Compare source wording without treating paths, formatting or code as content."""
    text = " ".join(
        child.content
        for token in create_answer_markdown_parser().parse(markdown)
        if token.type == "inline"
        for child in token.children or []
        if child.type == "text"
    )
    text = unicodedata.normalize("NFKC", text).casefold()
    words = set(re.findall(r"[a-z]{3,}", text)) - {
        "the",
        "and",
        "for",
        "with",
        "from",
        "this",
        "that",
        "are",
    }
    for run in re.findall(r"[\u3400-\u9fff]+", text):
        words.update(run[index : index + 2] for index in range(len(run) - 1))
    return words


def normalize_answer_images(
    answer: str,
    allowed: dict[str, tuple[str, str]],
    *,
    max_occurrences: int = 128,
    max_candidate_line_bytes: int = 4 * 1024,
    max_canonical_bytes: int = 1024 * 1024,
    unavailable_text: str = "图片暂不可用",
) -> tuple[str, list[AnswerImageOccurrence]]:
    text = answer
    bindings = allowed
    previous: list[AnswerImageOccurrence] | None = None
    total = 0
    # Removing HTML or definitions can expose Markdown that the first AST hid.
    # Each further pass may preserve only the exact previously authorized nodes.
    for _ in range(max_occurrences + 2):
        blocks, count = _image_blocks(
            text,
            max_occurrences=max_occurrences,
            max_candidate_line_bytes=max_candidate_line_bytes,
            max_canonical_bytes=max_canonical_bytes,
            unavailable_text=unavailable_text,
        )
        total += max(0, count - len(previous or []))
        if total > max_occurrences:
            raise ValueError("答案图片引用次数超出限制")
        positions = (
            None
            if previous is None
            else {
                (item["start"], item["end"], item["attachment_id"]) for item in previous
            }
        )
        normalized, occurrences = _rewrite_image_blocks(
            text, blocks, bindings, positions
        )
        if normalized == text:
            return normalized, occurrences
        text = normalized
        previous = occurrences
        bindings = {
            attachment_id: (attachment_id, caption)
            for attachment_id, caption in allowed.values()
        }
    raise ValueError("答案图片输出无法完成校验")


def _rewrite_image_blocks(
    answer: str,
    blocks: list[tuple[int, int, str | None, str]],
    allowed: dict[str, tuple[str, str]],
    authorized_positions: set[tuple[int, int, str]] | None,
) -> tuple[str, list[AnswerImageOccurrence]]:
    parts: list[str] = []
    occurrences: list[AnswerImageOccurrence] = []
    cursor = 0
    length = 0
    seen: set[str] = set()
    for start, end, ref, fallback in blocks:
        prefix = answer[cursor:start]
        parts.append(prefix)
        length += len(prefix)
        binding = allowed.get(ref) if ref else None
        if (
            authorized_positions is not None
            and (start, end, ref) not in authorized_positions
        ):
            binding = None
        if binding is None:
            replacement = fallback
        else:
            attachment_id, caption = binding
            replacement = (
                f"![{escape_image_caption(caption)}](nianlun-image:{attachment_id})"
            )
            occurrences.append(
                {
                    "attachment_id": attachment_id,
                    "start": length,
                    "end": length + len(replacement),
                    "kind": "repeat" if attachment_id in seen else "first",
                }
            )
            seen.add(attachment_id)
        parts.append(replacement)
        length += len(replacement)
        cursor = end
    parts.append(answer[cursor:])
    return "".join(parts), occurrences


__all__ = [
    "AnswerImageOccurrence",
    "add_cited_source_images",
    "answer_image_references",
    "escape_image_caption",
    "normalize_answer_images",
]
