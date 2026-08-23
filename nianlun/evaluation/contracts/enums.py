"""Enums shared across evaluation stages and public outcomes."""

from enum import StrEnum


class AnswerVerdict(StrEnum):
    CORRECT = "correct"
    PARTIALLY_CORRECT = "partially_correct"
    INCORRECT = "incorrect"
    UNCERTAIN = "uncertain"


class ReferenceQuality(StrEnum):
    ADEQUATE = "adequate"
    INCOMPLETE = "incomplete"
    CONFLICTING = "conflicting"
    UNKNOWN = "unknown"


class RetrievalCoverage(StrEnum):
    NONE = "none"
    PARTIAL = "partial"
    FULL = "full"
    UNCERTAIN = "uncertain"


class RetrievalNoise(StrEnum):
    NONE = "none"
    LIMITED = "limited"
    SUBSTANTIAL = "substantial"
    UNCERTAIN = "uncertain"


class EvidenceConsistency(StrEnum):
    CONSISTENT = "consistent"
    CONFLICTING = "conflicting"
    UNCERTAIN = "uncertain"


class SupportLevel(StrEnum):
    FULL = "full"
    PARTIAL = "partial"
    NONE = "none"
    CONFLICTING = "conflicting"
    UNCERTAIN = "uncertain"


class CriticDecision(StrEnum):
    CONFIRM = "confirm"
    OVERTURN = "overturn"
    UNCERTAIN = "uncertain"


class AttributionCategory(StrEnum):
    RETRIEVAL_MISSING = "retrieval_missing"
    RETRIEVAL_INCOMPLETE = "retrieval_incomplete"
    RETRIEVAL_NOISE = "retrieval_noise"
    GENERATION_EMPTY = "generation_empty"
    HALLUCINATION = "hallucination"
    GENERATION_INCOMPLETE = "generation_incomplete"
    REASONING_ERROR = "reasoning_error"
    UNKNOWN = "unknown"


class AttributionStrength(StrEnum):
    STRONG = "strong"
    PLAUSIBLE = "plausible"
    INSUFFICIENT = "insufficient"


class HallucinationEvidence(StrEnum):
    UNSUPPORTED = "unsupported"
    CONTRADICTED = "contradicted"


class CriticPromptId(StrEnum):
    REFERENCE_CHALLENGE = "reference_challenge"
    FALSE_NEGATIVE_RECOVERY = "false_negative_recovery"
    FALSE_POSITIVE_CORRECTION = "false_positive_correction"
    EVIDENCE_CONFLICT_RESOLUTION = "evidence_conflict_resolution"
    SEVERITY_BOUNDARY_CORRECTION = "severity_boundary_correction"
    GENERAL = "general"


class RoutingFlag(StrEnum):
    REFERENCE_QUALITY_ISSUE = "reference_quality_issue"
    VERDICT_EVIDENCE_TENSION = "verdict_evidence_tension"
    EVIDENCE_CONFLICT = "evidence_conflict"
    CONTEXTS_TRUNCATED = "contexts_truncated"


class EvaluationStatus(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"


class EvaluationStage(StrEnum):
    VALIDATION = "validation"
    CORRECTNESS = "correctness"
    EVIDENCE = "evidence"
    CRITIC = "critic"
    ATTRIBUTION = "attribution"
