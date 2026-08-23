"""Structured output owned by the answer-correctness stage."""

from pydantic import Field

from nianlun.evaluation.contracts.assessment import MetricAssessment
from nianlun.evaluation.contracts.base import EvaluationSchema
from nianlun.evaluation.contracts.enums import AnswerVerdict, ReferenceQuality


class ReferenceQualityAssessment(MetricAssessment[ReferenceQuality]):
    """Assessment of whether the reference answer is usable for judging."""


class CorrectnessAssessment(MetricAssessment[AnswerVerdict]):
    """Assessment of the actual answer against the question and reference."""

    matched_facts: list[str] = Field(default_factory=list)
    missing_facts: list[str] = Field(default_factory=list)
    incorrect_claims: list[str] = Field(default_factory=list)


class CorrectnessResult(EvaluationSchema):
    """Paired answer-correctness and reference-quality assessments."""

    correctness: CorrectnessAssessment
    reference_quality: ReferenceQualityAssessment
