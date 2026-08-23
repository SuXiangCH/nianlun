from __future__ import annotations

import asyncio
import json
from collections import deque
from dataclasses import dataclass
from typing import Any

import pytest

from nianlun.evaluation import EvaluationCase, RagEvaluator
from nianlun.evaluation.contracts import (
    AnswerVerdict,
    ContextItem,
    ReferenceQuality,
)
from nianlun.evaluation.stages.correctness.schema import CorrectnessResult
from nianlun.models.llm import (
    LLMCallTelemetry,
    LLMClient,
    LLMMetadata,
    ModelInvocationError,
    StructuredOutputTelemetry,
)


@dataclass
class FakeResponse:
    content: str
    usage_metadata: dict[str, int]


def _valid_payload(reason: str = "valid") -> str:
    return json.dumps(
        {
            "correctness": {
                "value": AnswerVerdict.CORRECT,
                "reason": reason,
            },
            "reference_quality": {
                "value": ReferenceQuality.ADEQUATE,
                "reason": "the reference is adequate",
            },
        }
    )


def test_judge_retries_transient_invocation_and_records_usage() -> None:
    responses: deque[Exception | FakeResponse] = deque(
        [
            TimeoutError("sensitive provider detail"),
            FakeResponse(
                content=json.dumps({"value": "not-an-enum"}),
                usage_metadata={"input_tokens": 10, "output_tokens": 2},
            ),
            FakeResponse(
                content=_valid_payload(),
                usage_metadata={"input_tokens": 12, "output_tokens": 3},
            ),
        ]
    )

    async def invoke(prompt: str) -> FakeResponse:
        del prompt
        response = responses.popleft()
        if isinstance(response, Exception):
            raise response
        return response

    judge = LLMClient(
        invoke,
        metadata=LLMMetadata(provider="fake", model="fake", temperature=0.0),
        max_schema_retries=1,
        max_invoke_retries=1,
        retry_base_delay_seconds=0,
    )

    async def run() -> tuple[CorrectnessResult, Any]:
        result = await judge.generate_structured_output(
            prompt="evaluate", schema=CorrectnessResult
        )
        return result, judge.last_call_telemetry()

    result, telemetry = asyncio.run(run())

    assert result.correctness.value is AnswerVerdict.CORRECT
    assert telemetry.model_attempts == 3
    assert telemetry.invoke_retry_count == 1
    assert telemetry.input_tokens == 22
    assert telemetry.output_tokens == 5
    assert telemetry.structured_output.schema_retry_count == 1


def test_llm_call_telemetry_merge_owns_all_llm_fields() -> None:
    first = LLMCallTelemetry(
        input_tokens=10,
        output_tokens=2,
        model_attempts=2,
        invoke_retry_count=1,
        structured_output=StructuredOutputTelemetry(
            strict_parse_failures=1,
            json_repair_attempt_count=1,
            json_repair_success_count=1,
            schema_retry_count=0,
        ),
    )
    second = LLMCallTelemetry(
        input_tokens=12,
        output_tokens=3,
        model_attempts=1,
        invoke_retry_count=0,
        structured_output=StructuredOutputTelemetry(schema_retry_count=1),
    )

    merged = first.merge(second)

    assert merged == LLMCallTelemetry(
        input_tokens=22,
        output_tokens=5,
        model_attempts=3,
        invoke_retry_count=1,
        structured_output=StructuredOutputTelemetry(
            strict_parse_failures=1,
            json_repair_attempt_count=1,
            json_repair_success_count=1,
            schema_retry_count=1,
        ),
    )


def test_judge_telemetry_is_isolated_between_concurrent_calls() -> None:
    async def invoke(prompt: str) -> Any:
        await asyncio.sleep(0)
        if prompt.startswith("repair"):
            return FakeResponse(
                content=_valid_payload("repaired")[:-1] + ",}",
                usage_metadata={"input_tokens": 7, "output_tokens": 2},
            )
        return FakeResponse(
            content=_valid_payload("strict"),
            usage_metadata={"input_tokens": 11, "output_tokens": 4},
        )

    judge = LLMClient(
        invoke,
        metadata=LLMMetadata(provider="fake", model="fake", temperature=0.0),
    )

    async def run_one(prompt: str) -> tuple[int, int, int, int]:
        await judge.generate_structured_output(prompt=prompt, schema=CorrectnessResult)
        telemetry = judge.last_call_telemetry()
        return (
            telemetry.input_tokens,
            telemetry.output_tokens,
            telemetry.structured_output.json_repair_attempt_count,
            telemetry.structured_output.json_repair_success_count,
        )

    async def run_both() -> list[tuple[int, int, int, int]]:
        return list(
            await asyncio.gather(run_one("repair this"), run_one("strict this"))
        )

    repair_stats, strict_stats = asyncio.run(run_both())

    assert repair_stats == (7, 2, 1, 1)
    assert strict_stats == (11, 4, 0, 0)


