"""Structured output and route record owned by the critic stage."""

from pydantic import Field

from nianlun.evaluation.contracts.base import EvaluationSchema
from nianlun.evaluation.contracts.enums import (
    CriticDecision,
    CriticPromptId,
    RoutingFlag,
)
from nianlun.evaluation.stages.correctness.schema import CorrectnessResult


class CriticResult(CorrectnessResult):
    """Corrected answer and reference assessments produced by the critic."""


class CriticRunRecord(EvaluationSchema):
    """Critic execution metadata; result is retained only for failed runs."""

    critic_prompt_id: CriticPromptId
    critic_prompt_version: str = Field(min_length=1)
    routing_flags: list[RoutingFlag] = Field(default_factory=list)
    decision: CriticDecision
    overruled_correctness_result: bool
    result: CriticResult | None = None
