"""Critic stage implementation."""

from nianlun.evaluation.contracts.case import EvaluationCase
from nianlun.evaluation.contracts.enums import AnswerVerdict, CriticDecision
from nianlun.evaluation.judge.runtime import EvaluationRuntime, StructuredGeneration
from nianlun.evaluation.stages.correctness.schema import CorrectnessResult
from nianlun.evaluation.stages.critic.schema import CriticResult, CriticRunRecord
from nianlun.evaluation.stages.evidence.schema import EvidenceResult
from nianlun.evaluation.stages.critic.prompt import build_prompt
from nianlun.evaluation.stages.critic.routing import CriticRoute, route_critic


class Critic:
    def __init__(self, runtime: EvaluationRuntime) -> None:
        self.runtime = runtime

    def route(
        self,
        correctness: CorrectnessResult,
        evidence: EvidenceResult,
        *,
        contexts_truncated: bool,
    ) -> CriticRoute:
        return route_critic(
            correctness,
            evidence,
            contexts_truncated=contexts_truncated,
        )

    async def evaluate(
        self,
        case: EvaluationCase,
        correctness: CorrectnessResult,
        evidence: EvidenceResult,
        route: CriticRoute,
    ) -> StructuredGeneration[CriticResult]:
        return await self.runtime.generate_structured(
            build_prompt(case, correctness, evidence, route),
            CriticResult,
        )

    def run_record(
        self,
        correctness: CorrectnessResult,
        result: CriticResult,
        route: CriticRoute,
    ) -> CriticRunRecord:
        return CriticRunRecord(
            critic_prompt_id=route.prompt_id,
            critic_prompt_version=route.prompt_version,
            routing_flags=route.routing_flags,
            decision=_decision(correctness, result),
            overruled_correctness_result=(
                result.correctness.value is not correctness.correctness.value
            ),
            result=result,
        )


def _decision(
    correctness: CorrectnessResult,
    result: CriticResult,
) -> CriticDecision:
    if result.correctness.value is AnswerVerdict.UNCERTAIN:
        return CriticDecision.UNCERTAIN
    if result.correctness.value is correctness.correctness.value:
        return CriticDecision.CONFIRM
    return CriticDecision.OVERTURN
