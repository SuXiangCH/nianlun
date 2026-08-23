from __future__ import annotations

import asyncio
from collections import deque
import logging
from typing import TypeVar

import pytest
from pydantic import BaseModel, ValidationError

from nianlun.evaluation import EvaluationCase, EvaluationOutcome, RagEvaluator
from nianlun.evaluation.orchestration.pipeline import EvaluationConfig
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
    StructuredOutputStats,
    SupportLevel,
    case_fingerprint,
    normalize_contexts,
)
from nianlun.evaluation.stages.attribution.schema import AttributionAssessment
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
    EvidenceResult,
    RetrievalCoverageAssessment,
    RetrievalNoiseAssessment,
    SupportAssessment,
)

T = TypeVar("T", bound=BaseModel)


class FakeJudge:
    def __init__(self, *responses: BaseModel | Exception) -> None:
        self.responses = deque(responses)
        self.calls: list[type[BaseModel]] = []
        self.metadata = JudgeMetadata(provider="fake", model="fake", temperature=0.0)
        self.structured_output = StructuredOutputStats()

    async def generate_structured_output(self, *, prompt: str, schema: type[T]) -> T:
        del prompt
        self.calls.append(schema)
        response = self.responses.popleft()
        if isinstance(response, Exception):
            raise response
        if schema is EvidenceModelOutput and isinstance(response, EvidenceResult):
            return schema.model_validate(
                response.model_dump(
                    exclude={"reference_answer_support", "actual_answer_support"}
                )
            )
        return schema.model_validate(response)


def _case(
    *, answer: str = "Paris", contexts: list[ContextItem] | None = None
) -> EvaluationCase:
    return EvaluationCase(
        question="What is the capital of France?",
        reference_answer="Paris is the capital of France.",
        actual_answer=answer,
        retrieval_contexts=(
            [ContextItem(text="Paris is the capital of France.")]
            if contexts is None
            else contexts
        ),
    )


def _preliminary(
    verdict: AnswerVerdict = AnswerVerdict.CORRECT,
) -> CorrectnessResult:
    return CorrectnessResult(
        correctness=CorrectnessAssessment(
            value=verdict,
            reason="initial judgment",
        ),
        reference_quality=ReferenceQualityAssessment(
            value=ReferenceQuality.ADEQUATE,
            reason="the reference is adequate",
        ),
    )


def _review(
    *,
    support: SupportLevel = SupportLevel.FULL,
    coverage: RetrievalCoverage = RetrievalCoverage.FULL,
    consistency: EvidenceConsistency = EvidenceConsistency.CONSISTENT,
    reference_support: SupportLevel = SupportLevel.FULL,
) -> EvidenceResult:
    coverage_ids = (
        ["ctx-1"]
        if coverage in {RetrievalCoverage.FULL, RetrievalCoverage.PARTIAL}
        else []
    )
    support_ids = [] if support is SupportLevel.NONE else ["ctx-1"]
    reference_support_ids = [] if reference_support is SupportLevel.NONE else ["ctx-1"]
    consistency_ids = (
        ["ctx-1"] if consistency is EvidenceConsistency.CONFLICTING else []
    )
    reference_claims = _claims_for_support(reference_support, "reference claim")
    actual_claims = _claims_for_support(support, "actual claim")
    return EvidenceResult(
        retrieval_coverage=RetrievalCoverageAssessment(
            value=coverage, reason="coverage review", context_ids=coverage_ids
        ),
        retrieval_noise=RetrievalNoiseAssessment(
            value=RetrievalNoise.NONE, reason="noise review"
        ),
        evidence_consistency=EvidenceConsistencyAssessment(
            value=consistency,
            reason="consistency review",
            context_ids=consistency_ids,
        ),
        reference_answer_support=SupportAssessment(
            value=reference_support,
            reason="reference support review",
            context_ids=reference_support_ids,
        ),
        actual_answer_support=SupportAssessment(
            value=support,
            reason="actual answer support review",
            context_ids=support_ids,
        ),
        reference_claim_assessments=reference_claims,
        actual_claim_assessments=actual_claims,
    )


