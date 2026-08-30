"""Aggregate evaluation results without claiming external-label correctness."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from enum import StrEnum
from typing import TypeVar

from nianlun.evaluation.contracts.enums import (
    AnswerVerdict,
    AttributionCategory,
    AttributionStrength,
    CriticDecision,
    CriticPromptId,
    EvaluationStatus,
    EvidenceConsistency,
    HallucinationEvidence,
    ReferenceQuality,
    RetrievalCoverage,
    RetrievalNoise,
    RoutingFlag,
    SupportLevel,
)
from nianlun.evaluation.contracts.outcome import EvaluationOutcome
from nianlun.evaluation.contracts.run_logs import EvaluationUsage, StructuredOutputStats
from nianlun.evaluation.contracts.summary import EvaluationSummary

EnumT = TypeVar("EnumT", bound=StrEnum)


def summarize_results(results: Iterable[EvaluationOutcome]) -> EvaluationSummary:
    """Summarize one evaluator version; external-label metrics are intentionally absent."""
    items = list(results)
    fingerprints = {item.run_logs.evaluator_fingerprint for item in items}
    if len(fingerprints) > 1:
        raise ValueError("cannot summarize mixed evaluator fingerprints")

    completed = [
        item for item in items if item.evaluation_status is EvaluationStatus.COMPLETED
    ]
    failed_cases = len(items) - len(completed)
    correctness_assessments = [
        item.correctness for item in completed if item.correctness is not None
    ]
    verdicts = Counter(item.value for item in correctness_assessments)
    references = Counter(
        item.reference_quality.value
        for item in completed
        if item.reference_quality is not None
    )
    evidence_results = [
        item.evidence for item in completed if item.evidence is not None
    ]
    critics = [
        item.run_logs.critic_run
        for item in completed
        if item.run_logs.critic_run is not None
    ]
    attributions = [
        item.attribution for item in completed if item.attribution is not None
    ]

    coverage = Counter(item.retrieval_coverage.value for item in evidence_results)
    noise = Counter(item.retrieval_noise.value for item in evidence_results)
    consistency = Counter(item.evidence_consistency.value for item in evidence_results)
    reference_support = Counter(
        item.reference_answer_support.value for item in evidence_results
    )
    actual_support = Counter(
        item.actual_answer_support.value for item in evidence_results
    )
    primary = Counter(item.value for item in attributions)
    secondary = Counter(
        issue for item in attributions for issue in item.secondary_issues
    )
    attribution_strength = Counter(item.attribution_strength for item in attributions)
    prompt_ids = Counter(item.critic_prompt_id for item in critics)
    decisions = Counter(item.decision for item in critics)
    flags = Counter(flag for item in critics for flag in item.routing_flags)
    hallucination_evidence = Counter(
        claim.evidence
        for assessment in attributions
        for claim in assessment.hallucinated_claims
    )

    preliminary_noncorrect = [
        item
        for item in completed
        if item.run_logs.correctness_result is not None
        and item.run_logs.critic_run is not None
        and item.run_logs.correctness_result.correctness.value != AnswerVerdict.CORRECT
    ]
    preliminary_correct = [
        item
        for item in completed
        if item.run_logs.correctness_result is not None
        and item.run_logs.critic_run is not None
        and item.run_logs.correctness_result.correctness.value == AnswerVerdict.CORRECT
    ]
    recovered_false_negatives = sum(
        item.correctness is not None and item.correctness.value == AnswerVerdict.CORRECT
        for item in preliminary_noncorrect
    )
    corrected_false_positives = sum(
        item.correctness is not None and item.correctness.value != AnswerVerdict.CORRECT
        for item in preliminary_correct
    )
    usage = _sum_usage(items)
    stats = _sum_structured_output(items)
    completed_count = len(completed)
    decidable_count = completed_count - verdicts[AnswerVerdict.UNCERTAIN]

    return EvaluationSummary(
        evaluator_fingerprint=next(iter(fingerprints), None),
        total_cases=len(items),
        completed_cases=completed_count,
        failed_cases=failed_cases,
        evidence_cases=len(evidence_results),
        critic_cases=len(critics),
        attributed_cases=len(attributions),
        verdict_counts=_enum_counts(AnswerVerdict, verdicts),
        reference_quality_counts=_enum_counts(ReferenceQuality, references),
        retrieval_coverage_counts=_enum_counts(RetrievalCoverage, coverage),
        retrieval_noise_counts=_enum_counts(RetrievalNoise, noise),
        evidence_consistency_counts=_enum_counts(EvidenceConsistency, consistency),
        reference_answer_support_counts=_enum_counts(SupportLevel, reference_support),
        actual_answer_support_counts=_enum_counts(SupportLevel, actual_support),
        primary_attribution_counts=_enum_counts(AttributionCategory, primary),
        secondary_issue_counts=_enum_counts(AttributionCategory, secondary),
        attribution_strength_counts=_enum_counts(
            AttributionStrength, attribution_strength
        ),
        hallucination_evidence_counts=_enum_counts(
            HallucinationEvidence, hallucination_evidence
        ),
        critic_prompt_counts=_enum_counts(CriticPromptId, prompt_ids),
        critic_decision_counts=_enum_counts(CriticDecision, decisions),
        routing_flag_counts=_enum_counts(RoutingFlag, flags),
        correct_verdict_rate=_rate(verdicts[AnswerVerdict.CORRECT], completed_count),
        decidable_correct_verdict_rate=_rate(
            verdicts[AnswerVerdict.CORRECT], decidable_count
        ),
        partial_correctness_rate=_rate(
            verdicts[AnswerVerdict.PARTIALLY_CORRECT], completed_count
        ),
        incorrect_rate=_rate(verdicts[AnswerVerdict.INCORRECT], completed_count),
        uncertain_rate=_rate(verdicts[AnswerVerdict.UNCERTAIN], completed_count),
        evaluation_failure_rate=_rate(failed_cases, len(items)),
        critic_invocation_rate=_rate(len(critics), completed_count),
        preliminary_overturn_rate=_rate(
            decisions[CriticDecision.OVERTURN], len(critics)
        ),
        false_negative_recovery_rate=_rate(
            recovered_false_negatives, len(preliminary_noncorrect)
        ),
        false_positive_correction_rate=_rate(
            corrected_false_positives, len(preliminary_correct)
        ),
        verdict_evidence_tension_rate=_rate(
            flags[RoutingFlag.VERDICT_EVIDENCE_TENSION], len(critics)
        ),
        usage=usage,
        structured_output=stats,
        duration_ms_total=sum(item.run_logs.duration_ms for item in items),
    )


def _enum_counts(enum_type: type[EnumT], counts: Counter[EnumT]) -> dict[EnumT, int]:
    return {item: counts[item] for item in enum_type}


def _rate(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _sum_usage(results: list[EvaluationOutcome]) -> EvaluationUsage:
    return EvaluationUsage(
        calls=sum(item.run_logs.usage.calls for item in results),
        model_attempts=sum(item.run_logs.usage.model_attempts for item in results),
        invoke_retry_count=sum(
            item.run_logs.usage.invoke_retry_count for item in results
        ),
        input_tokens=sum(item.run_logs.usage.input_tokens for item in results),
        output_tokens=sum(item.run_logs.usage.output_tokens for item in results),
    )


def _sum_structured_output(results: list[EvaluationOutcome]) -> StructuredOutputStats:
    return StructuredOutputStats(
        strict_parse_failures=sum(
            item.run_logs.structured_output.strict_parse_failures for item in results
        ),
        json_repair_attempt_count=sum(
            item.run_logs.structured_output.json_repair_attempt_count
            for item in results
        ),
        json_repair_success_count=sum(
            item.run_logs.structured_output.json_repair_success_count
            for item in results
        ),
        schema_retry_count=sum(
            item.run_logs.structured_output.schema_retry_count for item in results
        ),
        semantic_retry_count=sum(
            item.run_logs.structured_output.semantic_retry_count for item in results
        ),
    )
