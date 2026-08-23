"""Top-level orchestration for the staged RAG evaluation pipeline."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import asdict, dataclass
from typing import Any

from nianlun.evaluation.contracts.case import (
    EvaluationCase,
    case_fingerprint,
    normalize_contexts,
)
from nianlun.evaluation.orchestration.fingerprint import evaluator_fingerprint
from nianlun.evaluation.contracts.enums import (
    AnswerVerdict,
    AttributionCategory,
    AttributionStrength,
    EvaluationStage,
    EvaluationStatus,
)
from nianlun.evaluation.contracts.outcome import EvaluationOutcome
from nianlun.evaluation.contracts.run_logs import (
    EvaluationError,
    EvaluationRunLogs,
    InputStats,
    JudgeMetadata,
    PromptVersions,
)
from nianlun.evaluation.stages.attribution.schema import (
    AttributionAssessment,
    AttributionRunRecord,
)
from nianlun.evaluation.stages.correctness.schema import (
    CorrectnessAssessment,
    CorrectnessResult,
    ReferenceQualityAssessment,
)
from nianlun.evaluation.stages.critic.schema import CriticResult, CriticRunRecord
from nianlun.evaluation.stages.evidence.schema import (
    EvidenceModelOutput,
    EvidenceResult,
)
from nianlun.evaluation.judge.runtime import (
    EvaluationRuntime,
    StructuredGeneration,
    StructuredGenerationError,
    UsageAccumulator,
)
from nianlun.evaluation.stages.attribution.attribution import Attribution
from nianlun.evaluation.stages.attribution.policy import allowed_attributions
from nianlun.evaluation.stages.attribution.prompt import (
    PROMPT_VERSION as ATTRIBUTION_PROMPT_VERSION,
)
from nianlun.evaluation.stages.common import SEMANTIC_CORRECTION_PROMPT_VERSION
from nianlun.evaluation.stages.correctness.correctness import Correctness
from nianlun.evaluation.stages.correctness.prompt import (
    PROMPT_VERSION as CORRECTNESS_PROMPT_VERSION,
)
from nianlun.evaluation.stages.critic.critic import Critic
from nianlun.evaluation.stages.critic.prompt import COMMON_PROMPT_VERSION
from nianlun.evaluation.stages.critic.routing import BRANCH_PROMPT_VERSION
from nianlun.evaluation.stages.evidence.evidence import Evidence
from nianlun.evaluation.stages.evidence.prompt import (
    PROMPT_VERSION as EVIDENCE_PROMPT_VERSION,
)
from nianlun.models.llm import LLMMetadata, StructuredLLM

EVALUATION_VERSION = "2.6"
ROUTING_VERSION = "2026-08-21.v2"

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class EvaluationConfig:
    """Behavior-bearing pipeline configuration included in the fingerprint."""

    max_context_chars: int = 12_000
    max_context_items: int = 50
    critic_policy: str = "always"  # Placeholder for a calibrated fast path.
    evaluation_version: str = EVALUATION_VERSION
    routing_version: str = ROUTING_VERSION
    temperature: float = 0.0
    max_semantic_retries: int = 2

    def __post_init__(self) -> None:
        if self.max_context_chars < 1 or self.max_context_items < 1:
            raise ValueError("context limits must be positive")
        if self.critic_policy != "always":
            raise ValueError(
                f"evaluation {EVALUATION_VERSION} supports only critic_policy='always'"
            )
        if self.max_semantic_retries < 0:
            raise ValueError("max_semantic_retries cannot be negative")


class RagEvaluator:
    """Evaluate the common four-field RAG case through four explicit stages."""

    def __init__(
        self,
        *,
        judge: StructuredLLM,
        config: EvaluationConfig | None = None,
        judge_metadata: JudgeMetadata | None = None,
    ) -> None:
        self.judge = judge
        self.config = config or EvaluationConfig()
        self.judge_metadata = judge_metadata or _resolve_judge_metadata(
            judge,
            self.config.temperature,
        )
        runtime = EvaluationRuntime(
            judge,
            max_semantic_retries=self.config.max_semantic_retries,
        )
        self.correctness = Correctness(runtime)
        self.evidence = Evidence(runtime)
        self.critic = Critic(runtime)
        self.attribution = Attribution(runtime)
        self._fingerprint = evaluator_fingerprint(
            {
                "config": asdict(self.config),
                "prompt_versions": _prompt_versions().model_dump(mode="json"),
                "schemas": [
                    EvaluationCase.model_json_schema(),
                    EvaluationOutcome.model_json_schema(),
                    CorrectnessAssessment.model_json_schema(),
                    ReferenceQualityAssessment.model_json_schema(),
                    CorrectnessResult.model_json_schema(),
                    EvidenceModelOutput.model_json_schema(),
                    EvidenceResult.model_json_schema(),
                    CriticResult.model_json_schema(),
                    AttributionAssessment.model_json_schema(),
                ],
                "judge": self.judge_metadata.model_dump(mode="json"),
                "judge_config": getattr(judge, "fingerprint_config", {}),
            }
        )

    @property
    def evaluator_fingerprint(self) -> str:
        return self._fingerprint

    async def evaluate(self, case: EvaluationCase) -> EvaluationOutcome:
        """Evaluate one valid case; model-stage failures become failed outcomes."""
        started = time.perf_counter()
        input_count = len(case.retrieval_contexts)
        canonical_fingerprint = case_fingerprint(case)
        logger.info(
            "evaluation.started case=%s retrieval_contexts=%d",
            canonical_fingerprint,
            input_count,
        )
        if case.is_empty_answer:
            return self._empty_answer_result(
                canonical_fingerprint,
                input_count,
                started,
            )

        usage = UsageAccumulator()
        try:
            normalized_case = normalize_contexts(case)
            canonical_fingerprint = case_fingerprint(normalized_case)
            normalized_case, contexts_truncated = _truncate_contexts(
                normalized_case,
                self.config,
            )
        except ValueError as exc:
            return self._failed_result(
                input_count=input_count,
                contexts_truncated=False,
                canonical_fingerprint=canonical_fingerprint,
                started=started,
                stage=EvaluationStage.VALIDATION,
                exc=exc,
            )
        if contexts_truncated:
            logger.info(
                "evaluation.contexts_truncated case=%s retained_contexts=%d",
                canonical_fingerprint,
                len(normalized_case.retrieval_contexts),
            )

        usage.calls += 2
        correctness_generation, evidence_generation = await asyncio.gather(
            self.correctness.evaluate(normalized_case),
            self.evidence.evaluate(normalized_case),
            return_exceptions=True,
        )
        _add_generation_usage(usage, correctness_generation)
        _add_generation_usage(usage, evidence_generation)
        if isinstance(correctness_generation, BaseException):
            if not isinstance(correctness_generation, Exception):
                raise correctness_generation
            return self._failed_result(
                input_count=input_count,
                contexts_truncated=contexts_truncated,
                canonical_fingerprint=canonical_fingerprint,
                started=started,
                stage=EvaluationStage.CORRECTNESS,
                exc=correctness_generation,
                evidence_result=(
                    evidence_generation.output
                    if isinstance(evidence_generation, StructuredGeneration)
                    else None
                ),
                usage=usage,
            )
        correctness_result = correctness_generation.output
        if isinstance(evidence_generation, BaseException):
            if not isinstance(evidence_generation, Exception):
                raise evidence_generation
            return self._failed_result(
                input_count=input_count,
                contexts_truncated=contexts_truncated,
                canonical_fingerprint=canonical_fingerprint,
                started=started,
                stage=EvaluationStage.EVIDENCE,
                exc=evidence_generation,
                correctness_result=correctness_result,
                usage=usage,
            )
        evidence_result = evidence_generation.output

        route = self.critic.route(
            correctness_result,
            evidence_result,
            contexts_truncated=contexts_truncated,
        )
        logger.info(
            "evaluation.critic.routed case=%s prompt_id=%s flags=%s",
            canonical_fingerprint,
            route.prompt_id,
            ",".join(flag.value for flag in route.routing_flags) or "none",
        )
        try:
            usage.calls += 1
            critic_generation = await self.critic.evaluate(
                normalized_case,
                correctness_result,
                evidence_result,
                route,
            )
            usage.add(critic_generation)
            critic_result = critic_generation.output
            critic_run = self.critic.run_record(
                correctness_result,
                critic_result,
                route,
            )
        except Exception as exc:
            if isinstance(exc, StructuredGenerationError):
                usage.add(exc)
            return self._failed_result(
                input_count=input_count,
                contexts_truncated=contexts_truncated,
                canonical_fingerprint=canonical_fingerprint,
                started=started,
                stage=EvaluationStage.CRITIC,
                exc=exc,
                correctness_result=correctness_result,
                evidence_result=evidence_result,
                usage=usage,
            )

        attribution_assessment: AttributionAssessment | None = None
        attribution_run: AttributionRunRecord | None = None
        if critic_result.correctness.value in {
            AnswerVerdict.PARTIALLY_CORRECT,
            AnswerVerdict.INCORRECT,
        }:
            # Empty retrieval narrows the candidates to retrieval_missing/unknown
            # inside allowed_attributions; the root cause stays a model judgment.
            allowed = allowed_attributions(
                critic_result.correctness,
                evidence_result,
                contexts_truncated,
            )
            logger.info(
                "evaluation.attribution.routed case=%s allowed=%s",
                canonical_fingerprint,
                ",".join(item.value for item in allowed),
            )
            try:
                usage.calls += 1
                attribution_generation = await self.attribution.evaluate(
                    normalized_case,
                    evidence_result,
                    critic_result,
                    allowed,
                )
                usage.add(attribution_generation)
                attribution_assessment = attribution_generation.output
                attribution_run = AttributionRunRecord(
                    allowed_attributions=allowed,
                )
            except Exception as exc:
                if isinstance(exc, StructuredGenerationError):
                    usage.add(exc)
                return self._failed_result(
                    input_count=input_count,
                    contexts_truncated=contexts_truncated,
                    canonical_fingerprint=canonical_fingerprint,
                    started=started,
                    stage=EvaluationStage.ATTRIBUTION,
                    exc=exc,
                    correctness_result=correctness_result,
                    evidence_result=evidence_result,
                    critic_run=critic_run,
                    usage=usage,
                )

        outcome = EvaluationOutcome(
            evaluation_status=EvaluationStatus.COMPLETED,
            correctness=critic_result.correctness,
            reference_quality=critic_result.reference_quality,
            evidence=evidence_result,
            attribution=attribution_assessment,
            run_logs=self._logs(
                canonical_fingerprint=canonical_fingerprint,
                input_count=input_count,
                contexts_truncated=contexts_truncated,
                correctness_result=correctness_result,
                evidence_result=None,
                critic_run=critic_run.model_copy(update={"result": None}),
                attribution_run=attribution_run,
                usage=usage,
                started=started,
            ),
            error=None,
        )
        logger.info(
            "evaluation.completed case=%s verdict=%s attribution=%s calls=%d duration_ms=%d",
            canonical_fingerprint,
            outcome.correctness.value,
            outcome.attribution.value if outcome.attribution is not None else "none",
            outcome.run_logs.usage.calls,
            outcome.run_logs.duration_ms,
        )
        return outcome

    def _empty_answer_result(
        self,
        canonical_fingerprint: str,
        input_count: int,
        started: float,
    ) -> EvaluationOutcome:
        outcome = EvaluationOutcome(
            evaluation_status=EvaluationStatus.COMPLETED,
            correctness=CorrectnessAssessment(
                value=AnswerVerdict.INCORRECT,
                reason="actual_answer is empty",
            ),
            reference_quality=None,
            evidence=None,
            attribution=AttributionAssessment(
                value=AttributionCategory.GENERATION_EMPTY,
                reason="actual_answer is empty",
                attribution_strength=AttributionStrength.STRONG,
            ),
            run_logs=self._logs(
                canonical_fingerprint=canonical_fingerprint,
                input_count=input_count,
                contexts_truncated=False,
                correctness_result=None,
                evidence_result=None,
                critic_run=None,
                attribution_run=AttributionRunRecord(
                    allowed_attributions=[AttributionCategory.GENERATION_EMPTY],
                    deterministic=True,
                ),
                usage=UsageAccumulator(),
                started=started,
            ),
            error=None,
        )
        logger.info(
            "evaluation.completed case=%s verdict=%s attribution=%s calls=0 duration_ms=%d",
            canonical_fingerprint,
            outcome.correctness.value,
            outcome.attribution.value,
            outcome.run_logs.duration_ms,
        )
        return outcome

    def _failed_result(
        self,
        *,
        input_count: int,
        contexts_truncated: bool,
        canonical_fingerprint: str,
        started: float,
        stage: EvaluationStage,
        exc: Exception,
        correctness_result: CorrectnessResult | None = None,
        evidence_result: EvidenceResult | None = None,
        critic_run: CriticRunRecord | None = None,
        usage: UsageAccumulator | None = None,
    ) -> EvaluationOutcome:
        error = _evaluation_error(stage, exc)
        outcome = EvaluationOutcome(
            evaluation_status=EvaluationStatus.FAILED,
            correctness=None,
            reference_quality=None,
            evidence=None,
            attribution=None,
            run_logs=self._logs(
                canonical_fingerprint=canonical_fingerprint,
                input_count=input_count,
                contexts_truncated=contexts_truncated,
                correctness_result=correctness_result,
                evidence_result=evidence_result,
                critic_run=critic_run,
                attribution_run=None,
                usage=usage or UsageAccumulator(),
                started=started,
            ),
            error=error,
        )
        logger.warning(
            "evaluation.failed case=%s stage=%s code=%s error_type=%s calls=%d duration_ms=%d",
            canonical_fingerprint,
            stage.value,
            error.code,
            type(exc).__name__,
            outcome.run_logs.usage.calls,
            outcome.run_logs.duration_ms,
        )
        return outcome

    def _logs(
        self,
        *,
        canonical_fingerprint: str,
        input_count: int,
        contexts_truncated: bool,
        correctness_result: CorrectnessResult | None,
        evidence_result: EvidenceResult | None,
        critic_run: CriticRunRecord | None,
        attribution_run: AttributionRunRecord | None,
        usage: UsageAccumulator,
        started: float,
    ) -> EvaluationRunLogs:
        return EvaluationRunLogs(
            case_fingerprint=canonical_fingerprint,
            evaluator_fingerprint=self._fingerprint,
            evaluation_version=self.config.evaluation_version,
            routing_version=self.config.routing_version,
            prompt_versions=_prompt_versions(),
            input_stats=InputStats(
                retrieval_context_count=input_count,
                contexts_truncated=contexts_truncated,
            ),
            correctness_result=correctness_result,
            evidence_result=evidence_result,
            critic_run=critic_run,
            attribution_run=attribution_run,
            judge=self.judge_metadata,
            usage=usage.usage(),
            structured_output=usage.structured_output(),
            duration_ms=int((time.perf_counter() - started) * 1000),
        )


def _truncate_contexts(
    case: EvaluationCase,
    config: EvaluationConfig,
) -> tuple[EvaluationCase, bool]:
    kept = []
    remaining = config.max_context_chars
    truncated = False
    for context in case.retrieval_contexts[: config.max_context_items]:
        if remaining <= 0:
            truncated = True
            break
        text = context.text[:remaining]
        if len(text) < len(context.text):
            truncated = True
        remaining -= len(text)
        kept.append(context.model_copy(update={"text": text}))
    if len(case.retrieval_contexts) > config.max_context_items:
        truncated = True
    return case.model_copy(update={"retrieval_contexts": kept}), truncated


def _add_generation_usage(
    usage: UsageAccumulator,
    generation: StructuredGeneration[Any] | BaseException,
) -> None:
    if isinstance(generation, (StructuredGeneration, StructuredGenerationError)):
        usage.add(generation)


def _resolve_judge_metadata(
    judge: StructuredLLM,
    default_temperature: float,
) -> JudgeMetadata:
    metadata = getattr(judge, "metadata", None)
    if isinstance(metadata, JudgeMetadata):
        return metadata
    if isinstance(metadata, LLMMetadata):
        return JudgeMetadata(
            provider=metadata.provider,
            model=metadata.model,
            temperature=metadata.temperature,
        )
    return JudgeMetadata(
        provider="custom",
        model="custom",
        temperature=default_temperature,
    )


def _evaluation_error(stage: EvaluationStage, exc: Exception) -> EvaluationError:
    cause = exc.cause if isinstance(exc, StructuredGenerationError) else exc
    code = getattr(cause, "code", None)
    public_message = getattr(cause, "public_message", None)
    retryable = getattr(cause, "retryable", False)
    if isinstance(code, str) and isinstance(public_message, str):
        return EvaluationError(
            stage=stage,
            code=code,
            message=public_message,
            retryable=bool(retryable),
        )
    if isinstance(cause, ValueError):
        if stage is EvaluationStage.VALIDATION:
            return EvaluationError(
                stage=stage,
                code="invalid_evaluation_input",
                message="evaluation input failed validation",
            )
        return EvaluationError(
            stage=stage,
            code="invalid_stage_output",
            message="judge output violated evaluation constraints",
        )
    return EvaluationError(
        stage=stage,
        code="evaluation_stage_failed",
        message="evaluation stage failed",
    )


def _prompt_versions() -> PromptVersions:
    return PromptVersions(
        correctness=CORRECTNESS_PROMPT_VERSION,
        evidence=EVIDENCE_PROMPT_VERSION,
        critic_common=COMMON_PROMPT_VERSION,
        critic_branch=BRANCH_PROMPT_VERSION,
        attribution=ATTRIBUTION_PROMPT_VERSION,
        semantic_correction=SEMANTIC_CORRECTION_PROMPT_VERSION,
    )
