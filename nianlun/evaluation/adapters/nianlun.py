"""Explicit conversion of Nianlun batch records into the generic case contract."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from nianlun.evaluation.contracts.case import ContextItem, EvaluationCase


def adapt_nianlun_result(record: Mapping[str, Any]) -> EvaluationCase:
    """Map a Nianlun result record without leaking private execution metadata."""
    if record.get("success") is False:
        raise ValueError("cannot evaluate an unsuccessful Nianlun batch record")
    snippets = record.get("retrieved_snippets", [])
    if not isinstance(snippets, Sequence) or isinstance(snippets, (str, bytes)):
        raise ValueError("retrieved_snippets must be a list")
    contexts: list[ContextItem] = []
    for snippet in snippets:
        if not isinstance(snippet, Mapping):
            raise ValueError("retrieved_snippets items must be objects")
        contexts.append(
            ContextItem(
                text=_required_text(snippet, "text"),
                context_id=_optional_identifier(snippet, "citation_id"),
                source_id=_optional_text(snippet, "doc_id"),
                source_name=_optional_text(snippet, "doc_name"),
                location=_location(snippet),
                score=_optional_score(snippet.get("score")),
            )
        )
    return EvaluationCase(
        question=_required_text(record, "question"),
        reference_answer=_required_text(record, "expected_answer"),
        actual_answer=_answer_text(record),
        retrieval_contexts=contexts,
    )


def _required_text(value: Mapping[str, Any], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item.strip():
        raise ValueError(f"{key} must be a non-blank string")
    return item


def _optional_text(value: Mapping[str, Any], key: str) -> str | None:
    item = value.get(key)
    if item is None:
        return None
    if not isinstance(item, str):
        raise ValueError(f"{key} must be a string when supplied")
    return item or None


def _optional_identifier(value: Mapping[str, Any], key: str) -> str | None:
    item = value.get(key)
    if item is None:
        return None
    if isinstance(item, bool) or not isinstance(item, (str, int)):
        raise ValueError(f"{key} must be a string or integer when supplied")
    text = str(item)
    return text or None


def _answer_text(value: Mapping[str, Any]) -> str:
    item = value.get("agent_answer")
    if item is None:
        return ""
    if not isinstance(item, str):
        raise ValueError("agent_answer must be a string or null")
    return item


def _optional_score(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("score must be a number when supplied")
    return float(value)


def _location(value: Mapping[str, Any]) -> str | None:
    node_id = _optional_text(value, "node_id")
    line_spec = _optional_text(value, "line_spec")
    return ":".join(item for item in (node_id, line_spec) if item) or None
