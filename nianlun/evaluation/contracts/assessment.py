"""Minimal contract shared by every independently reported metric."""

from __future__ import annotations

from typing import Generic, TypeVar

from pydantic import Field, field_validator

from nianlun.evaluation.contracts.base import EvaluationSchema

MetricValueT = TypeVar("MetricValueT")


class MetricAssessment(EvaluationSchema, Generic[MetricValueT]):
    """One metric value paired with its explanation."""

    value: MetricValueT = Field(description="The assessed value for this metric.")
    reason: str = Field(
        min_length=1,
        description="A concise explanation grounded in the supplied evaluation input.",
    )

    @field_validator("reason")
    @classmethod
    def reject_blank_reason(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("reason cannot be blank")
        return value
