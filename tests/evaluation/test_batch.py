from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import TypeVar

import pytest
from pydantic import BaseModel

from nianlun.evaluation import EvaluationCase, RagEvaluator
from nianlun.evaluation.adapters import adapt_nianlun_result
from nianlun.evaluation.batch import evaluate_cases, evaluate_jsonl
from nianlun.evaluation.contracts import (
    AnswerVerdict,
    EvaluationOutcome,
    EvidenceConsistency,
    JudgeMetadata,
    ReferenceQuality,
    RetrievalCoverage,
    RetrievalNoise,
    StructuredOutputStats,
    SupportLevel,
    case_fingerprint,
)
from nianlun.evaluation.stages.correctness.schema import (
    CorrectnessAssessment,
    CorrectnessResult,
    ReferenceQualityAssessment,
)
from nianlun.evaluation.stages.critic.schema import CriticResult
from nianlun.evaluation.stages.evidence.schema import (
    ClaimEvidenceAssessment,
    EvidenceConsistencyAssessment,
    EvidenceModelOutput,
    RetrievalCoverageAssessment,
    RetrievalNoiseAssessment,
)

T = TypeVar("T", bound=BaseModel)


class UnusedJudge:
    metadata = JudgeMetadata(provider="fake", model="fake", temperature=0.0)
    structured_output = StructuredOutputStats()

    async def generate_structured_output(self, *, prompt: str, schema: type[T]) -> T:
        raise AssertionError(
            f"empty answers must not call judge: prompt={prompt!r}, schema={schema}"
        )


class TrackingEvaluator(RagEvaluator):
    def __init__(self) -> None:
        super().__init__(judge=UnusedJudge())
        self.active: int = 0
        self.max_active: int = 0

    async def evaluate(self, case: EvaluationCase) -> EvaluationOutcome:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(0.01)
            return await super().evaluate(case)
        finally:
            self.active -= 1


def _case_payload() -> dict[str, object]:
    return {
        "question": "q",
        "reference_answer": "r",
        "actual_answer": "",
        "retrieval_contexts": [{"text": "context without an explicit id"}],
    }


def test_evaluate_cases_bounds_active_tasks_and_preserves_order() -> None:
    evaluator = TrackingEvaluator()
    cases = [
        EvaluationCase(
            question=f"q-{index}",
            reference_answer="r",
            actual_answer="",
            retrieval_contexts=[],
        )
        for index in range(7)
    ]

    results = asyncio.run(evaluate_cases(evaluator, cases, workers=3))

    assert evaluator.max_active == 3
    assert [result.run_logs.case_fingerprint for result in results] == [
        case_fingerprint(case) for case in cases
    ]


