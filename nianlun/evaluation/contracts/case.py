"""Input contracts and context normalization."""

from __future__ import annotations

import hashlib
import json

from pydantic import Field, field_validator

from nianlun.evaluation.contracts.base import EvaluationSchema


class ContextItem(EvaluationSchema):
    text: str = Field(min_length=1)
    context_id: str | None = None
    source_id: str | None = None
    source_name: str | None = None
    location: str | None = None
    score: float | None = None

    @field_validator("text")
    @classmethod
    def reject_blank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("context text cannot be blank")
        return value


class EvaluationCase(EvaluationSchema):
    question: str = Field(min_length=1)
    reference_answer: str = Field(min_length=1)
    actual_answer: str
    retrieval_contexts: list[ContextItem]

    @field_validator("question", "reference_answer")
    @classmethod
    def reject_blank_required_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("required text cannot be blank")
        return value

    @property
    def is_empty_answer(self) -> bool:
        return not self.actual_answer.strip()


def context_ids(case: EvaluationCase) -> set[str]:
    """Return the explicitly normalized context IDs available to a stage."""
    return {
        context.context_id for context in case.retrieval_contexts if context.context_id
    }


def normalize_contexts(case: EvaluationCase) -> EvaluationCase:
    """Assign stable batch-local IDs and reject duplicate explicit IDs."""
    seen: set[str] = set()
    normalized: list[ContextItem] = []
    for index, context in enumerate(case.retrieval_contexts, start=1):
        context_id = context.context_id or f"ctx-{index}"
        if context_id in seen:
            raise ValueError(f"duplicate context_id: {context_id}")
        seen.add(context_id)
        normalized.append(context.model_copy(update={"context_id": context_id}))
    return case.model_copy(update={"retrieval_contexts": normalized})


def case_fingerprint(case: EvaluationCase) -> str:
    """Return the stable hash of the normalized four-field input contract."""
    payload = case.model_dump(mode="json", exclude_none=False)
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return f"sha256:{hashlib.sha256(encoded.encode()).hexdigest()}"
