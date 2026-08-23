"""Cross-stage execution logs and telemetry contracts."""

from pydantic import Field

from nianlun.evaluation.contracts.base import EvaluationSchema
from nianlun.evaluation.contracts.enums import EvaluationStage
from nianlun.evaluation.stages.attribution.schema import AttributionRunRecord
from nianlun.evaluation.stages.correctness.schema import CorrectnessResult
from nianlun.evaluation.stages.critic.schema import CriticRunRecord
from nianlun.evaluation.stages.evidence.schema import EvidenceResult


class PromptVersions(EvaluationSchema):
    correctness: str = Field(min_length=1)
    evidence: str = Field(min_length=1)
    critic_common: str = Field(min_length=1)
    critic_branch: str = Field(min_length=1)
    attribution: str = Field(min_length=1)
    semantic_correction: str = Field(min_length=1)


class InputStats(EvaluationSchema):
    retrieval_context_count: int = Field(ge=0)
    contexts_truncated: bool


class JudgeMetadata(EvaluationSchema):
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    temperature: float


class EvaluationUsage(EvaluationSchema):
    calls: int = Field(ge=0)
    model_attempts: int = Field(ge=0)
    invoke_retry_count: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)


class StructuredOutputStats(EvaluationSchema):
    strict_parse_failures: int = Field(default=0, ge=0)
    json_repair_attempt_count: int = Field(default=0, ge=0)
    json_repair_success_count: int = Field(default=0, ge=0)
    schema_retry_count: int = Field(default=0, ge=0)
    semantic_retry_count: int = Field(default=0, ge=0)


class EvaluationError(EvaluationSchema):
    stage: EvaluationStage
    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    retryable: bool = False


class EvaluationRunLogs(EvaluationSchema):
    case_fingerprint: str = Field(min_length=1)
    evaluator_fingerprint: str = Field(min_length=1)
    evaluation_version: str = Field(min_length=1)
    routing_version: str = Field(min_length=1)
    prompt_versions: PromptVersions
    input_stats: InputStats
    correctness_result: CorrectnessResult | None
    evidence_result: EvidenceResult | None
    critic_run: CriticRunRecord | None
    attribution_run: AttributionRunRecord | None
    judge: JudgeMetadata
    usage: EvaluationUsage
    structured_output: StructuredOutputStats
    duration_ms: int = Field(ge=0)
