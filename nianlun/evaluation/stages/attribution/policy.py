"""Deterministic candidate policy for error attribution."""

from nianlun.evaluation.contracts.enums import (
    AttributionCategory,
    RetrievalCoverage,
    RetrievalNoise,
    SupportLevel,
)
from nianlun.evaluation.stages.correctness.schema import CorrectnessAssessment
from nianlun.evaluation.stages.evidence.schema import EvidenceResult


def allowed_attributions(
    correctness: CorrectnessAssessment,
    evidence: EvidenceResult,
    contexts_truncated: bool,
) -> list[AttributionCategory]:
    """Return the only candidates the attribution model may choose from.

    Candidate gating combines retrieval coverage, the aggregate support
    level, claim-level assessments, and the critic's missing/incorrect fact
    lists, so a valid root cause is never excluded by a single aggregate
    value.
    """
    if contexts_truncated:
        return [AttributionCategory.UNKNOWN]
    coverage = evidence.retrieval_coverage.value
    if coverage is RetrievalCoverage.NONE:
        return [AttributionCategory.RETRIEVAL_MISSING, AttributionCategory.UNKNOWN]
    if coverage is RetrievalCoverage.UNCERTAIN:
        return [AttributionCategory.UNKNOWN]
    support = evidence.actual_answer_support.value
    claim_values = {claim.value for claim in evidence.actual_claim_assessments}
    has_unsupported_claim = bool(
        claim_values & {SupportLevel.NONE, SupportLevel.CONFLICTING}
    )
    candidates: list[AttributionCategory] = []
    if coverage is RetrievalCoverage.PARTIAL:
        # Partial coverage starts the candidate set; critic and claim-level
        # signals below can still add generation-side root causes.
        candidates.extend(
            [
                AttributionCategory.RETRIEVAL_INCOMPLETE,
                AttributionCategory.HALLUCINATION,
            ]
        )
    if support is SupportLevel.PARTIAL or correctness.missing_facts:
        candidates.append(AttributionCategory.GENERATION_INCOMPLETE)
    if (
        support in {SupportLevel.NONE, SupportLevel.CONFLICTING}
        or has_unsupported_claim
        or correctness.incorrect_claims
    ):
        candidates.append(AttributionCategory.HALLUCINATION)
    if (
        support in {SupportLevel.NONE, SupportLevel.CONFLICTING}
        or correctness.incorrect_claims
    ):
        candidates.append(AttributionCategory.REASONING_ERROR)
    if (
        evidence.retrieval_noise.value is RetrievalNoise.SUBSTANTIAL
        and evidence.retrieval_noise.context_ids
    ):
        candidates.append(AttributionCategory.RETRIEVAL_NOISE)
    candidates.append(AttributionCategory.UNKNOWN)
    return list(dict.fromkeys(candidates))