def test_jsonl_writes_each_result_and_recovers_matching_fingerprint(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    input_path = tmp_path / "input.jsonl"
    output_path = tmp_path / "output.jsonl"
    input_path.write_text(
        json.dumps(_case_payload()) + "\n" + json.dumps({"not": "a case"}) + "\n",
        encoding="utf-8",
    )
    evaluator = RagEvaluator(judge=UnusedJudge())
    caplog.set_level(logging.INFO, logger="nianlun.evaluation.batch")

    asyncio.run(
        evaluate_jsonl(
            evaluator,
            input_path=input_path,
            output_path=output_path,
            workers=2,
        )
    )
    first_run = [
        json.loads(line)
        for line in output_path.read_text(encoding="utf-8").splitlines()
    ]
    result_record = next(record["result"] for record in first_run if "result" in record)
    error_record = next(record["error"] for record in first_run if "error" in record)
    assert result_record["correctness"]["value"] == "incorrect"
    assert error_record["code"] == "invalid_input"
    assert "evaluation.batch.started cases=2 workers=2 resumable_cases=0" in caplog.text
    assert "[1/2] ERR input_line=2 code=invalid_input" in caplog.text
    assert "[2/2] OK input_line=1 verdict=incorrect" in caplog.text
    assert (
        "evaluation.batch.completed processed=2 completed=1 failed=0 invalid=1 "
        "skipped=0" in caplog.text
    )

    asyncio.run(
        evaluate_jsonl(
            evaluator,
            input_path=input_path,
            output_path=output_path,
            workers=1,
        )
    )
    second_run = [
        json.loads(line)
        for line in output_path.read_text(encoding="utf-8").splitlines()
    ]
    assert len(second_run) == 4
    skipped_record = next(
        record["skipped"] for record in second_run if "skipped" in record
    )
    assert skipped_record["code"] == "duplicate_case"
    assert skipped_record["duplicate_of_input_line"] == 1
    assert "SKIP input_line=1 duplicate_of=1" in caplog.text


class AlwaysFailJudge:
    metadata = JudgeMetadata(provider="fake", model="fake", temperature=0.0)

    async def generate_structured_output(self, *, prompt: str, schema: type[T]) -> T:
        del prompt, schema
        raise RuntimeError("secret provider request and credential")


class SuccessfulJudge:
    metadata = JudgeMetadata(provider="fake", model="fake", temperature=0.0)

    async def generate_structured_output(self, *, prompt: str, schema: type[T]) -> T:
        del prompt
        values: Mapping[type[BaseModel], BaseModel] = {
            CorrectnessResult: CorrectnessResult(
                correctness=CorrectnessAssessment(
                    value=AnswerVerdict.CORRECT,
                    reason="correct",
                ),
                reference_quality=ReferenceQualityAssessment(
                    value=ReferenceQuality.ADEQUATE,
                    reason="the reference is adequate",
                ),
            ),
            EvidenceModelOutput: EvidenceModelOutput(
                retrieval_coverage=RetrievalCoverageAssessment(
                    value=RetrievalCoverage.FULL,
                    reason="complete evidence",
                    context_ids=["ctx-1"],
                ),
                retrieval_noise=RetrievalNoiseAssessment(
                    value=RetrievalNoise.NONE, reason="no noise"
                ),
                evidence_consistency=EvidenceConsistencyAssessment(
                    value=EvidenceConsistency.CONSISTENT, reason="consistent"
                ),
                reference_claim_assessments=[
                    ClaimEvidenceAssessment(
                        value=SupportLevel.FULL,
                        reason="reference claim supported",
                        claim="reference claim",
                        context_ids=["ctx-1"],
                    )
                ],
                actual_claim_assessments=[
                    ClaimEvidenceAssessment(
                        value=SupportLevel.FULL,
                        reason="actual claim supported",
                        claim="actual claim",
                        context_ids=["ctx-1"],
                    )
                ],
            ),
            CriticResult: CriticResult(
                correctness=CorrectnessAssessment(
                    value=AnswerVerdict.CORRECT,
                    reason="correct",
                ),
                reference_quality=ReferenceQualityAssessment(
                    value=ReferenceQuality.ADEQUATE,
                    reason="the reference is adequate",
                ),
            ),
        }
        return schema.model_validate(values[schema])


def test_failed_results_are_retried_and_errors_are_sanitized(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    input_path = tmp_path / "input.jsonl"
    output_path = tmp_path / "output.jsonl"
    payload = _case_payload()
    payload["actual_answer"] = "answer"
    input_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    caplog.set_level(logging.INFO, logger="nianlun.evaluation.batch")

    asyncio.run(
        evaluate_jsonl(
            RagEvaluator(judge=AlwaysFailJudge()),
            input_path=input_path,
            output_path=output_path,
        )
    )
    first = json.loads(output_path.read_text(encoding="utf-8"))
    assert first["result"]["evaluation_status"] == "failed"
    serialized = json.dumps(first)
    assert "secret" not in serialized
    assert "credential" not in serialized
    assert "[1/1] ERR input_line=1 stage=correctness" in caplog.text
    assert "secret" not in caplog.text
    assert "credential" not in caplog.text

    asyncio.run(
        evaluate_jsonl(
            RagEvaluator(judge=SuccessfulJudge()),
            input_path=input_path,
            output_path=output_path,
        )
    )
    records = [
        json.loads(line)
        for line in output_path.read_text(encoding="utf-8").splitlines()
    ]
    assert [record["result"]["evaluation_status"] for record in records] == [
        "failed",
        "completed",
    ]


def test_jsonl_deduplicates_cases_within_the_same_run(tmp_path: Path) -> None:
    input_path = tmp_path / "input.jsonl"
    output_path = tmp_path / "output.jsonl"
    line = json.dumps(_case_payload()) + "\n"
    input_path.write_text(line + line, encoding="utf-8")

    asyncio.run(
        evaluate_jsonl(
            RagEvaluator(judge=UnusedJudge()),
            input_path=input_path,
            output_path=output_path,
            workers=2,
        )
    )

    records = [
        json.loads(line)
        for line in output_path.read_text(encoding="utf-8").splitlines()
    ]
    assert len(records) == 2
    skipped = next(record["skipped"] for record in records if "skipped" in record)
    assert skipped == {"code": "duplicate_case", "duplicate_of_input_line": 1}


def test_jsonl_rejects_same_input_and_output_path(tmp_path: Path) -> None:
    path = tmp_path / "cases.jsonl"
    path.write_text(json.dumps(_case_payload()) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="must be different"):
        asyncio.run(
            evaluate_jsonl(
                RagEvaluator(judge=UnusedJudge()),
                input_path=path,
                output_path=path,
            )
        )


def test_jsonl_does_not_resume_from_a_malformed_completed_record(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "input.jsonl"
    output_path = tmp_path / "output.jsonl"
    input_path.write_text(json.dumps(_case_payload()) + "\n", encoding="utf-8")
    evaluator = RagEvaluator(judge=UnusedJudge())
    output_path.write_text(
        json.dumps(
            {
                "result": {
                    "evaluation_status": "completed",
                    "run_logs": {
                        "case_fingerprint": "fabricated",
                        "evaluator_fingerprint": evaluator.evaluator_fingerprint,
                    },
                }
            }
        )
        + "\n",
        encoding="utf-8",
    )

    asyncio.run(
        evaluate_jsonl(
            evaluator,
            input_path=input_path,
            output_path=output_path,
        )
    )

    assert len(output_path.read_text(encoding="utf-8").splitlines()) == 2


def test_nianlun_adapter_only_maps_contract_fields() -> None:
    case = adapt_nianlun_result(
        {
            "question": "q",
            "expected_answer": "r",
            "agent_answer": "a",
            "retrieved_snippets": [
                {
                    "text": "evidence",
                    "citation_id": 1,
                    "doc_id": "document-1",
                    "doc_name": "source.pdf",
                    "node_id": "node-1",
                    "line_spec": "4-7",
                    "score": 0.9,
                }
            ],
            "private_runtime_data": {"ignored": True},
        }
    )
    assert case.retrieval_contexts[0].context_id == "1"
    assert case.retrieval_contexts[0].location == "node-1:4-7"


def test_nianlun_adapter_rejects_failed_source_records() -> None:
    with pytest.raises(ValueError, match="unsuccessful"):
        adapt_nianlun_result(
            {
                "question": "q",
                "expected_answer": "r",
                "agent_answer": None,
                "retrieved_snippets": [],
                "success": False,
            }
        )
