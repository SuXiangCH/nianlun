"""Configuration for the isolated deep-search subagent."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DeepSearchConfig:
    """Hard limits for one deep-search runner.

    The values are deliberately local to the subagent module. Application and
    request wiring can provide an instance later without changing the runner's
    execution contract.
    """

    max_turns: int = 10
    timeout_seconds: float = 120.0
    max_concurrent: int = 2
    max_result_chars: int = 8_000
    max_answer_chars: int = 4_000
    max_evidence_items: int = 16
    max_evidence_text_chars: int = 600
    max_open_questions: int = 8
    max_open_question_chars: int = 400
    max_search_summary_chars: int = 1_000

    def __post_init__(self) -> None:
        positive_fields = (
            "max_turns",
            "max_concurrent",
            "max_result_chars",
            "max_answer_chars",
            "max_evidence_items",
            "max_evidence_text_chars",
            "max_open_questions",
            "max_open_question_chars",
            "max_search_summary_chars",
        )
        for name in positive_fields:
            value = getattr(self, name)
            if not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")

        if (
            not isinstance(self.timeout_seconds, (int, float))
            or self.timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be positive")
        if self.max_answer_chars > self.max_result_chars:
            raise ValueError("max_answer_chars cannot exceed max_result_chars")


__all__ = ["DeepSearchConfig"]
