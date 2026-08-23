"""Shared structured-model invocation, correction retries, and telemetry."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

from pydantic import BaseModel

from nianlun.evaluation.contracts.run_logs import EvaluationUsage, StructuredOutputStats
from nianlun.evaluation.stages.common import semantic_correction_prompt
from nianlun.models.llm import LLMCallTelemetry, StructuredLLM

T = TypeVar("T", bound=BaseModel)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CallTelemetry:
    llm: LLMCallTelemetry = field(default_factory=LLMCallTelemetry)
    semantic_retry_count: int = 0


@dataclass(frozen=True, slots=True)
class StructuredGeneration(Generic[T]):
    output: T
    telemetry: CallTelemetry


class StructuredGenerationError(RuntimeError):
    def __init__(self, cause: Exception, telemetry: CallTelemetry) -> None:
        super().__init__("structured generation failed")
        self.cause = cause
        self.telemetry = telemetry


@dataclass(slots=True)
class UsageAccumulator:
    calls: int = 0
    model_attempts: int = 0
    invoke_retry_count: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    strict_parse_failures: int = 0
    json_repair_attempt_count: int = 0
    json_repair_success_count: int = 0
    schema_retry_count: int = 0
    semantic_retry_count: int = 0

    def add(
        self,
        result: StructuredGeneration[Any] | StructuredGenerationError,
    ) -> None:
        telemetry = result.telemetry
        llm = telemetry.llm
        structured = llm.structured_output
        self.model_attempts += llm.model_attempts
        self.invoke_retry_count += llm.invoke_retry_count
        self.input_tokens += llm.input_tokens
        self.output_tokens += llm.output_tokens
        self.strict_parse_failures += structured.strict_parse_failures
        self.json_repair_attempt_count += structured.json_repair_attempt_count
        self.json_repair_success_count += structured.json_repair_success_count
        self.schema_retry_count += structured.schema_retry_count
        self.semantic_retry_count += telemetry.semantic_retry_count

    def usage(self) -> EvaluationUsage:
        return EvaluationUsage(
            calls=self.calls,
            model_attempts=self.model_attempts,
            invoke_retry_count=self.invoke_retry_count,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
        )

    def structured_output(self) -> StructuredOutputStats:
        return StructuredOutputStats(
            strict_parse_failures=self.strict_parse_failures,
            json_repair_attempt_count=self.json_repair_attempt_count,
            json_repair_success_count=self.json_repair_success_count,
            schema_retry_count=self.schema_retry_count,
            semantic_retry_count=self.semantic_retry_count,
        )


class EvaluationRuntime:
    """Generate structured stage output with shared retries and telemetry."""

    def __init__(
        self,
        judge: StructuredLLM,
        *,
        max_semantic_retries: int,
    ) -> None:
        self.judge = judge
        self.max_semantic_retries = max_semantic_retries

    async def generate_structured(
        self,
        prompt: str,
        schema: type[T],
        semantic_validator: Callable[[T], None] | None = None,
    ) -> StructuredGeneration[T]:
        current_prompt = prompt
        aggregate = CallTelemetry()
        logger.info(
            "evaluation.structured.start schema=%s max_semantic_retries=%d",
            schema.__name__,
            self.max_semantic_retries,
        )
        for semantic_attempt in range(self.max_semantic_retries + 1):
            try:
                value = await self.judge.generate_structured_output(
                    prompt=current_prompt,
                    schema=schema,
                )
                # Custom StructuredLLM implementations are revalidated at this boundary.
                validated = schema.model_validate(value)
            except Exception as exc:
                aggregate = _merge_telemetry(
                    aggregate,
                    _judge_call_telemetry(self.judge),
                )
                logger.warning(
                    "evaluation.structured.failed schema=%s attempt=%d error_type=%s",
                    schema.__name__,
                    semantic_attempt + 1,
                    type(exc).__name__,
                )
                raise StructuredGenerationError(exc, aggregate) from exc

            aggregate = _merge_telemetry(
                aggregate,
                _judge_call_telemetry(self.judge),
            )
            if semantic_validator is None:
                logger.info(
                    "evaluation.structured.completed schema=%s attempts=%d semantic_retries=%d",
                    schema.__name__,
                    semantic_attempt + 1,
                    aggregate.semantic_retry_count,
                )
                return StructuredGeneration(output=validated, telemetry=aggregate)
            try:
                semantic_validator(validated)
            except ValueError as exc:
                if semantic_attempt == self.max_semantic_retries:
                    logger.warning(
                        "evaluation.structured.failed schema=%s attempt=%d error_type=%s",
                        schema.__name__,
                        semantic_attempt + 1,
                        type(exc).__name__,
                    )
                    raise StructuredGenerationError(exc, aggregate) from exc
                aggregate = _increment_semantic_retry(aggregate)
                logger.warning(
                    "evaluation.structured.semantic_retry schema=%s retry=%d",
                    schema.__name__,
                    aggregate.semantic_retry_count,
                )
                current_prompt = semantic_correction_prompt(
                    prompt,
                    schema,
                    str(exc)[:500],
                )
                continue
            logger.info(
                "evaluation.structured.completed schema=%s attempts=%d semantic_retries=%d",
                schema.__name__,
                semantic_attempt + 1,
                aggregate.semantic_retry_count,
            )
            return StructuredGeneration(output=validated, telemetry=aggregate)
        raise AssertionError("unreachable")


def _judge_call_telemetry(judge: StructuredLLM) -> CallTelemetry:
    getter = getattr(judge, "last_call_telemetry", None)
    if callable(getter):
        telemetry = getter()
        if isinstance(telemetry, LLMCallTelemetry):
            return CallTelemetry(llm=telemetry)
    return CallTelemetry(llm=LLMCallTelemetry(model_attempts=1))


def _merge_telemetry(first: CallTelemetry, second: CallTelemetry) -> CallTelemetry:
    return CallTelemetry(
        llm=first.llm.merge(second.llm),
        semantic_retry_count=(first.semantic_retry_count + second.semantic_retry_count),
    )


def _increment_semantic_retry(telemetry: CallTelemetry) -> CallTelemetry:
    return CallTelemetry(
        llm=telemetry.llm,
        semantic_retry_count=telemetry.semantic_retry_count + 1,
    )
