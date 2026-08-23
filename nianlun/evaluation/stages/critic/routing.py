"""Deterministic critic branch selection."""

from dataclasses import dataclass

from nianlun.evaluation.contracts.enums import (
    AnswerVerdict,
    CriticPromptId,
    EvidenceConsistency,
    ReferenceQuality,
    RetrievalCoverage,
    RoutingFlag,
    SupportLevel,
)
from nianlun.evaluation.stages.correctness.schema import CorrectnessResult
from nianlun.evaluation.stages.evidence.schema import EvidenceResult

BRANCH_PROMPT_VERSION = "2026-08-19.v4"


@dataclass(frozen=True, slots=True)
class CriticRoute:
    prompt_id: CriticPromptId
    prompt_version: str
    routing_flags: list[RoutingFlag]


def route_critic(
    correctness: CorrectnessResult,
    evidence: EvidenceResult,
    *,
    contexts_truncated: bool,
) -> CriticRoute:
    flags: list[RoutingFlag] = []
    if correctness.reference_quality.value is not ReferenceQuality.ADEQUATE:
        flags.append(RoutingFlag.REFERENCE_QUALITY_ISSUE)
    tension = (
        correctness.correctness.value is AnswerVerdict.CORRECT
        and evidence.actual_answer_support.value
        in {SupportLevel.NONE, SupportLevel.PARTIAL, SupportLevel.CONFLICTING}
    ) or (
        correctness.correctness.value is not AnswerVerdict.CORRECT
        and evidence.actual_answer_support.value is SupportLevel.FULL
        and evidence.evidence_consistency.value is EvidenceConsistency.CONSISTENT
    )
    if tension:
        flags.append(RoutingFlag.VERDICT_EVIDENCE_TENSION)
    if evidence.evidence_consistency.value is EvidenceConsistency.CONFLICTING:
        flags.append(RoutingFlag.EVIDENCE_CONFLICT)
    if contexts_truncated:
        flags.append(RoutingFlag.CONTEXTS_TRUNCATED)

    if (
        correctness.reference_quality.value is not ReferenceQuality.ADEQUATE
        or (
            evidence.retrieval_coverage.value is RetrievalCoverage.FULL
            and evidence.reference_answer_support.value is SupportLevel.NONE
        )
        or evidence.reference_answer_support.value is SupportLevel.CONFLICTING
    ):
        prompt_id = CriticPromptId.REFERENCE_CHALLENGE
    elif (
        correctness.correctness.value is not AnswerVerdict.CORRECT
        and evidence.actual_answer_support.value is SupportLevel.FULL
        and evidence.evidence_consistency.value is EvidenceConsistency.CONSISTENT
    ):
        prompt_id = CriticPromptId.FALSE_NEGATIVE_RECOVERY
    elif (
        correctness.correctness.value is AnswerVerdict.CORRECT
        and evidence.actual_answer_support.value
        in {SupportLevel.NONE, SupportLevel.PARTIAL, SupportLevel.CONFLICTING}
    ):
        prompt_id = CriticPromptId.FALSE_POSITIVE_CORRECTION
    elif evidence.evidence_consistency.value in {
        EvidenceConsistency.CONFLICTING,
        EvidenceConsistency.UNCERTAIN,
    }:
        prompt_id = CriticPromptId.EVIDENCE_CONFLICT_RESOLUTION
    elif correctness.correctness.value in {
        AnswerVerdict.PARTIALLY_CORRECT,
        AnswerVerdict.INCORRECT,
    } and evidence.actual_answer_support.value in {
        SupportLevel.PARTIAL,
        SupportLevel.CONFLICTING,
    }:
        prompt_id = CriticPromptId.SEVERITY_BOUNDARY_CORRECTION
    else:
        prompt_id = CriticPromptId.GENERAL
    return CriticRoute(prompt_id, BRANCH_PROMPT_VERSION, flags)
