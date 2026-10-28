"""Structured results and size-bounded serialization for deep search."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field

from nianlun.agent.subagents.config import DeepSearchConfig
from nianlun.models.llm import content_to_text

DeepSearchStatus = Literal["completed", "failed"]


class EvidenceImageRef(BaseModel):
    """A child-local identity, never a parent authorization or storage locator."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    ref_id: str = Field(pattern=r"^img_[0-9]{2,}$", max_length=32)
    knowledge_base_id: str = Field(min_length=1, max_length=128)
    generation: int = Field(ge=1)
    content_version: int = Field(ge=0)
    image_id: str = Field(pattern=r"^img_[0-9a-f]{16,64}$")
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_start: int = Field(ge=0)
    source_end: int = Field(gt=0)
    text_start: int = Field(ge=0)
    text_end: int = Field(gt=0)


class EvidenceOutput(BaseModel):
    """Model-facing evidence schema used by LangGraph structured output."""

    model_config = ConfigDict(extra="ignore")

    doc_id: str | None = None
    doc_name: str | None = None
    node_id: str | None = None
    title: str | None = None
    line_num: int | None = None
    line_spec: str | None = None
    char_offset: int | None = None
    char_limit: int | None = None
    total_chars: int | None = None
    text_truncated: bool | None = None
    text: str = ""


class DeepSearchOutput(BaseModel):
    """Model-facing schema for the child Agent's final answer."""

    model_config = ConfigDict(extra="ignore")

    answer: str = ""
    evidence: list[EvidenceOutput] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    search_summary: str = ""


def _clip(value: Any, limit: int) -> str:
    text = "" if value is None else str(value)
    return text[:limit]


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


@dataclass(frozen=True, slots=True)
class Evidence:
    """A compact source location that can be persisted by the parent."""

    doc_id: str | None = None
    doc_name: str | None = None
    node_id: str | None = None
    title: str | None = None
    line_num: int | None = None
    line_spec: str | None = None
    char_offset: int | None = None
    char_limit: int | None = None
    total_chars: int | None = None
    text_truncated: bool | None = None
    text: str = ""
    image_refs: tuple[EvidenceImageRef, ...] = ()

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "Evidence":
        return cls(
            doc_id=_clip(value.get("doc_id"), 128) or None,
            doc_name=_clip(value.get("doc_name"), 256) or None,
            node_id=_clip(value.get("node_id"), 128) or None,
            title=_clip(value.get("title"), 256) or None,
            line_num=_optional_int(value.get("line_num")),
            line_spec=_clip(value.get("line_spec"), 128) or None,
            char_offset=_optional_int(value.get("char_offset")),
            char_limit=_optional_int(value.get("char_limit")),
            total_chars=_optional_int(value.get("total_chars")),
            text_truncated=(
                value.get("text_truncated")
                if isinstance(value.get("text_truncated"), bool)
                else None
            ),
            text=_clip(value.get("text"), 600),
        )

    def to_dict(
        self, *, text_limit: int = 600, image_ref_limit: int = 2
    ) -> dict[str, Any]:
        text = _clip(self.text, text_limit)
        payload: dict[str, Any] = {
            "doc_id": self.doc_id,
            "doc_name": self.doc_name,
            "node_id": self.node_id,
            "title": self.title,
            "line_num": self.line_num,
            "line_spec": self.line_spec,
            "char_offset": self.char_offset,
            "char_limit": self.char_limit,
            "total_chars": self.total_chars,
            "text_truncated": self.text_truncated,
            "text": text,
        }
        refs = [
            ref.model_dump()
            for ref in self.image_refs[:image_ref_limit]
            if (
                0 <= ref.text_start < ref.text_end <= len(text)
                and text[ref.text_start : ref.text_end].startswith("![")
                and text[ref.text_start : ref.text_end].endswith(
                    f"](nianlun-image:{ref.ref_id})"
                )
            )
        ]
        if refs:
            payload["image_refs"] = refs
        return payload


