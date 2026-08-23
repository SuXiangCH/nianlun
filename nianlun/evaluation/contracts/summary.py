"""Typed aggregate report for metrics observable without external human labels."""

from __future__ import annotations

from pydantic import Field

from nianlun.evaluation.contracts.base import EvaluationSchema
from nianlun.evaluation.contracts.enums import (
    AnswerVerdict,
    AttributionCategory,
    AttributionStrength,
    CriticDecision,
    CriticPromptId,
    EvidenceConsistency,
    HallucinationEvidence,
    ReferenceQuality,
    RetrievalCoverage,
    RetrievalNoise,
    RoutingFlag,
    SupportLevel,
)
from nianlun.evaluation.contracts.run_logs import (
    EvaluationUsage,
    StructuredOutputStats,
)


class EvaluationSummary(EvaluationSchema):
    """Aggregate only internally observable metrics for one evaluator fingerprint."""

    evaluator_fingerprint: str | None
    total_cases: int = Field(ge=0)
    completed_cases: int = Field(ge=0)
    failed_cases: int = Field(ge=0)
    evidence_cases: int = Field(ge=0)
    critic_cases: int = Field(ge=0)
    attributed_cases: int = Field(ge=0)
    verdict_counts: dict[AnswerVerdict, int]
    reference_quality_counts: dict[ReferenceQuality, int]
    retrieval_coverage_counts: dict[RetrievalCoverage, int]
    retrieval_noise_counts: dict[RetrievalNoise, int]
    evidence_consistency_counts: dict[EvidenceConsistency, int]
    reference_answer_support_counts: dict[SupportLevel, int]
    actual_answer_support_counts: dict[SupportLevel, int]
    primary_attribution_counts: dict[AttributionCategory, int]
    secondary_issue_counts: dict[AttributionCategory, int]
    attribution_strength_counts: dict[AttributionStrength, int]
    hallucination_evidence_counts: dict[HallucinationEvidence, int]
    critic_prompt_counts: dict[CriticPromptId, int]
    critic_decision_counts: dict[CriticDecision, int]
    routing_flag_counts: dict[RoutingFlag, int]
    correct_verdict_rate: float | None = Field(ge=0.0, le=1.0)
    decidable_correct_verdict_rate: float | None = Field(ge=0.0, le=1.0)
    partial_correctness_rate: float | None = Field(ge=0.0, le=1.0)
    incorrect_rate: float | None = Field(ge=0.0, le=1.0)
    uncertain_rate: float | None = Field(ge=0.0, le=1.0)
    evaluation_failure_rate: float | None = Field(ge=0.0, le=1.0)
    critic_invocation_rate: float | None = Field(ge=0.0, le=1.0)
    preliminary_overturn_rate: float | None = Field(ge=0.0, le=1.0)
    false_negative_recovery_rate: float | None = Field(ge=0.0, le=1.0)
    false_positive_correction_rate: float | None = Field(ge=0.0, le=1.0)
    verdict_evidence_tension_rate: float | None = Field(ge=0.0, le=1.0)
    usage: EvaluationUsage
    structured_output: StructuredOutputStats
    duration_ms_total: int = Field(ge=0)