def _claims_for_support(
    support: SupportLevel, claim: str
) -> list[ClaimEvidenceAssessment]:
    if support not in {
        SupportLevel.FULL,
        SupportLevel.PARTIAL,
        SupportLevel.CONFLICTING,
    }:
        return []
    return [
        ClaimEvidenceAssessment(
            value=support,
            reason=f"{claim} evidence",
            claim=claim,
            context_ids=["ctx-1"],
        )
    ]


def _critic(verdict: AnswerVerdict) -> CriticResult:
    return CriticResult(
        correctness=CorrectnessAssessment(
            value=verdict,
            reason="final judgment",
            matched_facts=["Paris is the capital"]
            if verdict is AnswerVerdict.CORRECT
            else [],
            incorrect_claims=["the answer is incorrect"]
            if verdict is AnswerVerdict.INCORRECT
            else [],
        ),
        reference_quality=ReferenceQualityAssessment(
            value=ReferenceQuality.ADEQUATE,
            reason="the reference is adequate",
        ),
    )


def test_empty_answer_is_deterministic_and_does_not_call_judge() -> None:
    judge = FakeJudge()
    result = asyncio.run(RagEvaluator(judge=judge).evaluate(_case(answer="   ")))

    assert result.correctness.value is AnswerVerdict.INCORRECT
    assert result.attribution is not None
    assert result.attribution.value is AttributionCategory.GENERATION_EMPTY
    assert result.run_logs.usage.calls == 0
    assert not judge.calls
    assert result.run_logs.correctness_result is None
    assert result.run_logs.evidence_result is None
    record = result.run_logs.attribution_run
    assert record is not None
    assert record.deterministic
    assert record.allowed_attributions == [AttributionCategory.GENERATION_EMPTY]

    invalid_failed = result.model_dump(mode="json")
    invalid_failed["evaluation_status"] = "failed"
    invalid_failed["error"] = {
        "stage": "validation",
        "code": "invalid_evaluation_input",
        "message": "invalid",
    }
    with pytest.raises(ValidationError, match="cannot contain final"):
        EvaluationOutcome.model_validate(invalid_failed)


def test_critic_can_recover_false_negative(caplog) -> None:
    judge = FakeJudge(
        _preliminary(AnswerVerdict.INCORRECT),
        _review(support=SupportLevel.FULL),
        _critic(AnswerVerdict.CORRECT),
    )
    caplog.set_level(logging.INFO, logger="nianlun.evaluation")
    result = asyncio.run(RagEvaluator(judge=judge).evaluate(_case()))

    assert result.correctness.value is AnswerVerdict.CORRECT
    assert result.run_logs.critic_run is not None
    assert result.run_logs.critic_run.decision.value == "overturn"
    assert result.run_logs.critic_run.result is None
    assert result.correctness.matched_facts == ["Paris is the capital"]
    assert result.reference_quality is not None
    assert result.reference_quality.value is ReferenceQuality.ADEQUATE
    assert result.run_logs.evidence_result is None
    assert len(judge.calls) == 3
    assert EvidenceModelOutput in judge.calls
    assert EvidenceResult not in judge.calls
    assert result.evidence is not None
    assert (
        result.evidence.actual_answer_support.reason
        == "Derived from 1 claim assessments."
    )
    assert "evaluation.evidence.derived" in caplog.text
    assert "evaluation.critic.routed" in caplog.text
    assert "evaluation.completed" in caplog.text
    assert "Paris is the capital" not in caplog.text


