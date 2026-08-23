"""Structured output and route record owned by the attribution stage."""

from pydantic import Field, model_validator

from nianlun.evaluation.contracts.assessment import MetricAssessment
from nianlun.evaluation.contracts.base import EvaluationSchema
from nianlun.evaluation.contracts.enums import (
    AttributionCategory,
    AttributionStrength,
    HallucinationEvidence,
)


class HallucinatedClaim(EvaluationSchema):
    """Unsupported or contradicted claim identified during attribution."""

    claim: str = Field(
        min_length=1,
        description="The specific factual assertion from the actual answer.",
    )
    evidence: HallucinationEvidence = Field(
        description=(
            "Whether the supplied material leaves the claim unsupported or a retrieval "
            "context directly contradicts it."
        )
    )
    contradicted_by_reference: bool = Field(
        default=False,
        description=(
            "Whether the reference answer also directly conflicts with the claim. This is "
            "additional context and is not sufficient proof of contradiction by itself."
        ),
    )
    context_ids: list[str] = Field(
        default_factory=list,
        description=(
            "IDs of retrieval contexts that directly contradict the claim. Required for "
            "contradicted evidence and empty for unsupported evidence."
        ),
    )

    @model_validator(mode="after")
    def validate_evidence(self) -> "HallucinatedClaim":
        if self.evidence is HallucinationEvidence.UNSUPPORTED:
            if self.context_ids or self.contradicted_by_reference:
                raise ValueError("unsupported claim cannot cite contradictory evidence")
        elif not self.context_ids:
            raise ValueError(
                "contradicted claim requires a contradictory context citation"
            )
        return self


class AttributionAssessment(MetricAssessment[AttributionCategory]):
    """Final cause assessment with evidence for a defective answer."""

    value: AttributionCategory = Field(
        description="The one primary cause that most directly explains the answer defect."
    )
    secondary_issues: list[AttributionCategory] = Field(
        default_factory=list,
        description=(
            "Independently evidenced secondary causes that materially contributed to the "
            "defect; do not repeat the primary value."
        ),
    )
    attribution_strength: AttributionStrength = Field(
        description="Evidence strength for the primary attribution value, not for secondary issues."
    )
    hallucinated_claims: list[HallucinatedClaim] = Field(
        default_factory=list,
        description=(
            "Specific unsupported or contradicted assertions. Required when hallucination "
            "is a primary or secondary attribution."
        ),
    )
    omitted_facts: list[str] = Field(
        default_factory=list,
        description="Answer-essential facts omitted from the actual answer; empty when none are confirmed.",
    )
    reasoning_errors: list[str] = Field(
        default_factory=list,
        description="Traceable invalid calculations, comparisons, inferences, or temporal steps.",
    )
    noise_context_ids: list[str] = Field(
        default_factory=list,
        description=(
            "IDs of irrelevant or conflicting retrieval contexts directly linked to the "
            "defect; do not list unrelated noise."
        ),
    )

    @model_validator(mode="after")
    def validate_attribution(self) -> "AttributionAssessment":
        secondary = set(self.secondary_issues)
        if len(secondary) != len(self.secondary_issues):
            raise ValueError("secondary_issues cannot contain duplicates")
        if self.value in secondary:
            raise ValueError("primary attribution cannot be a secondary issue")
        if AttributionCategory.UNKNOWN in secondary:
            raise ValueError("unknown cannot be a secondary issue")
        is_unknown = self.value is AttributionCategory.UNKNOWN
        if is_unknown != (
            self.attribution_strength is AttributionStrength.INSUFFICIENT
        ):
            raise ValueError("unknown attribution must use insufficient strength")
        if (
            self.value is AttributionCategory.HALLUCINATION
            or AttributionCategory.HALLUCINATION in secondary
        ) and not self.hallucinated_claims:
            raise ValueError("hallucination attribution requires hallucinated_claims")
        return self


class AttributionRunRecord(EvaluationSchema):
    """Attribution routing metadata, not the final assessment itself."""

    allowed_attributions: list[AttributionCategory] = Field(min_length=1)
    deterministic: bool = False