def test_llm_client_injects_schema_on_first_structured_call() -> None:
    prompts: list[str] = []

    async def invoke(prompt: str) -> FakeResponse:
        prompts.append(prompt)
        return FakeResponse(
            content=_valid_payload(),
            usage_metadata={"input_tokens": 1, "output_tokens": 1},
        )

    model = LLMClient(
        invoke,
        metadata=LLMMetadata(provider="fake", model="fake", temperature=0.0),
    )

    result = asyncio.run(
        model.generate_structured_output(
            prompt="evaluate this answer", schema=CorrectnessResult
        )
    )

    assert result.correctness.value is AnswerVerdict.CORRECT
    assert len(prompts) == 1
    assert prompts[0].startswith("evaluate this answer\n\nReturn only one JSON object")
    assert '"title": "CorrectnessResult"' in prompts[0]


def test_llm_client_supports_plain_text_generation() -> None:
    async def invoke(prompt: str) -> FakeResponse:
        assert prompt == "plain prompt"
        return FakeResponse(
            content="plain response",
            usage_metadata={"input_tokens": 4, "output_tokens": 2},
        )

    model = LLMClient(
        invoke,
        metadata=LLMMetadata(provider="fake", model="fake", temperature=0.0),
    )

    async def run() -> tuple[str, Any]:
        result = await model.generate("plain prompt")
        return result, model.last_call_telemetry()

    result, telemetry = asyncio.run(run())

    assert result == "plain response"
    assert telemetry.input_tokens == 4
    assert telemetry.output_tokens == 2
    assert telemetry.model_attempts == 1
    assert telemetry.invoke_retry_count == 0


def test_plain_generation_retries_transient_invocation() -> None:
    responses: deque[Exception | FakeResponse] = deque(
        [
            TimeoutError("sensitive provider detail"),
            FakeResponse(
                content="plain response",
                usage_metadata={"input_tokens": 6, "output_tokens": 3},
            ),
        ]
    )

    async def invoke(prompt: str) -> FakeResponse:
        assert prompt == "plain prompt"
        response = responses.popleft()
        if isinstance(response, Exception):
            raise response
        return response

    model = LLMClient(
        invoke,
        metadata=LLMMetadata(provider="fake", model="fake", temperature=0.0),
        max_invoke_retries=1,
        retry_base_delay_seconds=0,
    )

    async def run() -> tuple[str, Any]:
        result = await model.generate("plain prompt")
        return result, model.last_call_telemetry()

    result, telemetry = asyncio.run(run())

    assert result == "plain response"
    assert telemetry.input_tokens == 6
    assert telemetry.output_tokens == 3
    assert telemetry.model_attempts == 2
    assert telemetry.invoke_retry_count == 1


def test_plain_generation_normalizes_terminal_invocation_error() -> None:
    async def invoke(prompt: str) -> FakeResponse:
        del prompt
        raise ValueError("sensitive provider detail")

    model = LLMClient(
        invoke,
        metadata=LLMMetadata(provider="fake", model="fake", temperature=0.0),
    )

    async def run() -> Any:
        with pytest.raises(ModelInvocationError) as exc_info:
            await model.generate("plain prompt")
        return exc_info.value, model.last_call_telemetry()

    error, telemetry = asyncio.run(run())

    assert error.code == "model_invocation_failed"
    assert str(error) == "model provider request failed"
    assert "sensitive" not in str(error)
    assert telemetry.model_attempts == 1
    assert telemetry.invoke_retry_count == 0


