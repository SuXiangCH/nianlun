"""Error-attribution stage implementation and semantic validation."""

from collections.abc import Sequence

from nianlun.evaluation.contracts.case import EvaluationCase, context_ids
from nianlun.evaluation.contracts.enums import AttributionCategory
from nianlun.evaluation.judge.runtime import EvaluationRuntime, StructuredGeneration
from nianlun.evaluation.stages.attribution.prompt import build_prompt
from nianlun.evaluation.stages.attribution.schema import AttributionAssessment
from nianlun.evaluation.stages.critic.schema import CriticResult
from nianlun.evaluation.stages.evidence.schema import EvidenceResult


class Attribution:
    def __init__(self, runtime: EvaluationRuntime) -> None:
        self.runtime = runtime

    async def evaluate(
        self,
        case: EvaluationCase,
        evidence: EvidenceResult,
        critic: CriticResult,
        allowed: Sequence[AttributionCategory],
    ) -> StructuredGeneration[AttributionAssessment]:
        candidates = list(allowed)
        return await self.runtime.generate_structured(
            build_prompt(case, evidence, critic, candidates),
            AttributionAssessment,
            semantic_validator=lambda value: validate_attribution(
                value,
                candidates,
                context_ids(case),
            ),
        )


def validate_attribution(
    assessment: AttributionAssessment,
    allowed: list[AttributionCategory],
    valid_ids: set[str],
) -> None:
    selected = [assessment.value, *assessment.secondary_issues]
    if any(item not in allowed for item in selected):
        raise ValueError("attribution is outside allowed_attributions")
    _ensure_context_ids(assessment.noise_context_ids, valid_ids)
    for claim in assessment.hallucinated_claims:
        _ensure_context_ids(claim.context_ids, valid_ids)


def _ensure_context_ids(referenced: list[str], valid_ids: set[str]) -> None:
    unknown = set(referenced) - valid_ids
    if unknown:
        raise ValueError(
            f"context_id references not present in the input: {sorted(unknown)}"
        )
