"""Generic, structured evaluation for RAG outputs."""

from .orchestration.pipeline import EvaluationConfig, RagEvaluator
from .reporting.summary import summarize_results
from .contracts import EvaluationCase, EvaluationOutcome, EvaluationSummary

__all__ = [
    "EvaluationCase",
    "EvaluationConfig",
    "EvaluationOutcome",
    "EvaluationSummary",
    "RagEvaluator",
    "summarize_results",
]