def test_completed_outcome_rejects_duplicate_evidence_and_wrong_call_count() -> None:
    judge = FakeJudge(
        _preliminary(AnswerVerdict.CORRECT),
        _review(),
        _critic(AnswerVerdict.CORRECT),
    )
    result = asyncio.run(RagEvaluator(judge=judge).evaluate(_case()))

    duplicate = result.model_dump(mode="json")
    duplicate["run_logs"]["evidence_result"] = duplicate["evidence"]
    with pytest.raises(ValidationError, match="cannot duplicate evidence"):
        EvaluationOutcome.model_validate(duplicate)

    duplicate_critic = result.model_dump(mode="json")
    duplicate_critic["run_logs"]["critic_run"]["result"] = {
        "correctness": duplicate_critic["correctness"],
        "reference_quality": duplicate_critic["reference_quality"],
    }
    with pytest.raises(ValidationError, match="cannot duplicate critic result"):
        EvaluationOutcome.model_validate(duplicate_critic)

    wrong_calls = result.model_dump(mode="json")
    wrong_calls["run_logs"]["usage"]["calls"] = 2
    with pytest.raises(ValidationError, match="exactly 3 logical stage calls"):
        EvaluationOutcome.model_validate(wrong_calls)


def test_failed_critic_keeps_completed_evidence_result_in_run_logs() -> None:
    judge = FakeJudge(
        _preliminary(AnswerVerdict.CORRECT),
        _review(),
    )

    result = asyncio.run(RagEvaluator(judge=judge).evaluate(_case()))

    assert result.evaluation_status.value == "failed"
    assert result.error is not None
    assert result.error.stage.value == "critic"
    assert result.evidence is None
    assert result.run_logs.evidence_result is not None


def test_failed_correctness_keeps_successful_evidence_in_run_logs() -> None:
    judge = FakeJudge(
        RuntimeError("judge unavailable"),
        _review(),
    )

    result = asyncio.run(RagEvaluator(judge=judge).evaluate(_case()))

    assert result.evaluation_status.value == "failed"
    assert result.error is not None
    assert result.error.stage.value == "correctness"
    assert result.evidence is None
    assert result.run_logs.correctness_result is None
    assert result.run_logs.evidence_result is not None


def test_failed_attribution_keeps_critic_result_in_run_logs() -> None:
    judge = FakeJudge(
        _preliminary(AnswerVerdict.INCORRECT),
        _review(support=SupportLevel.NONE),
        _critic(AnswerVerdict.INCORRECT),
    )

    result = asyncio.run(RagEvaluator(judge=judge).evaluate(_case()))

    assert result.evaluation_status.value == "failed"
    assert result.error is not None
    assert result.error.stage.value == "attribution"
    assert result.run_logs.critic_run is not None
    assert result.run_logs.critic_run.result is not None
    assert result.run_logs.evidence_result is not None


def test_empty_retrieval_error_uses_restricted_model_attribution() -> None:
    judge = FakeJudge(
        _preliminary(AnswerVerdict.INCORRECT),
        _review(
            coverage=RetrievalCoverage.NONE,
            support=SupportLevel.NONE,
            reference_support=SupportLevel.NONE,
        ),
        _critic(AnswerVerdict.INCORRECT),
        AttributionAssessment(
            value=AttributionCategory.UNKNOWN,
            attribution_strength=AttributionStrength.INSUFFICIENT,
            reason="empty retrieval does not prove retrieval caused the error",
        ),
    )
    result = asyncio.run(RagEvaluator(judge=judge).evaluate(_case(contexts=[])))

    assert result.attribution is not None
    assert result.attribution.value is AttributionCategory.UNKNOWN
    record = result.run_logs.attribution_run
    assert record is not None
    assert not record.deterministic
    assert record.allowed_attributions == [
        AttributionCategory.RETRIEVAL_MISSING,
        AttributionCategory.UNKNOWN,
    ]
    assert len(judge.calls) == 4


def test_attribution_must_stay_in_allowed_candidates() -> None:
    judge = FakeJudge(
        _preliminary(AnswerVerdict.INCORRECT),
        _review(coverage=RetrievalCoverage.PARTIAL, support=SupportLevel.NONE),
        _critic(AnswerVerdict.INCORRECT),
        AttributionAssessment(
            value=AttributionCategory.RETRIEVAL_INCOMPLETE,
            attribution_strength=AttributionStrength.PLAUSIBLE,
            reason="missing required evidence",
        ),
    )
    result = asyncio.run(RagEvaluator(judge=judge).evaluate(_case()))

    assert result.attribution is not None
    assert result.attribution.value is AttributionCategory.RETRIEVAL_INCOMPLETE
    assert result.run_logs.usage.calls == 4


