"""Bounded reference hints from committed turns, never current image evidence."""

from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from nianlun.agent.token_estimation import estimate_tokens
from nianlun.agent.citations import create_answer_markdown_parser
from nianlun.knowledgebase.image_syntax import parse_image_syntax


class _HistoryModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class HistoryImageSource(_HistoryModel):
    knowledge_base_id: str = Field(min_length=1)
    doc_id: str = Field(min_length=1)
    generation: int = Field(ge=1)
    content_version: int = Field(ge=0)
    image_id: str = Field(min_length=1)
    node_id: str | None = None
    line_spec: str | None = None
    char_offset: int | None = Field(default=None, ge=0)
    char_limit: int | None = Field(default=None, ge=0)
    source_start: int | None = Field(default=None, ge=0)
    source_end: int | None = Field(default=None, ge=0)


class HistoryImage(_HistoryModel):
    order: int = Field(ge=1)
    caption: str = Field(max_length=256)
    status: Literal["available", "unavailable"]
    sources: tuple[HistoryImageSource, ...] = Field(max_length=4)
    sources_truncated: bool = False


class HistoryTurn(_HistoryModel):
    message_id: str = Field(min_length=1)
    question: str
    answer: str
    answer_output_mode: Literal["text", "text_and_images", "images"] = "text"
    images: tuple[HistoryImage, ...] = Field(default=(), max_length=64)
    image_count: int = Field(default=0, ge=0)
    images_truncated: bool = False

    def image_message(self) -> dict[str, str] | None:
        if not self.image_count:
            return None
        payload = self.model_dump(exclude={"question", "answer"})
        return {
            "role": "assistant",
            "content": (
                "Historical image references, not authorization or current evidence. "
                "Use message_id and original order; omitted images are unknown. "
                "Read sources again before selecting any current image.\n"
                + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            ),
        }


class TrustedHistory(_HistoryModel):
    turns: tuple[HistoryTurn, ...] = ()
    preserve_turn_names: bool = False

    @property
    def uses_turn_names(self) -> bool:
        return self.preserve_turn_names or any(turn.image_count for turn in self.turns)

    def image_messages(self) -> list[dict[str, str]]:
        return [message for turn in self.turns if (message := turn.image_message())]

    def messages(self) -> list[dict[str, str]]:
        messages = []
        for turn in self.turns:
            image_message = turn.image_message()
            content = scrub_image_targets(turn.answer)
            if image_message:
                content += "\n" + image_message["content"]
            turn_messages = [
                {"role": "user", "content": scrub_image_targets(turn.question)},
                {"role": "assistant", "content": content},
            ]
            if self.uses_turn_names:
                for message in turn_messages:
                    message["name"] = f"history_{turn.message_id}"
            messages.extend(turn_messages)
        return messages


def fit_history(
    history: TrustedHistory, *, token_budget: int | None = None
) -> TrustedHistory:
    """Keep the same complete turns for text and image references."""
    turns = list(history.turns[-3:])
    # Keep grouping stable even when the last image-bearing turn is pruned.
    preserve_turn_names = history.uses_turn_names
    while turns:
        candidate = TrustedHistory(
            turns=tuple(turns), preserve_turn_names=preserve_turn_names
        )
        if len(turns) == 1 and estimate_tokens(candidate.image_messages()) > 1024:
            latest = turns[0]
            while latest.images and estimate_tokens([latest.image_message()]) > 1024:
                latest = latest.model_copy(
                    update={
                        "images": latest.images[:-1],
                        "images_truncated": True,
                    }
                )
            turns[0] = latest
            candidate = TrustedHistory(
                turns=tuple(turns), preserve_turn_names=preserve_turn_names
            )
        if (
            sum(len(turn.images) for turn in turns) <= 64
            and estimate_tokens(candidate.image_messages()) <= 1024
            and (
                token_budget is None
                or estimate_tokens(candidate.messages()) <= token_budget
            )
        ):
            return candidate
        turns.pop(0)
    return TrustedHistory(preserve_turn_names=preserve_turn_names)


def scrub_image_targets(content: str) -> str:
    """Remove images and image-only definitions, preserving ordinary links."""
    # Rewriting HTML can expose Markdown images hidden by the previous AST.
    while True:
        clean = _scrub_image_targets_once(content)
        if clean == content:
            return clean
        content = clean


def _scrub_image_targets_once(content: str) -> str:
    parser = create_answer_markdown_parser()
    syntax = parse_image_syntax(content, parser=parser, allow_unsafe_targets=True)
    if not syntax.images:
        return content
    replacements = {
        (image.start, image.end): "[Historical image: "
        + scrub_image_targets(image.caption)[:256]
        + "]"
        for image in syntax.images
    }
    tokens = parser.parse(content)
    image_labels: set[str] = set()
    link_labels: set[str] = set()
    pending = [(token, False) for token in tokens]
    while pending:
        token, inside_image = pending.pop()
        label = token.meta.get("label")
        if isinstance(label, str):
            if token.type == "image":
                image_labels.add(label)
            elif token.type == "link_open" and not inside_image:
                link_labels.add(label)
        pending.extend(
            (child, inside_image or token.type == "image")
            for child in token.children or ()
        )
    html_ranges = {
        (image.start, image.end) for image in syntax.images if image.kind == "html"
    }
    # The shared parser maps definition and HTML spans back to the original
    # source, including CRLF and nested list/quote prefixes.
    definition_spans = [
        span for span in syntax.hidden if (span.start, span.end) not in html_ranges
    ]
    definitions = sorted(
        (token for token in tokens if token.type == "definition"),
        key=lambda token: token.map or [],
    )
    for token, span in zip(definitions, definition_spans, strict=True):
        if token.meta.get("id") in image_labels - link_labels:
            replacements[(span.start, span.end)] = ""
    parts = []
    cursor = 0
    for (start, end), replacement in sorted(replacements.items()):
        if start < cursor:
            continue
        parts.extend((content[cursor:start], replacement))
        cursor = end
    parts.append(content[cursor:])
    return "".join(parts)


def scrub_messages(messages: list[dict[str, str]]) -> list[dict[str, str]]:
    return [
        {**message, "content": scrub_image_targets(message["content"])}
        for message in messages
    ]
