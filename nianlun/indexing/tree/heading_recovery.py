"""Recover effective Markdown heading levels without modifying source text.

The deterministic path recognizes common document outline markers.  The optional
LLM path follows the same bounded contract used by MinerU's title-level aid: it
may assign a level to each existing heading ID, but cannot add, remove, reorder,
or rename headings.  Callers retain the original Markdown and line locations.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Literal, Protocol, Sequence

from json_repair import repair_json

from nianlun.models.llm import content_to_text


logger = logging.getLogger(__name__)


HeadingRecoveryMode = Literal["off", "rules", "rules_then_llm"]
HeadingConfidence = Literal["source", "rule", "llm"]

_ARABIC_OUTLINE_RE = re.compile(
    r"^\s*(?P<number>\d+(?:[.．]\d+){0,5})(?:[.．、:：）)\]]|\s|$)"
)
_CJK_PRIMARY_RE = re.compile(r"^\s*(?P<number>[一二三四五六七八九十百千]+)[、．.]\s*")
_CJK_PAREN_RE = re.compile(r"^\s*[（(](?P<number>[一二三四五六七八九十百千]+)[）)]\s*")
_ARABIC_PAREN_RE = re.compile(r"^\s*[（(](?P<number>\d+)[）)]\s*")
_CJK_CHAPTER_RE = re.compile(
    r"^\s*第\s*(?P<number>[一二三四五六七八九十百千\d]+)\s*[章节篇部分]\s*"
)
_CJK_DIGITS = {
    "一": 1,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}
_CJK_UNITS = {"十": 10, "百": 100, "千": 1000}


class LLMInvoker(Protocol):
    async def ainvoke(
        self, prompt: Any, config: Any | None = None, **kwargs: Any
    ) -> Any: ...


@dataclass(frozen=True, slots=True)
class HeadingRecoveryConfig:
    """Safety limits shared by the rules and optional LLM recovery paths."""

    max_source_level: int = 2
    max_level: int = 6
    # Keep title recovery aligned with the shared ChatOpenAI request timeout.
    llm_timeout_seconds: float = 600.0
    llm_max_retries: int = 2


@dataclass(frozen=True, slots=True)
class HeadingRecoveryResult:
    headings: list[dict[str, int | str]]
    mode: Literal["source", "rules", "rules_then_llm", "fallback"]
    diagnostics: tuple[str, ...] = ()

    def as_metadata(self) -> dict[str, str | list[str]]:
        return {"mode": self.mode, "diagnostics": list(self.diagnostics)}


@dataclass(frozen=True, slots=True)
class _OutlineMarker:
    family: Literal["arabic", "cjk"]
    depth: int
    parts: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class _Heading:
    title: str
    line_num: int
    raw_level: int
    level: int
    marker: _OutlineMarker | None
    confidence: HeadingConfidence = "source"


def _cjk_number(value: str) -> int | None:
    if value.isdigit():
        return int(value)
    total = 0
    current = 0
    for char in value:
        if char in _CJK_DIGITS:
            current = _CJK_DIGITS[char]
            continue
        unit = _CJK_UNITS.get(char)
        if unit is None:
            return None
        total += (current or 1) * unit
        current = 0
    value_number = total + current
    return value_number if value_number > 0 else None


def _marker_for_title(title: str) -> _OutlineMarker | None:
    arabic = _ARABIC_OUTLINE_RE.match(title)
    if arabic is not None:
        parts = tuple(int(part) for part in re.split(r"[.．]", arabic["number"]))
        if all(part > 0 for part in parts):
            return _OutlineMarker("arabic", len(parts), parts)

    chapter = _CJK_CHAPTER_RE.match(title)
    primary = _CJK_PRIMARY_RE.match(title)
    for match in (chapter, primary):
        if match is None:
            continue
        value = _cjk_number(match["number"])
        if value is not None:
            return _OutlineMarker("cjk", 1, (value,))

    for pattern in (_CJK_PAREN_RE, _ARABIC_PAREN_RE):
        match = pattern.match(title)
        if match is None:
            continue
        value = _cjk_number(match["number"])
        if value is not None:
            return _OutlineMarker("cjk", 2, (value,))
    return None


def _normalize_headings(headings: Sequence[dict[str, Any]]) -> list[_Heading]:
    normalized: list[_Heading] = []
    for item in headings:
        title = item.get("title")
        line_num = item.get("line_num")
        level = item.get("level")
        if (
            not isinstance(title, str)
            or not isinstance(line_num, int)
            or isinstance(line_num, bool)
            or line_num < 1
            or not isinstance(level, int)
            or isinstance(level, bool)
            or level < 1
        ):
            raise ValueError(
                "heading must contain title, positive level, and 1-based line_num"
            )
        normalized.append(
            _Heading(title, line_num, level, level, _marker_for_title(title))
        )
    return normalized


def _source_result(
    headings: list[_Heading], *diagnostics: str
) -> HeadingRecoveryResult:
    return HeadingRecoveryResult(
        [
            {
                "title": heading.title,
                "line_num": heading.line_num,
                "level": heading.level,
            }
            for heading in headings
        ],
        "source",
        tuple(diagnostics),
    )


def _has_rule_evidence(headings: list[_Heading], config: HeadingRecoveryConfig) -> bool:
    if (
        len(headings) < 2
        or max(heading.raw_level for heading in headings) > config.max_source_level
    ):
        return False
    markers = [heading.marker for heading in headings if heading.marker is not None]
    if len(markers) < 2 or not any(marker.depth > 1 for marker in markers):
        return False
    family_counts: dict[str, int] = {}
    for marker in markers:
        family_counts[marker.family] = family_counts.get(marker.family, 0) + 1
    return any(count >= 2 for count in family_counts.values())


def recover_heading_levels(
    headings: Sequence[dict[str, Any]],
    *,
    config: HeadingRecoveryConfig | None = None,
) -> HeadingRecoveryResult:
    """Recover numbered-outline levels using only the existing ordered headings.

    This function is intentionally source-agnostic.  It can consume headings from
    Markdown, HTML, office converters, or a PDF parser as long as each item has
    ``title``, ``level``, and ``line_num``.  It never changes title text or line
    locations and only alters levels when the outline evidence is strong enough.
    """
    resolved_config = config or HeadingRecoveryConfig()
    source = _normalize_headings(headings)
    if not source:
        return _source_result(source, "heading_recovery=no_headings")
    if not _has_rule_evidence(source, resolved_config):
        return _source_result(source, "heading_recovery=no_rule_evidence")

    has_document_title = any(
        heading.raw_level == 1 and heading.marker is None for heading in source
    )
    offset = 1 if has_document_title else 0
    seen_arabic: set[tuple[int, ...]] = set()
    seen_cjk_primary = False
    recovered: list[_Heading] = []
    changed_count = 0

    for heading in source:
        marker = heading.marker
        if marker is None:
            recovered.append(heading)
            continue

        candidate_level = marker.depth + offset
        parent_seen = True
        if marker.family == "arabic":
            if marker.depth > 1:
                parent_seen = marker.parts[:-1] in seen_arabic
            seen_arabic.add(marker.parts)
        elif marker.depth > 1:
            parent_seen = seen_cjk_primary
        else:
            seen_cjk_primary = True

        if not parent_seen:
            candidate_level = 1 + offset
        candidate_level = min(max(candidate_level, 1), resolved_config.max_level)
        if candidate_level != heading.raw_level:
            changed_count += 1
            recovered.append(
                _Heading(
                    heading.title,
                    heading.line_num,
                    heading.raw_level,
                    candidate_level,
                    marker,
                    "rule",
                )
            )
        else:
            recovered.append(heading)

    if changed_count == 0:
        return _source_result(source, "heading_recovery=rules_no_change")
    return HeadingRecoveryResult(
        [
            {
                "title": heading.title,
                "line_num": heading.line_num,
                "level": heading.level,
            }
            for heading in recovered
        ],
        "rules",
        ("heading_recovery=rules", f"rule_recovered={changed_count}"),
    )


def _llm_prompt(headings: list[_Heading], config: HeadingRecoveryConfig) -> str:
    payload = [
        {
            "id": index,
            "title": heading.title,
            "line_num": heading.line_num,
            "source_level": heading.raw_level,
            "rule_level": heading.level,
            "locked": heading.confidence == "rule" or heading.raw_level == 1,
        }
        for index, heading in enumerate(headings)
    ]
    return (
        "You assign hierarchical levels to an ordered list of existing document headings.\n"
        "Do not add, remove, rename, reorder, or otherwise modify headings.\n"
        "Keep every locked heading at its rule_level. Use source text and numbering as evidence.\n"
        f'Return only JSON in the form {{"levels": {{"0": 1}}}}. Levels must be integers from 1 to {config.max_level}; a heading may deepen by at most one level relative to its predecessor.\n'
        f"headings={json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}"
    )


def _decode_llm_levels(
    response: Any,
    headings: list[_Heading],
    config: HeadingRecoveryConfig,
) -> list[int]:
    raw = content_to_text(response).strip()
    if raw.startswith("```"):
        first_newline = raw.find("\n")
        raw = raw[first_newline + 1 :] if first_newline >= 0 else ""
        if raw.endswith("```"):
            raw = raw[:-3].rstrip()
    payload = repair_json(raw, return_objects=True)
    if not isinstance(payload, dict) or not isinstance(payload.get("levels"), dict):
        raise ValueError("title-level response must contain a levels object")
    levels = payload["levels"]
    expected_ids = {str(index) for index in range(len(headings))}
    if set(levels) != expected_ids:
        raise ValueError("title-level response IDs do not match input headings")

    parsed: list[int] = []
    for index, heading in enumerate(headings):
        level = levels[str(index)]
        if (
            not isinstance(level, int)
            or isinstance(level, bool)
            or not 1 <= level <= config.max_level
        ):
            raise ValueError("title-level response contains an invalid level")
        if (
            heading.confidence == "rule" or heading.raw_level == 1
        ) and level != heading.level:
            raise ValueError("title-level response changed a locked heading")
        if parsed and level > parsed[-1] + 1:
            raise ValueError("title-level response skips an intermediate level")
        parsed.append(level)
    return parsed


def _llm_failure_reason(
    error: Exception, *, stage: Literal["invoke", "response"]
) -> str:
    """Return a diagnostic reason without serializing provider error text."""
    if isinstance(error, TimeoutError):
        return "timeout"
    if stage == "response":
        validation_reasons = {
            "title-level response must contain a levels object": "missing_levels",
            "title-level response IDs do not match input headings": "heading_ids",
            "title-level response contains an invalid level": "invalid_level",
            "title-level response changed a locked heading": "changed_locked_level",
            "title-level response skips an intermediate level": "skipped_level",
        }
        return f"invalid_response:{validation_reasons.get(str(error), 'unparseable')}"

    status_code = getattr(error, "status_code", None)
    response = getattr(error, "response", None)
    if not isinstance(status_code, int) and response is not None:
        status_code = getattr(response, "status_code", None)
    if isinstance(status_code, int) and 100 <= status_code <= 599:
        return f"invoke_failed:http_{status_code}"
    return "invoke_failed"


async def recover_heading_levels_with_llm(
    headings: Sequence[dict[str, Any]],
    llm: LLMInvoker | None,
    *,
    config: HeadingRecoveryConfig | None = None,
) -> HeadingRecoveryResult:
    """Optionally refine unresolved headings through a constrained LLM response.

    Any unavailable or invalid model response retains deterministic rule output.
    The method does not log the heading payload, which may contain document data.
    """
    resolved_config = config or HeadingRecoveryConfig()
    rules_result = recover_heading_levels(headings, config=resolved_config)
    source_headings = _normalize_headings(headings)
    if not source_headings:
        return rules_result
    if llm is None:
        return HeadingRecoveryResult(
            rules_result.headings,
            "fallback",
            (*rules_result.diagnostics, "llm_fallback=unavailable"),
        )

    rule_headings = _normalize_headings(rules_result.headings)
    enriched = [
        _Heading(
            heading.title,
            heading.line_num,
            source_headings[index].raw_level,
            heading.level,
            _marker_for_title(heading.title),
            "rule"
            if heading.level != source_headings[index].raw_level
            or (heading.marker is not None and heading.marker.depth == 1)
            else "source",
        )
        for index, heading in enumerate(rule_headings)
    ]
    prompt = _llm_prompt(enriched, resolved_config)
    attempts = resolved_config.llm_max_retries + 1
    failure_reason = "invoke_failed"
    for attempt in range(1, attempts + 1):
        try:
            response = await asyncio.wait_for(
                llm.ainvoke(prompt, temperature=0),
                resolved_config.llm_timeout_seconds,
            )
        except Exception as error:
            failure_reason = _llm_failure_reason(error, stage="invoke")
        else:
            try:
                levels = _decode_llm_levels(response, enriched, resolved_config)
            except Exception as error:
                failure_reason = _llm_failure_reason(error, stage="response")
            else:
                changed_count = sum(
                    level != heading.level for level, heading in zip(levels, enriched)
                )
                return HeadingRecoveryResult(
                    [
                        {
                            "title": heading.title,
                            "line_num": heading.line_num,
                            "level": level,
                        }
                        for heading, level in zip(enriched, levels)
                    ],
                    "rules_then_llm",
                    (*rules_result.diagnostics, f"llm_recovered={changed_count}"),
                )
        logger.warning(
            "heading recovery LLM attempt failed: stage=%s reason=%s attempt=%d/%d headings=%d",
            "response" if failure_reason.startswith("invalid_response") else "invoke",
            failure_reason,
            attempt,
            attempts,
            len(enriched),
        )
    logger.error(
        "heading recovery LLM fallback: reason=%s attempts=%d headings=%d",
        failure_reason,
        attempts,
        len(enriched),
    )
    return HeadingRecoveryResult(
        rules_result.headings,
        "fallback",
        (
            *rules_result.diagnostics,
            f"llm_fallback={failure_reason}",
            f"llm_attempts={attempts}",
        ),
    )


__all__ = [
    "HeadingRecoveryConfig",
    "HeadingRecoveryMode",
    "HeadingRecoveryResult",
    "recover_heading_levels",
    "recover_heading_levels_with_llm",
]
