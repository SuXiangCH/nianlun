"""Public contracts shared by the evaluation pipeline and its stages."""

from nianlun.evaluation.contracts.assessment import MetricAssessment
from nianlun.evaluation.contracts.base import EvaluationSchema
from nianlun.evaluation.contracts.case import (
    ContextItem,
    EvaluationCase,
    case_fingerprint,
    normalize_contexts,
)
from nianlun.evaluation.contracts.enums import *  # noqa: F403
from nianlun.evaluation.contracts.labels import (
    ATTRIBUTION_ANNOTATIONS,
    ATTRIBUTION_LABEL_VERSION,
    LocalizedEnumAnnotation,
)
from nianlun.evaluation.contracts.outcome import EvaluationOutcome
from nianlun.evaluation.contracts.run_logs import (
    EvaluationError,
    EvaluationRunLogs,
    EvaluationUsage,
    InputStats,
    JudgeMetadata,
    PromptVersions,
    StructuredOutputStats,
)
from nianlun.evaluation.contracts.summary import EvaluationSummary

__all__ = [
    "ATTRIBUTION_ANNOTATIONS",
    "ATTRIBUTION_LABEL_VERSION",
    "ContextItem",
    "EvaluationCase",
    "EvaluationError",
    "EvaluationOutcome",
    "EvaluationRunLogs",
    "EvaluationSchema",
    "EvaluationSummary",
    "EvaluationUsage",
    "InputStats",
    "JudgeMetadata",
    "LocalizedEnumAnnotation",
    "MetricAssessment",
    "PromptVersions",
    "StructuredOutputStats",
    "case_fingerprint",
    "normalize_contexts",
]