@dataclass(frozen=True, slots=True)
class DeepSearchResult:
    """The only result shape that may cross the parent/subagent boundary."""

    status: DeepSearchStatus
    answer: str = ""
    evidence: tuple[Evidence, ...] = ()
    open_questions: tuple[str, ...] = ()
    search_summary: str = ""
    error_code: str | None = None
    error_message: str | None = None

    def to_dict(self, config: DeepSearchConfig | None = None) -> dict[str, Any]:
        """Return a bounded JSON-compatible payload."""
        limits = config or DeepSearchConfig()
        if self.status != "completed":
            payload: dict[str, Any] = {"status": "failed"}
            if self.error_code or self.error_message:
                payload["error"] = {
                    "code": _clip(self.error_code, 64),
                    "message": _clip(self.error_message, 1_000),
                }
                while len(_json(payload)) > limits.max_result_chars:
                    error = payload["error"]
                    field = "message" if error["message"] else "code"
                    error[field] = error[field][: len(error[field]) // 2]
            return payload

        evidence = []
        remaining_refs = limits.max_image_refs
        for item in self.evidence[: limits.max_evidence_items]:
            serialized = item.to_dict(
                text_limit=limits.max_evidence_text_chars,
                image_ref_limit=min(limits.max_evidence_image_refs, remaining_refs),
            )
            remaining_refs -= len(serialized.get("image_refs", []))
            evidence.append(serialized)
        open_questions = [
            _clip(item, limits.max_open_question_chars)
            for item in self.open_questions[: limits.max_open_questions]
        ]
        answer = _clip(self.answer, limits.max_answer_chars)
        search_summary = _clip(self.search_summary, limits.max_search_summary_chars)

        def build_payload() -> dict[str, Any]:
            return {
                "status": "completed",
                "answer": answer,
                "evidence": evidence,
                "open_questions": open_questions,
                "search_summary": search_summary,
            }

        payload = build_payload()
        while len(_json(payload)) > limits.max_result_chars and evidence:
            evidence.pop()
            payload = build_payload()

        while len(_json(payload)) > limits.max_result_chars and open_questions:
            open_questions.pop()
            payload = build_payload()

        while len(_json(payload)) > limits.max_result_chars and search_summary:
            search_summary = search_summary[: max(0, len(search_summary) // 2)]
            payload = build_payload()

        if len(_json(payload)) > limits.max_result_chars and answer:
            low, high = 0, len(answer)
            best = ""
            while low <= high:
                middle = (low + high) // 2
                candidate = answer[:middle]
                answer = candidate
                candidate_payload = build_payload()
                if len(_json(candidate_payload)) <= limits.max_result_chars:
                    best = candidate
                    low = middle + 1
                else:
                    high = middle - 1
            answer = best
            payload = build_payload()

        # The configuration has bounded every field, so this is only reachable
        # when a caller deliberately chooses an unrealistically small limit.
        if len(_json(payload)) > limits.max_result_chars:
            payload = {
                "status": "completed",
                "answer": "",
                "evidence": [],
                "open_questions": [],
                "search_summary": "",
            }
        return payload

    def to_json(self, config: DeepSearchConfig | None = None) -> str:
        return _json(self.to_dict(config))


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def result_from_agent_output(
    output: Any,
    config: DeepSearchConfig | None = None,
) -> DeepSearchResult:
    """Normalize a child Agent output without exposing its message history."""
    if isinstance(output, DeepSearchResult):
        return output

    if isinstance(output, BaseModel):
        return result_from_agent_output(output.model_dump(), config)

    if isinstance(output, Mapping) and "structured_response" in output:
        return result_from_agent_output(output["structured_response"], config)

    if isinstance(output, Mapping) and output.get("status") == "failed":
        error = output.get("error")
        if isinstance(error, Mapping):
            return failed_result(
                str(error.get("code") or "subagent_failed"),
                str(error.get("message") or "Subagent failed."),
            )
        return failed_result("subagent_failed", "Subagent failed.")

    if isinstance(output, Mapping) and any(
        key in output
        for key in ("answer", "evidence", "open_questions", "search_summary")
    ):
        answer = output.get("answer")
        evidence_value = output.get("evidence", ())
        open_questions_value = output.get("open_questions", ())
        search_summary = output.get("search_summary", "")
        if not isinstance(evidence_value, (list, tuple)):
            evidence_value = ()
        evidence = tuple(
            Evidence.from_mapping(item)
            for item in evidence_value
            if isinstance(item, Mapping)
        )
        if not isinstance(open_questions_value, (list, tuple)):
            open_questions_value = ()
        open_questions = tuple(str(item) for item in open_questions_value)
        result = DeepSearchResult(
            status="completed",
            answer=_clip(answer, (config or DeepSearchConfig()).max_answer_chars),
            evidence=evidence,
            open_questions=open_questions,
            search_summary=_clip(
                search_summary, (config or DeepSearchConfig()).max_search_summary_chars
            ),
        )
        return result

    if isinstance(output, str):
        parsed = _parse_json_text(output)
        if parsed is not None:
            return result_from_agent_output(parsed, config)
        return DeepSearchResult(status="completed", answer=output)

    messages = output.get("messages", ()) if isinstance(output, Mapping) else ()
    for message in reversed(messages if isinstance(messages, (list, tuple)) else ()):
        role = getattr(message, "type", None) or getattr(message, "role", None)
        content = getattr(message, "content", None)
        if isinstance(message, Mapping):
            role = message.get("type") or message.get("role")
            content = message.get("content")
        if role in {"ai", "assistant"} and content:
            text = content_to_text(content)
            if text:
                parsed = _parse_json_text(text)
                if parsed is not None:
                    return result_from_agent_output(parsed, config)
                return DeepSearchResult(status="completed", answer=text)

    return DeepSearchResult(
        status="failed",
        error_code="empty_result",
        error_message="Subagent returned no usable research result.",
    )


def failed_result(code: str, message: str) -> DeepSearchResult:
    return DeepSearchResult(
        status="failed",
        error_code=code,
        error_message=message,
    )


def bound_result(
    result: DeepSearchResult, config: DeepSearchConfig
) -> DeepSearchResult:
    """Materialize the configured result limits before a result leaves the runner."""
    payload = result.to_dict(config)
    bounded = result_from_agent_output(payload, config)
    # Only this internal round trip preserves trusted references. The public
    # model-output parser deliberately ignores every supplied image identity.
    return replace(
        bounded,
        evidence=tuple(
            replace(
                item,
                image_refs=tuple(
                    EvidenceImageRef.model_validate(ref)
                    for ref in raw.get("image_refs", [])
                ),
            )
            for item, raw in zip(bounded.evidence, payload.get("evidence", []))
        ),
    )


def _parse_json_text(value: str) -> Mapping[str, Any] | None:
    text = value.strip()
    if text.startswith("```") and text.endswith("```"):
        lines = text.splitlines()
        if len(lines) >= 3:
            text = "\n".join(lines[1:-1]).strip()
    if not text.startswith("{"):
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, Mapping) else None


__all__ = [
    "DeepSearchResult",
    "DeepSearchStatus",
    "DeepSearchOutput",
    "EvidenceOutput",
    "Evidence",
    "EvidenceImageRef",
    "bound_result",
    "failed_result",
    "result_from_agent_output",
]