def test_context_truncation_limits_attribution_to_unknown() -> None:
    judge = FakeJudge(
        _preliminary(AnswerVerdict.INCORRECT),
        _review(coverage=RetrievalCoverage.UNCERTAIN, support=SupportLevel.NONE),
        _critic(AnswerVerdict.INCORRECT),
        AttributionAssessment(
            value=AttributionCategory.UNKNOWN,
            attribution_strength=AttributionStrength.INSUFFICIENT,
            reason="contexts were truncated",
        ),
    )
    evaluator = RagEvaluator(judge=judge, config=EvaluationConfig(max_context_chars=3))
    result = asyncio.run(evaluator.evaluate(_case()))

    assert result.run_logs.input_stats.contexts_truncated is True
    assert result.run_logs.case_fingerprint == case_fingerprint(
        normalize_contexts(_case())
    )
    assert result.run_logs.prompt_versions.critic_branch
    assert result.run_logs.attribution_run is not None
    assert result.run_logs.attribution_run.allowed_attributions == [
        AttributionCategory.UNKNOWN
    ]


def test_secondary_attributions_must_stay_in_allowed_candidates() -> None:
    judge = FakeJudge(
        _preliminary(AnswerVerdict.INCORRECT),
        _review(coverage=RetrievalCoverage.PARTIAL, support=SupportLevel.NONE),
        _critic(AnswerVerdict.INCORRECT),
        AttributionAssessment(
            value=AttributionCategory.RETRIEVAL_INCOMPLETE,
            secondary_issues=[AttributionCategory.RETRIEVAL_MISSING],
            attribution_strength=AttributionStrength.PLAUSIBLE,
            reason="invalid secondary attribution",
        ),
        AttributionAssessment(
            value=AttributionCategory.RETRIEVAL_INCOMPLETE,
            attribution_strength=AttributionStrength.PLAUSIBLE,
            reason="corrected attribution",
        ),
    )

    result = asyncio.run(RagEvaluator(judge=judge).evaluate(_case()))

    assert result.evaluation_status.value == "completed"
    assert result.error is None
    assert result.run_logs.structured_output.semantic_retry_count == 1


def test_empty_retrieval_rejects_impossible_evidence_result() -> None:
    judge = FakeJudge(
        _preliminary(AnswerVerdict.CORRECT),
        _review(),
        _review(
            coverage=RetrievalCoverage.NONE,
            support=SupportLevel.NONE,
            reference_support=SupportLevel.NONE,
        ),
        _critic(AnswerVerdict.CORRECT),
    )

    result = asyncio.run(RagEvaluator(judge=judge).evaluate(_case(contexts=[])))

    assert result.evaluation_status.value == "completed"
    assert result.error is None
    assert result.run_logs.structured_output.semantic_retry_count == 1


def test_evidence_metric_context_ids_are_semantically_validated() -> None:
    invalid_coverage = RetrievalCoverageAssessment(
        value=RetrievalCoverage.FULL,
        reason="complete evidence",
        context_ids=["missing-context"],
    )
    invalid_review = _review().model_copy(
        update={"retrieval_coverage": invalid_coverage}
    )
    judge = FakeJudge(
        _preliminary(AnswerVerdict.CORRECT),
        invalid_review,
        _review(),
        _critic(AnswerVerdict.CORRECT),
    )

    result = asyncio.run(RagEvaluator(judge=judge).evaluate(_case()))

    assert result.evaluation_status.value == "completed"
    assert result.run_logs.structured_output.semantic_retry_count == 1


def test_semantic_retry_exhaustion_fails_the_stage() -> None:
    judge = FakeJudge(
        _preliminary(AnswerVerdict.CORRECT),
        _review(),
        _review(),
    )
    evaluator = RagEvaluator(
        judge=judge,
        config=EvaluationConfig(max_semantic_retries=1),
    )

    result = asyncio.run(evaluator.evaluate(_case(contexts=[])))

    assert result.evaluation_status.value == "failed"
    assert result.error is not None
    assert result.error.stage.value == "evidence"
    assert result.run_logs.structured_output.semantic_retry_count == 1


