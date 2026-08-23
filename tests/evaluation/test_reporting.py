from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import TypeVar

import pytest
from pydantic import BaseModel

from nianlun.evaluation import EvaluationCase, RagEvaluator, summarize_results
from nianlun.evaluation.contracts import (
    AnswerVerdict,
    AttributionCategory,
    AttributionStrength,
    ContextItem,
    EvidenceConsistency,
    JudgeMetadata,
    ReferenceQuality,
    RetrievalCoverage,
    RetrievalNoise,
    SupportLevel,
)
from nianlun.evaluation.stages.correctness.schema import (
    CorrectnessAssessment,
    CorrectnessResult,
    ReferenceQualityAssessment,
)
from nianlun.evaluation.stages.critic.schema import CriticResult
from nianlun.evaluation.stages.evidence.schema import (
    ClaimEvidenceAssessment,
    EvidenceConsistencyAssessment,
    EvidenceModelOutput,
    RetrievalCoverageAssessment,
    RetrievalNoiseAssessment,
)

T = TypeVar("T", bound=BaseModel)


class SummaryJudge:
    metadata = JudgeMetadata(provider="fake", model="fake", temperature=0.0)

    async def generate_structured_output(self, *, prompt: str, schema: type[T]) -> T:
        del prompt
        values: Mapping[type[BaseModel], BaseModel] = {
            CorrectnessResult: CorrectnessResult(
                correctness=CorrectnessAssessment(
                    value=AnswerVerdict.CORRECT,
                    reason="correct",
                ),
                reference_quality=ReferenceQualityAssessment(
                    value=ReferenceQuality.ADEQUATE,
                    reason="the reference is adequate",
                ),
            ),
            EvidenceModelOutput: EvidenceModelOutput(
                retrieval_coverage=RetrievalCoverageAssessment(
                    value=RetrievalCoverage.FULL,
                    reason="complete evidence",
                    context_ids=["ctx-1"],
                ),
                retrieval_noise=RetrievalNoiseAssessment(
                    value=RetrievalNoise.NONE, reason="no noise"
                ),
                evidence_consistency=EvidenceConsistencyAssessment(
                    value=EvidenceConsistency.CONSISTENT, reason="consistent"
                ),
                reference_claim_assessments=[
                    ClaimEvidenceAssessment(
                        value=SupportLevel.FULL,
                        reason="reference claim supported",
                        claim="reference claim",
                        context_ids=["ctx-1"],
                    )
                ],
                actual_claim_assessments=[
                    ClaimEvidenceAssessment(
                        value=SupportLevel.FULL,
                        reason="actual claim supported",
                        claim="actual claim",
                        context_ids=["ctx-1"],
                    )
                ],
            ),
            CriticResult: CriticResult(
                correctness=CorrectnessAssessment(
                    value=AnswerVerdict.CORRECT,
                    reason="correct",
                ),
                reference_quality=ReferenceQualityAssessment(
                    value=ReferenceQuality.ADEQUATE,
                    reason="the reference is adequate",
                ),
            ),
        }
        return schema.model_validate(values[schema])


def test_summary_uses_top_level_deterministic_attributions() -> None:
    evaluator = RagEvaluator(judge=SummaryJudge())
    empty = EvaluationCase(
        question="q",
        reference_answer="r",
        actual_answer="",
        retrieval_contexts=[],
    )
    correct = EvaluationCase(
        question="q",
        reference_answer="r",
        actual_answer="a",
        retrieval_contexts=[ContextItem(text="evidence")],
    )

    async def evaluate_both():
        return await asyncio.gather(
            evaluator.evaluate(empty), evaluator.evaluate(correct)
        )

    results = asyncio.run(evaluate_both())

    summary = summarize_results(results)

    assert summary.total_cases == 2
    assert summary.correct_verdict_rate == 0.5
    assert summary.evaluation_failure_rate == 0.0
    assert summary.critic_invocation_rate == 0.5
    assert summary.primary_attribution_counts[AttributionCategory.GENERATION_EMPTY] == 1
    assert summary.attribution_strength_counts[AttributionStrength.STRONG] == 1
    assert summary.reference_answer_support_counts[SupportLevel.FULL] == 1
    assert summary.actual_answer_support_counts[SupportLevel.FULL] == 1
    assert summary.usage.calls == 3


def test_summary_rejects_mixed_evaluator_fingerprints() -> None:
    evaluator = RagEvaluator(judge=SummaryJudge())
    case = EvaluationCase(
        question="q",
        reference_answer="r",
        actual_answer="",
        retrieval_contexts=[],
    )
    result = asyncio.run(evaluator.evaluate(case))
    mixed = result.model_copy(
        update={
            "run_logs": result.run_logs.model_copy(
                update={"evaluator_fingerprint": "sha256:other"}
            )
        }
    )

    with pytest.raises(ValueError, match="mixed evaluator fingerprints"):
        summarize_results([result, mixed])
