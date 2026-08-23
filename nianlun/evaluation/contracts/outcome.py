"""Public aggregate outcome for one evaluated case."""

from __future__ import annotations

from pydantic import model_validator

from nianlun.evaluation.contracts.base import EvaluationSchema
from nianlun.evaluation.contracts.enums import (
    AnswerVerdict,
    AttributionCategory,
    AttributionStrength,
    CriticDecision,
    EvaluationStatus,
)
from nianlun.evaluation.contracts.run_logs import EvaluationError, EvaluationRunLogs
from nianlun.evaluation.stages.attribution.schema import AttributionAssessment
from nianlun.evaluation.stages.correctness.schema import (
    CorrectnessAssessment,
    ReferenceQualityAssessment,
)
from nianlun.evaluation.stages.evidence.schema import EvidenceResult


class EvaluationOutcome(EvaluationSchema):
    evaluation_name: str = "rag_evaluation"
    evaluation_status: EvaluationStatus
    correctness: CorrectnessAssessment | None
    reference_quality: ReferenceQualityAssessment | None
    evidence: EvidenceResult | None
    attribution: AttributionAssessment | None
    run_logs: EvaluationRunLogs
    error: EvaluationError | None

    @model_validator(mode="after")
    def validate_status_contract(self) -> "EvaluationOutcome":
        if self.evaluation_status is EvaluationStatus.FAILED:
            if self.error is None:
                raise ValueError("failed outcome requires error")
            if any(
                value is not None
                for value in (
                    self.correctness,
                    self.reference_quality,
                    self.evidence,
                    self.attribution,
                )
            ):
                raise ValueError("failed outcome cannot contain final assessments")
            return self
        if self.error is not None or self.correctness is None:
            raise ValueError("completed outcome requires correctness and no error")
        if not self.run_logs.usage.calls:
            self._validate_deterministic_empty_answer()
            return self
        needs_attribution = self.correctness.value in {
            AnswerVerdict.PARTIALLY_CORRECT,
            AnswerVerdict.INCORRECT,
        }
        if needs_attribution != (self.attribution is not None):
            raise ValueError("attribution must match final correctness")
        if self.reference_quality is None or self.evidence is None:
            raise ValueError(
                "model-evaluated completed answer requires reference_quality and evidence"
            )
        if self.run_logs.correctness_result is None or self.run_logs.critic_run is None:
            raise ValueError("model-evaluated completed answer requires stage records")
        if self.run_logs.evidence_result is not None:
            raise ValueError("completed outcome cannot duplicate evidence in run_logs")
        critic_run = self.run_logs.critic_run
        if critic_run.result is not None:
            raise ValueError(
                "completed outcome cannot duplicate critic result in run_logs"
            )
        attribution_run = self.run_logs.attribution_run
        expected_calls = 3
        if needs_attribution:
            if attribution_run is None or self.attribution is None:
                raise ValueError(
                    "incorrect answer requires attribution routing metadata"
                )
            if self.attribution.value not in attribution_run.allowed_attributions:
                raise ValueError(
                    "final attribution must be allowed by routing metadata"
                )
            if attribution_run.deterministic:
                raise ValueError(
                    "model-evaluated outcome cannot use deterministic attribution"
                )
            expected_calls += 1
        elif attribution_run is not None:
            raise ValueError(
                "correct or uncertain answer cannot contain attribution routing"
            )
        if self.run_logs.usage.calls != expected_calls:
            raise ValueError(
                f"completed outcome requires exactly {expected_calls} logical stage calls"
            )
        preliminary_value = self.run_logs.correctness_result.correctness.value
        expected_overruled = self.correctness.value is not preliminary_value
        if critic_run.overruled_correctness_result != expected_overruled:
            raise ValueError("critic overturn metadata must match final correctness")
        if self.correctness.value is AnswerVerdict.UNCERTAIN:
            expected_decision = CriticDecision.UNCERTAIN
        elif expected_overruled:
            expected_decision = CriticDecision.OVERTURN
        else:
            expected_decision = CriticDecision.CONFIRM
        if critic_run.decision is not expected_decision:
            raise ValueError("critic decision must match final correctness")
        return self

    def _validate_deterministic_empty_answer(self) -> None:
        if self.correctness is None:
            raise ValueError("deterministic completed outcome requires correctness")
        if self.correctness.value is not AnswerVerdict.INCORRECT:
            raise ValueError(
                "zero-call completed outcome must be an empty-answer error"
            )
        if self.reference_quality is not None or self.evidence is not None:
            raise ValueError("empty-answer outcome cannot contain model assessments")
        if (
            self.attribution is None
            or self.attribution.value is not AttributionCategory.GENERATION_EMPTY
            or self.attribution.attribution_strength is not AttributionStrength.STRONG
        ):
            raise ValueError("empty-answer outcome requires generation_empty + strong")
        if any(
            value is not None
            for value in (
                self.run_logs.correctness_result,
                self.run_logs.evidence_result,
                self.run_logs.critic_run,
            )
        ):
            raise ValueError("empty-answer outcome cannot contain model stage records")
        record = self.run_logs.attribution_run
        if (
            record is None
            or not record.deterministic
            or record.allowed_attributions != [AttributionCategory.GENERATION_EMPTY]
        ):
            raise ValueError(
                "empty-answer outcome requires deterministic generation_empty routing"
            )
        usage = self.run_logs.usage
        stats = self.run_logs.structured_output
        if any(
            (
                usage.model_attempts,
                usage.invoke_retry_count,
                usage.input_tokens,
                usage.output_tokens,
                stats.strict_parse_failures,
                stats.json_repair_attempt_count,
                stats.json_repair_success_count,
                stats.schema_retry_count,
                stats.semantic_retry_count,
            )
        ):
            raise ValueError("empty-answer outcome cannot contain model telemetry")
