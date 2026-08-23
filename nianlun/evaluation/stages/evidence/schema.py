"""Structured output owned by the retrieval-evidence stage."""

from typing import Any, Generic, TypeVar

from pydantic import Field, field_validator, model_validator

from nianlun.evaluation.contracts.assessment import MetricAssessment
from nianlun.evaluation.contracts.base import EvaluationSchema
from nianlun.evaluation.contracts.enums import (
    EvidenceConsistency,
    RetrievalCoverage,
    RetrievalNoise,
    SupportLevel,
)

MetricValueT = TypeVar("MetricValueT")


class ClaimEvidenceAssessment(MetricAssessment[SupportLevel]):
    """Support for one material answer claim."""

    claim: str = Field(min_length=1)
    context_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_evidence_ids(cls, value: Any) -> Any:
        """Accept persisted pre-2.6 claims without exposing their fields to the model."""
        if not isinstance(value, dict):
            return value
        payload = dict(value)
        supporting = payload.pop("supporting_context_ids", None)
        conflicting = payload.pop("conflicting_context_ids", None)
        if supporting is None and conflicting is None:
            return payload
        if "context_ids" in payload:
            raise ValueError("legacy evidence IDs cannot be combined with context_ids")
        # For legacy conflicting claims, contradictory citations preserve the new field's meaning.
        payload["context_ids"] = conflicting or supporting or []
        return payload

    @field_validator("claim")
    @classmethod
    def reject_blank_claim(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("claim cannot be blank")
        return value

    @model_validator(mode="after")
    def validate_evidence_ids(self) -> "ClaimEvidenceAssessment":
        if len(set(self.context_ids)) != len(self.context_ids):
            raise ValueError("context_ids cannot contain duplicates")
        if (
            self.value in {SupportLevel.NONE, SupportLevel.UNCERTAIN}
            and self.context_ids
        ):
            raise ValueError(f"{self.value.value} support cannot cite evidence")
        if self.value in {SupportLevel.FULL, SupportLevel.PARTIAL}:
            if not self.context_ids:
                raise ValueError(f"{self.value.value} support requires context_ids")
        if self.value is SupportLevel.CONFLICTING and not self.context_ids:
            raise ValueError("conflicting support requires conflicting evidence")
        return self


class EvidenceMetricAssessment(MetricAssessment[MetricValueT], Generic[MetricValueT]):
    """One evidence metric with the contexts that justify it."""

    context_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_unique_context_ids(self) -> "EvidenceMetricAssessment[MetricValueT]":
        if len(set(self.context_ids)) != len(self.context_ids):
            raise ValueError("context_ids cannot contain duplicates")
        return self


class RetrievalCoverageAssessment(EvidenceMetricAssessment[RetrievalCoverage]):
    @model_validator(mode="after")
    def validate_coverage_evidence(self) -> "RetrievalCoverageAssessment":
        if self.value is RetrievalCoverage.NONE and self.context_ids:
            raise ValueError("none coverage cannot cite supporting evidence")
        if (
            self.value in {RetrievalCoverage.PARTIAL, RetrievalCoverage.FULL}
            and not self.context_ids
        ):
            raise ValueError(f"{self.value.value} coverage requires context_ids")
        return self


class RetrievalNoiseAssessment(EvidenceMetricAssessment[RetrievalNoise]):
    @model_validator(mode="after")
    def validate_noise_evidence(self) -> "RetrievalNoiseAssessment":
        if self.value is RetrievalNoise.NONE and self.context_ids:
            raise ValueError("none noise cannot cite noise evidence")
        if (
            self.value in {RetrievalNoise.LIMITED, RetrievalNoise.SUBSTANTIAL}
            and not self.context_ids
        ):
            raise ValueError(f"{self.value.value} noise requires context_ids")
        return self


class EvidenceConsistencyAssessment(EvidenceMetricAssessment[EvidenceConsistency]):
    @model_validator(mode="after")
    def validate_consistency_evidence(self) -> "EvidenceConsistencyAssessment":
        if self.value is EvidenceConsistency.CONFLICTING and not self.context_ids:
            raise ValueError("conflicting evidence requires context_ids")
        return self


class SupportAssessment(EvidenceMetricAssessment[SupportLevel]):
    @model_validator(mode="after")
    def validate_support_evidence(self) -> "SupportAssessment":
        if self.value is SupportLevel.NONE and self.context_ids:
            raise ValueError("none support cannot cite supporting evidence")
        if (
            self.value
            in {SupportLevel.FULL, SupportLevel.PARTIAL, SupportLevel.CONFLICTING}
            and not self.context_ids
        ):
            raise ValueError(f"{self.value.value} support requires context_ids")
        return self


class EvidenceModelOutput(EvaluationSchema):
    """Evidence observations emitted by the model before support aggregation."""

    retrieval_coverage: RetrievalCoverageAssessment
    retrieval_noise: RetrievalNoiseAssessment
    evidence_consistency: EvidenceConsistencyAssessment
    reference_claim_assessments: list[ClaimEvidenceAssessment] = Field(
        default_factory=list
    )
    actual_claim_assessments: list[ClaimEvidenceAssessment] = Field(
        default_factory=list
    )


class EvidenceResult(EvaluationSchema):
    """Independent retrieval-evidence metrics and claim-level observations."""

    retrieval_coverage: RetrievalCoverageAssessment
    retrieval_noise: RetrievalNoiseAssessment
    evidence_consistency: EvidenceConsistencyAssessment
    reference_answer_support: SupportAssessment
    actual_answer_support: SupportAssessment
    reference_claim_assessments: list[ClaimEvidenceAssessment] = Field(
        default_factory=list
    )
    actual_claim_assessments: list[ClaimEvidenceAssessment] = Field(
        default_factory=list
    )

    @model_validator(mode="after")
    def validate_claim_aggregates(self) -> "EvidenceResult":
        _validate_support_aggregate(
            self.reference_answer_support,
            self.reference_claim_assessments,
            "reference_answer_support",
        )
        _validate_support_aggregate(
            self.actual_answer_support,
            self.actual_claim_assessments,
            "actual_answer_support",
        )
        return self


def derive_evidence_result(output: EvidenceModelOutput) -> EvidenceResult:
    """Add deterministic answer-level support summaries to model observations."""
    return EvidenceResult(
        retrieval_coverage=output.retrieval_coverage,
        retrieval_noise=output.retrieval_noise,
        evidence_consistency=output.evidence_consistency,
        reference_answer_support=derive_support_assessment(
            output.reference_claim_assessments
        ),
        actual_answer_support=derive_support_assessment(
            output.actual_claim_assessments
        ),
        reference_claim_assessments=output.reference_claim_assessments,
        actual_claim_assessments=output.actual_claim_assessments,
    )


def derive_support_assessment(
    claims: list[ClaimEvidenceAssessment],
) -> SupportAssessment:
    """Derive answer-level support without asking the model to repeat claim evidence."""
    value = _aggregate_support_level(claims)
    return SupportAssessment(
        value=value,
        reason=f"Derived from {len(claims)} claim assessments.",
        context_ids=list(
            dict.fromkeys(
                context_id for claim in claims for context_id in claim.context_ids
            )
        ),
    )


def _validate_support_aggregate(
    aggregate: SupportAssessment, claims: list[ClaimEvidenceAssessment], field_name: str
) -> None:
    if not claims:
        if aggregate.value in {
            SupportLevel.FULL,
            SupportLevel.PARTIAL,
            SupportLevel.CONFLICTING,
        }:
            raise ValueError(
                f"{field_name}={aggregate.value.value} requires claim assessments"
            )
        return
    expected = _aggregate_support_level(claims)
    if aggregate.value is not expected:
        raise ValueError(
            f"{field_name}.value must agree with its claim assessments ({expected.value})"
        )
    claim_ids = {context_id for claim in claims for context_id in claim.context_ids}
    if not claim_ids.issubset(aggregate.context_ids):
        raise ValueError(
            f"{field_name}.context_ids must include all claim evidence IDs"
        )


def _aggregate_support_level(claims: list[ClaimEvidenceAssessment]) -> SupportLevel:
    if not claims:
        return SupportLevel.UNCERTAIN
    values = {claim.value for claim in claims}
    return (
        SupportLevel.CONFLICTING
        if SupportLevel.CONFLICTING in values
        else SupportLevel.UNCERTAIN
        if SupportLevel.UNCERTAIN in values
        else SupportLevel.FULL
        if values == {SupportLevel.FULL}
        else SupportLevel.NONE
        if values == {SupportLevel.NONE}
        else SupportLevel.PARTIAL
    )
