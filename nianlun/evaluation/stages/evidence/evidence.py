"""Retrieval-evidence stage implementation and semantic validation."""

import logging

from nianlun.evaluation.contracts.case import EvaluationCase, context_ids
from nianlun.evaluation.contracts.enums import (
    RetrievalCoverage,
    RetrievalNoise,
)
from nianlun.evaluation.judge.runtime import EvaluationRuntime, StructuredGeneration
from nianlun.evaluation.stages.evidence.prompt import build_prompt
from nianlun.evaluation.stages.evidence.schema import (
    EvidenceModelOutput,
    EvidenceResult,
    derive_evidence_result,
)

logger = logging.getLogger(__name__)


class Evidence:
    def __init__(self, runtime: EvaluationRuntime) -> None:
        self.runtime = runtime

    async def evaluate(
        self,
        case: EvaluationCase,
    ) -> StructuredGeneration[EvidenceResult]:
        generation = await self.runtime.generate_structured(
            build_prompt(case),
            EvidenceModelOutput,
            semantic_validator=lambda value: validate_evidence_output(value, case),
        )
        result = derive_evidence_result(generation.output)
        validate_evidence_result(result, case)
        logger.info(
            "evaluation.evidence.derived coverage=%s consistency=%s reference_support=%s "
            "actual_support=%s reference_claims=%d actual_claims=%d",
            result.retrieval_coverage.value,
            result.evidence_consistency.value,
            result.reference_answer_support.value,
            result.actual_answer_support.value,
            len(result.reference_claim_assessments),
            len(result.actual_claim_assessments),
        )
        return StructuredGeneration(
            output=result,
            telemetry=generation.telemetry,
        )


def validate_evidence_output(output: EvidenceModelOutput, case: EvaluationCase) -> None:
    _validate_context_ids(output, context_ids(case))
    _validate_empty_retrieval(output, case)


def validate_evidence_result(result: EvidenceResult, case: EvaluationCase) -> None:
    _validate_context_ids(result, context_ids(case))
    _validate_empty_retrieval(result, case)


def _validate_empty_retrieval(
    result: EvidenceModelOutput | EvidenceResult, case: EvaluationCase
) -> None:
    if case.retrieval_contexts:
        return
    if result.retrieval_coverage.value is not RetrievalCoverage.NONE:
        raise ValueError("empty retrieval must have retrieval_coverage=none")
    if result.retrieval_noise.value is not RetrievalNoise.NONE:
        raise ValueError("empty retrieval must have retrieval_noise=none")
    if any(
        metric.context_ids
        for metric in (
            result.retrieval_coverage,
            result.retrieval_noise,
            result.evidence_consistency,
        )
    ):
        raise ValueError("empty retrieval cannot cite context IDs")


def _validate_context_ids(
    result: EvidenceModelOutput | EvidenceResult, valid_ids: set[str]
) -> None:
    referenced: set[str] = set()
    for metric in (
        result.retrieval_coverage,
        result.retrieval_noise,
        result.evidence_consistency,
    ):
        referenced.update(metric.context_ids)
    if isinstance(result, EvidenceResult):
        referenced.update(result.reference_answer_support.context_ids)
        referenced.update(result.actual_answer_support.context_ids)
    for assessment in [
        *result.reference_claim_assessments,
        *result.actual_claim_assessments,
    ]:
        referenced.update(assessment.context_ids)
    _ensure_context_ids(referenced, valid_ids)


def _ensure_context_ids(referenced: set[str], valid_ids: set[str]) -> None:
    unknown = referenced - valid_ids
    if unknown:
        raise ValueError(
            f"context_id references not present in the input: {sorted(unknown)}"
        )