def test_generation_incomplete_candidate_depends_on_partial_support() -> None:
    from nianlun.evaluation.stages.attribution.policy import allowed_attributions

    correctness = CorrectnessAssessment(
        value=AnswerVerdict.INCORRECT,
        reason="wrong answer",
    )
    assert AttributionCategory.GENERATION_INCOMPLETE in allowed_attributions(
        correctness, _review(support=SupportLevel.PARTIAL), False
    )
    assert AttributionCategory.GENERATION_INCOMPLETE not in allowed_attributions(
        correctness, _review(support=SupportLevel.FULL), False
    )


def test_unsupported_claim_opens_hallucination_despite_partial_support() -> None:
    from nianlun.evaluation.stages.attribution.policy import allowed_attributions

    base = _review(support=SupportLevel.PARTIAL)
    review = EvidenceResult(
        retrieval_coverage=base.retrieval_coverage,
        retrieval_noise=base.retrieval_noise,
        evidence_consistency=base.evidence_consistency,
        reference_answer_support=base.reference_answer_support,
        actual_answer_support=base.actual_answer_support,
        reference_claim_assessments=base.reference_claim_assessments,
        actual_claim_assessments=[
            ClaimEvidenceAssessment(
                value=SupportLevel.FULL,
                reason="supported fact",
                claim="Paris is the capital",
                context_ids=["ctx-1"],
            ),
            ClaimEvidenceAssessment(
                value=SupportLevel.NONE,
                reason="no evidence for this assertion",
                claim="Berlin is the capital",
            ),
        ],
    )
    correctness = CorrectnessAssessment(
        value=AnswerVerdict.PARTIALLY_CORRECT,
        reason="one supported fact and one unsupported assertion",
        matched_facts=["Paris is the capital"],
        incorrect_claims=["Berlin is the capital"],
    )

    allowed = allowed_attributions(correctness, review, False)

    assert AttributionCategory.HALLUCINATION in allowed
    assert AttributionCategory.GENERATION_INCOMPLETE in allowed


def test_partial_coverage_still_allows_generation_incomplete_from_missing_facts() -> (
    None
):
    from nianlun.evaluation.stages.attribution.policy import allowed_attributions

    correctness = CorrectnessAssessment(
        value=AnswerVerdict.PARTIALLY_CORRECT,
        reason="the answer omits retrieved fact A",
        matched_facts=["retrieved fact B"],
        missing_facts=["retrieved fact A"],
    )

    allowed = allowed_attributions(
        correctness,
        _review(support=SupportLevel.FULL, coverage=RetrievalCoverage.PARTIAL),
        False,
    )

    assert AttributionCategory.RETRIEVAL_INCOMPLETE in allowed
    assert AttributionCategory.HALLUCINATION in allowed
    assert AttributionCategory.GENERATION_INCOMPLETE in allowed
    assert AttributionCategory.UNKNOWN in allowed


def test_missing_facts_open_generation_incomplete_despite_full_support() -> None:
    from nianlun.evaluation.stages.attribution.policy import allowed_attributions

    correctness = CorrectnessAssessment(
        value=AnswerVerdict.PARTIALLY_CORRECT,
        reason="supported statements but a required conclusion is missing",
        matched_facts=["Paris is the capital"],
        missing_facts=["the required conclusion"],
    )

    allowed = allowed_attributions(
        correctness, _review(support=SupportLevel.FULL), False
    )

    assert AttributionCategory.GENERATION_INCOMPLETE in allowed
    assert AttributionCategory.HALLUCINATION not in allowed


def test_confidence_is_not_an_evaluator_configuration() -> None:
    with pytest.raises(TypeError, match="critic_low_confidence_threshold"):
        EvaluationConfig(critic_low_confidence_threshold=0.7)  # type: ignore[call-arg]
