"""Recoverable JSONL batch execution for the common evaluation contract."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

from nianlun.evaluation.contracts.case import EvaluationCase
from nianlun.evaluation.contracts.outcome import EvaluationOutcome
from nianlun.evaluation.orchestration.pipeline import RagEvaluator

CaseAdapter = Callable[[Mapping[str, Any]], EvaluationCase]
logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _BatchProgress:
    total: int
    processed: int = 0
    completed: int = 0
    failed: int = 0
    invalid: int = 0
    skipped: int = 0


async def evaluate_cases(
    evaluator: RagEvaluator,
    cases: Iterable[EvaluationCase],
    *,
    workers: int = 1,
) -> list[EvaluationOutcome]:
    """Evaluate cases with bounded active tasks while preserving input order."""
    if workers < 1:
        raise ValueError("workers must be at least one")
    iterator = enumerate(cases)
    active: dict[asyncio.Task[EvaluationOutcome], int] = {}
    results: dict[int, EvaluationOutcome] = {}

    def schedule_next() -> bool:
        try:
            index, case = next(iterator)
        except StopIteration:
            return False
        active[asyncio.create_task(evaluator.evaluate(case))] = index
        return True

    for _ in range(workers):
        if not schedule_next():
            break
    try:
        while active:
            done, _ = await asyncio.wait(active, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                index = active.pop(task)
                results[index] = task.result()
                schedule_next()
    except BaseException:
        for task in active:
            task.cancel()
        await asyncio.gather(*active, return_exceptions=True)
        raise
    return [results[index] for index in range(len(results))]


async def evaluate_jsonl(
    evaluator: RagEvaluator,
    *,
    input_path: Path,
    output_path: Path,
    workers: int = 1,
    adapter: CaseAdapter | None = None,
) -> None:
    """Evaluate JSONL incrementally; compatible completed records are skipped.

    Non-completed records (for example failures from an earlier run) are
    re-evaluated, so the output file may contain several records for one case;
    readers should prefer the latest record per case fingerprint. Duplicate
    cases receive a ``skipped`` record that points at the input line of the
    record they duplicate.
    """
    if input_path.resolve() == output_path.resolve():
        raise ValueError("input and output paths must be different")
    if workers < 1:
        raise ValueError("workers must be at least one")
    started = time.perf_counter()
    progress = _BatchProgress(total=_count_non_blank_lines(input_path))
    completed = _completed_keys(output_path, evaluator.evaluator_fingerprint)
    scheduled: dict[str, int] = completed
    logger.info(
        "evaluation.batch.started cases=%d workers=%d resumable_cases=%d",
        progress.total,
        workers,
        len(completed),
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    active: dict[asyncio.Task[EvaluationOutcome], int] = {}
    with output_path.open("a", encoding="utf-8") as destination:
        with input_path.open(encoding="utf-8") as source:
            for line_number, line in enumerate(source, start=1):
                if not line.strip():
                    continue
                try:
                    raw = json.loads(line)
                    if not isinstance(raw, Mapping):
                        raise ValueError("line must be a JSON object")
                    case = (
                        adapter(raw) if adapter else EvaluationCase.model_validate(raw)
                    )
                    case_key = evaluator_case_key(case)
                    if case_key in scheduled:
                        _write_skipped(destination, line_number, scheduled[case_key])
                        progress.processed += 1
                        progress.skipped += 1
                        logger.info(
                            "[%d/%d] SKIP input_line=%d duplicate_of=%d",
                            progress.processed,
                            progress.total,
                            line_number,
                            scheduled[case_key],
                        )
                        continue
                    scheduled[case_key] = line_number
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    _write_error(destination, line_number, exc, invalid_input=True)
                    progress.processed += 1
                    progress.invalid += 1
                    logger.warning(
                        "[%d/%d] ERR input_line=%d code=invalid_input",
                        progress.processed,
                        progress.total,
                        line_number,
                    )
                    continue
                task = asyncio.create_task(evaluator.evaluate(case))
                active[task] = line_number
                if len(active) >= workers:
                    await _write_completed(destination, active, progress)
        while active:
            await _write_completed(destination, active, progress)
    logger.info(
        "evaluation.batch.completed processed=%d completed=%d failed=%d "
        "invalid=%d skipped=%d duration_sec=%.3f",
        progress.processed,
        progress.completed,
        progress.failed,
        progress.invalid,
        progress.skipped,
        time.perf_counter() - started,
    )


def evaluator_case_key(case: EvaluationCase) -> str:
    from nianlun.evaluation.contracts.case import case_fingerprint, normalize_contexts

    normalized = case if case.is_empty_answer else normalize_contexts(case)
    return case_fingerprint(normalized)


async def _write_completed(
    destination: TextIO,
    active: dict[asyncio.Task[EvaluationOutcome], int],
    progress: _BatchProgress,
) -> None:
    done, _ = await asyncio.wait(active, return_when=asyncio.FIRST_COMPLETED)
    for task in done:
        line_number = active.pop(task)
        try:
            result = task.result()
        except Exception as exc:
            _write_error(destination, line_number, exc, invalid_input=False)
            progress.processed += 1
            progress.failed += 1
            logger.warning(
                "[%d/%d] ERR input_line=%d code=evaluation_task_failed",
                progress.processed,
                progress.total,
                line_number,
            )
        else:
            _write_record(
                destination,
                {
                    "input_line": line_number,
                    "result": result.model_dump(mode="json"),
                },
            )
            progress.processed += 1
            if result.evaluation_status.value == "completed":
                progress.completed += 1
                correctness = result.correctness
                attribution = result.attribution
                logger.info(
                    "[%d/%d] OK input_line=%d verdict=%s attribution=%s duration_ms=%d",
                    progress.processed,
                    progress.total,
                    line_number,
                    correctness.value if correctness is not None else "none",
                    attribution.value if attribution is not None else "none",
                    result.run_logs.duration_ms,
                )
            else:
                progress.failed += 1
                error = result.error
                logger.warning(
                    "[%d/%d] ERR input_line=%d stage=%s code=%s duration_ms=%d",
                    progress.processed,
                    progress.total,
                    line_number,
                    error.stage if error is not None else "unknown",
                    error.code if error is not None else "unknown",
                    result.run_logs.duration_ms,
                )


def _write_error(
    destination: TextIO,
    line_number: int,
    exc: Exception,  # Deliberately unused: exception details stay out of the output.
    *,
    invalid_input: bool,
) -> None:
    code = "invalid_input" if invalid_input else "evaluation_task_failed"
    message = (
        "input line is not a valid evaluation case"
        if invalid_input
        else "evaluation task failed"
    )
    _write_record(
        destination,
        {
            "input_line": line_number,
            "error": {"code": code, "message": message},
        },
    )


def _write_skipped(destination: TextIO, line_number: int, duplicate_of: int) -> None:
    _write_record(
        destination,
        {
            "input_line": line_number,
            "skipped": {
                "code": "duplicate_case",
                "duplicate_of_input_line": duplicate_of,
            },
        },
    )


def _write_record(destination: TextIO, payload: Mapping[str, Any]) -> None:
    destination.write(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
    )
    destination.flush()


def _count_non_blank_lines(input_path: Path) -> int:
    with input_path.open(encoding="utf-8") as source:
        return sum(bool(line.strip()) for line in source)


def _completed_keys(output_path: Path, evaluator_fingerprint: str) -> dict[str, int]:
    """Map each completed case fingerprint to the input line of its record."""
    keys: dict[str, int] = {}
    if not output_path.exists():
        return keys
    with output_path.open(encoding="utf-8") as source:
        for line in source:
            try:
                payload = json.loads(line)
                result = EvaluationOutcome.model_validate(payload["result"])
                if result.evaluation_status.value != "completed":
                    continue
                key = result.run_logs.case_fingerprint
                result_fingerprint = result.run_logs.evaluator_fingerprint
                input_line = payload["input_line"]
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                continue
            if (
                isinstance(key, str)
                and result_fingerprint == evaluator_fingerprint
                and isinstance(input_line, int)
            ):
                keys[key] = input_line
    return keys