def test_evaluator_aggregates_per_call_token_usage() -> None:
    async def invoke(prompt: str) -> FakeResponse:
        if '"title": "CorrectnessResult"' in prompt:
            payload = {
                "correctness": {"value": "correct", "reason": "correct"},
                "reference_quality": {
                    "value": "adequate",
                    "reason": "the reference is adequate",
                },
            }
        elif '"title": "EvidenceModelOutput"' in prompt:
            payload = {
                "retrieval_coverage": {
                    "value": "full",
                    "reason": "complete evidence",
                    "context_ids": ["ctx-1"],
                },
                "retrieval_noise": {
                    "value": "none",
                    "reason": "no noise",
                    "context_ids": [],
                },
                "evidence_consistency": {
                    "value": "consistent",
                    "reason": "consistent evidence",
                    "context_ids": [],
                },
                "reference_claim_assessments": [
                    {
                        "value": "full",
                        "reason": "reference claim supported",
                        "claim": "reference claim",
                        "context_ids": ["ctx-1"],
                    }
                ],
                "actual_claim_assessments": [
                    {
                        "value": "full",
                        "reason": "actual claim supported",
                        "claim": "actual claim",
                        "context_ids": ["ctx-1"],
                    }
                ],
            }
        else:
            payload = {
                "correctness": {"value": "correct", "reason": "correct"},
                "reference_quality": {
                    "value": "adequate",
                    "reason": "the reference is adequate",
                },
            }
        return FakeResponse(
            content=json.dumps(payload),
            usage_metadata={"input_tokens": 10, "output_tokens": 2},
        )

    judge = LLMClient(
        invoke,
        metadata=LLMMetadata(provider="fake", model="fake", temperature=0.0),
    )
    case = EvaluationCase(
        question="q",
        reference_answer="r",
        actual_answer="a",
        retrieval_contexts=[ContextItem(text="evidence")],
    )

    result = asyncio.run(RagEvaluator(judge=judge).evaluate(case))

    assert result.run_logs.usage.calls == 3
    assert result.run_logs.usage.model_attempts == 3
    assert result.run_logs.usage.invoke_retry_count == 0
    assert result.run_logs.usage.input_tokens == 30
    assert result.run_logs.usage.output_tokens == 6


def test_judge_retry_configuration_changes_evaluator_fingerprint() -> None:
    async def invoke(prompt: str) -> FakeResponse:
        del prompt
        return FakeResponse(
            content=_valid_payload(),
            usage_metadata={"input_tokens": 1, "output_tokens": 1},
        )

    metadata = LLMMetadata(provider="fake", model="fake", temperature=0.0)
    first = RagEvaluator(
        judge=LLMClient(invoke, metadata=metadata, max_invoke_retries=1)
    )
    second = RagEvaluator(
        judge=LLMClient(invoke, metadata=metadata, max_invoke_retries=3)
    )

    assert first.evaluator_fingerprint != second.evaluator_fingerprint


def test_model_behavior_configuration_changes_evaluator_fingerprint() -> None:
    async def invoke(prompt: str) -> FakeResponse:
        del prompt
        return FakeResponse(
            content=_valid_payload(),
            usage_metadata={"input_tokens": 1, "output_tokens": 1},
        )

    provider_default = RagEvaluator(
        judge=LLMClient(
            invoke,
            metadata=LLMMetadata(
                provider="fake",
                model="fake",
                temperature=0.0,
                enable_thinking=None,
                endpoint_identity="sha256:endpoint-a",
            ),
        )
    )
    explicit_enable = RagEvaluator(
        judge=LLMClient(
            invoke,
            metadata=LLMMetadata(
                provider="fake",
                model="fake",
                temperature=0.0,
                enable_thinking=True,
                endpoint_identity="sha256:endpoint-a",
            ),
        )
    )
    disabled = RagEvaluator(
        judge=LLMClient(
            invoke,
            metadata=LLMMetadata(
                provider="fake",
                model="fake",
                temperature=0.0,
                enable_thinking=False,
                endpoint_identity="sha256:endpoint-a",
            ),
        )
    )
    other_endpoint = RagEvaluator(
        judge=LLMClient(
            invoke,
            metadata=LLMMetadata(
                provider="fake",
                model="fake",
                temperature=0.0,
                enable_thinking=None,
                endpoint_identity="sha256:endpoint-b",
            ),
        )
    )

    assert (
        provider_default.evaluator_fingerprint == explicit_enable.evaluator_fingerprint
    )
    assert provider_default.evaluator_fingerprint != disabled.evaluator_fingerprint
    assert (
        provider_default.evaluator_fingerprint != other_endpoint.evaluator_fingerprint
    )
