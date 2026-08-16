"""批量测试：加载问题集、并发执行、失败重试、增量写出明细与汇总。

每个 worker 线程懒加载一套独立的 AgentRuntime（thread-local），避免 agent 与
RetrievalCollector 在并发下互相污染。结果按提交顺序写出 JSONL，保证可重现。
"""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from collections.abc import Callable
from typing import Any

from nianlun.config import RESULTS_DIR
from nianlun.knowledgebase import sanitize_text
from nianlun.agent.lead_agent.runtime import AgentRuntime

_BATCH_RUNTIME_LOCAL = threading.local()
RuntimeFactory = Callable[[], AgentRuntime]


# ============ 问题集 IO ============


def load_question_set(path: Path) -> list[dict[str, Any]]:
    """加载 JSON / JSONL 格式的问题集。"""
    if not path.exists():
        raise FileNotFoundError(f"问题集不存在: {path}")

    if path.suffix.lower() == ".jsonl":
        records = []
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                records.append(json.loads(line))
        return records

    with open(path, "r", encoding="utf-8") as f:
        payload = json.load(f)

    if not isinstance(payload, list):
        raise ValueError("问题集 JSON 顶层必须是数组。")
    return payload


def extract_question(record: dict[str, Any]) -> str:
    """从问题记录中提取问题文本。"""
    for key in ("question", "query", "prompt", "input"):
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    raise ValueError(f"记录缺少问题字段: {record}")


def default_batch_output_path(batch_file: Path) -> Path:
    """生成批量测试结果输出路径。"""
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    stem = batch_file.stem
    return RESULTS_DIR / f"{stem}_agent_batch_{ts}.jsonl"


def write_jsonl(path: Path, item: dict[str, Any]) -> None:
    """以 JSONL 形式追加写入一条记录。"""
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(item, ensure_ascii=False) + "\n")


# ============ 重试策略 ============


def is_retryable_batch_error(exc: Exception) -> bool:
    """判断单题失败是否适合做批量级重试。"""
    for attr in ("status_code", "status"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value in (408, 409, 429) or value >= 500

    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    if isinstance(status_code, int):
        return status_code in (408, 409, 429) or status_code >= 500

    message = f"{type(exc).__name__}: {sanitize_text(str(exc))}".lower()
    retry_markers = (
        "504 gateway time-out",
        "504 gateway timeout",
        "502 bad gateway",
        "503 service unavailable",
        "408 request timeout",
        "apitimeouterror",
        "apiconnectionerror",
        "internalservererror",
        "ratelimiterror",
        "timed out",
        "timeout",
        "rate limit",
        "too many requests",
        "temporarily unavailable",
        "connection reset",
        "connection aborted",
        "connection error",
        "server error",
    )
    return any(marker in message for marker in retry_markers)


def get_batch_retry_delay_sec(retry_index: int) -> float:
    """返回第 N 次重试前的退避时间。"""
    return min(float(2 ** max(retry_index - 1, 0)), 8.0)


# ============ 单条执行 ============


@dataclass
class BatchCaseResult:
    """单条批量任务的执行结果。"""

    local_idx: int
    case_index: int
    output_record: dict[str, Any]
    success: bool
    route: str
    route_source: str
    retrieved_count: int
    duration_sec: float
    attempt_count: int
    error: str | None = None


def get_batch_worker_runtime(
    tool_logging: bool,
    runtime_factory: RuntimeFactory,
) -> AgentRuntime:
    """通过显式 factory 为当前线程懒加载独立 AgentRuntime。"""
    factory_key = id(runtime_factory)
    runtime = getattr(_BATCH_RUNTIME_LOCAL, "runtime", None)
    if (
        runtime is None
        or runtime.tool_logging != tool_logging
        or getattr(_BATCH_RUNTIME_LOCAL, "factory_key", None) != factory_key
    ):
        runtime = runtime_factory()
        _BATCH_RUNTIME_LOCAL.runtime = runtime
        _BATCH_RUNTIME_LOCAL.factory_key = factory_key
    return runtime


def process_batch_case(
    record: Any,
    *,
    local_idx: int,
    case_index: int,
    runtime: AgentRuntime | None = None,
    runtime_factory: RuntimeFactory | None = None,
    tool_logging: bool = True,
    retry_times: int = 3,
) -> BatchCaseResult:
    """执行单条批量任务。"""
    started = time.perf_counter()
    attempt_count = 0

    try:
        question = extract_question(record)
        max_attempts = retry_times + 1

        while True:
            attempt_count += 1

            try:
                if runtime is not None:
                    active_runtime = runtime
                elif runtime_factory is not None:
                    active_runtime = get_batch_worker_runtime(
                        tool_logging=tool_logging,
                        runtime_factory=runtime_factory,
                    )
                else:
                    raise RuntimeError("批量 worker 缺少 runtime_factory。")

                run_result = active_runtime.invoke(
                    question,
                    thread_id=f"batch-{case_index}-{attempt_count}",
                )
                duration_sec = round(time.perf_counter() - started, 3)
                retrieved_snippets = run_result["retrieved_snippets"]
                route = run_result.get("route", "retrieval")
                route_source = run_result.get("route_source", "unknown")
                route_reason = run_result.get("route_reason", "")

                output_record = {
                    "case_index": case_index,
                    "question": question,
                    "expected_answer": record.get("answer")
                    if isinstance(record, dict)
                    else None,
                    "source_record": record,
                    "agent_answer": run_result["answer"],
                    "route": route,
                    "route_source": route_source,
                    "route_reason": route_reason,
                    "used_retrieval": route == "retrieval",
                    "retrieved_texts": run_result["retrieved_texts"],
                    "retrieved_snippets": retrieved_snippets,
                    "retrieved_count": len(retrieved_snippets),
                    "duration_sec": duration_sec,
                    "attempt_count": attempt_count,
                    "retry_count": max(attempt_count - 1, 0),
                    "error": None,
                    "success": True,
                }
                return BatchCaseResult(
                    local_idx=local_idx,
                    case_index=case_index,
                    output_record=output_record,
                    success=True,
                    route=route,
                    route_source=route_source,
                    retrieved_count=len(retrieved_snippets),
                    duration_sec=duration_sec,
                    attempt_count=attempt_count,
                )
            except Exception as exc:
                if attempt_count < max_attempts and is_retryable_batch_error(exc):
                    time.sleep(get_batch_retry_delay_sec(attempt_count))
                    continue
                raise
    except Exception as exc:
        duration_sec = round(time.perf_counter() - started, 3)
        question = record.get("question") if isinstance(record, dict) else None
        output_record = {
            "case_index": case_index,
            "question": question,
            "expected_answer": record.get("answer")
            if isinstance(record, dict)
            else None,
            "source_record": record,
            "agent_answer": None,
            "route": "error",
            "route_source": "error",
            "route_reason": "",
            "used_retrieval": False,
            "retrieved_texts": [],
            "retrieved_snippets": [],
            "retrieved_count": 0,
            "duration_sec": duration_sec,
            "attempt_count": attempt_count,
            "retry_count": max(attempt_count - 1, 0),
            "error": str(exc),
            "success": False,
        }
        return BatchCaseResult(
            local_idx=local_idx,
            case_index=case_index,
            output_record=output_record,
            success=False,
            route="error",
            route_source="error",
            retrieved_count=0,
            duration_sec=duration_sec,
            attempt_count=attempt_count,
            error=str(exc),
        )


def commit_batch_case_result(
    output_path: Path,
    summary: dict[str, Any],
    result: BatchCaseResult,
    total_selected: int,
) -> None:
    """更新 summary、打印进度并写出单条结果。"""
    attempt_suffix = (
        f" attempts={result.attempt_count}" if result.attempt_count > 1 else ""
    )
    summary["total_attempts"] += result.attempt_count
    summary["retried_case_count"] += int(result.attempt_count > 1)

    if result.success:
        summary["success_count"] += 1
        summary["direct_count"] += int(result.route == "direct")
        summary["retrieval_count"] += int(result.route == "retrieval")
        summary["total_retrieved_snippets"] += result.retrieved_count
        print(
            f"[{result.local_idx}/{total_selected}] OK  case={result.case_index} "
            f"route={result.route}/{result.route_source} "
            f"retrieved={result.retrieved_count} time={result.duration_sec}s"
            f"{attempt_suffix}"
        )
    else:
        summary["error_count"] += 1
        print(
            f"[{result.local_idx}/{total_selected}] ERR case={result.case_index} "
            f"time={result.duration_sec}s{attempt_suffix} error={result.error}"
        )

    write_jsonl(output_path, result.output_record)


# ============ 批量运行 ============


def run_batch(
    runtime: AgentRuntime,
    batch_file: Path,
    output_path: Path,
    offset: int = 0,
    limit: int | None = None,
    workers: int = 1,
    tool_logging: bool = True,
    retry_times: int = 3,
    runtime_factory: RuntimeFactory | None = None,
) -> dict[str, Any]:
    """批量运行问题集，增量写出问答与检索结果。"""
    records = load_question_set(batch_file)
    total_records = len(records)

    if offset < 0:
        raise ValueError("offset 不能小于 0")
    if limit is not None and limit <= 0:
        raise ValueError("limit 必须大于 0")
    if workers <= 0:
        raise ValueError("workers 必须大于 0")
    if retry_times < 0:
        raise ValueError("retry_times 不能小于 0")
    if workers > 1 and runtime_factory is None:
        raise ValueError("并发批量运行必须显式传入 runtime_factory")
    if offset >= total_records:
        raise ValueError(f"offset={offset} 超出问题集长度 {total_records}")

    selected = records[offset:] if limit is None else records[offset : offset + limit]
    output_path.parent.mkdir(parents=True, exist_ok=True)

    summary: dict[str, Any] = {
        "batch_file": str(batch_file),
        "output_path": str(output_path),
        "total_records": total_records,
        "offset": offset,
        "limit": limit,
        "retry_times": retry_times,
        "run_count": len(selected),
        "success_count": 0,
        "error_count": 0,
        "direct_count": 0,
        "retrieval_count": 0,
        "total_retrieved_snippets": 0,
        "retried_case_count": 0,
        "total_attempts": 0,
        "started_at": datetime.now().isoformat(timespec="seconds"),
    }

    print(f"📦 批量测试: {batch_file}")
    print(f"📝 输出文件: {output_path}")
    print(f"🔢 运行题数: {len(selected)} / {total_records}")
    print(f"⚙️ 并发 worker: {workers}")
    print(f"🔁 单题失败重试: {retry_times} 次")

    if workers == 1:
        for local_idx, record in enumerate(selected, start=1):
            case_index = offset + local_idx - 1
            result = process_batch_case(
                record,
                local_idx=local_idx,
                case_index=case_index,
                runtime=runtime,
                tool_logging=tool_logging,
                retry_times=retry_times,
            )
            commit_batch_case_result(output_path, summary, result, len(selected))
    else:
        if tool_logging:
            print("⚠️ 并发模式下工具日志会交错，建议配合 --quiet-tools 使用。")

        pending_results: dict[int, BatchCaseResult] = {}
        next_to_write = 1

        with ThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix="rag-batch",
        ) as executor:
            future_to_local_idx = {
                executor.submit(
                    process_batch_case,
                    record,
                    local_idx=local_idx,
                    case_index=offset + local_idx - 1,
                    runtime_factory=runtime_factory,
                    tool_logging=tool_logging,
                    retry_times=retry_times,
                ): local_idx
                for local_idx, record in enumerate(selected, start=1)
            }

            for future in as_completed(future_to_local_idx):
                result = future.result()
                pending_results[result.local_idx] = result

                while next_to_write in pending_results:
                    ordered_result = pending_results.pop(next_to_write)
                    commit_batch_case_result(
                        output_path,
                        summary,
                        ordered_result,
                        len(selected),
                    )
                    next_to_write += 1

    summary["finished_at"] = datetime.now().isoformat(timespec="seconds")
    summary["total_retries"] = max(summary["total_attempts"] - summary["run_count"], 0)
    summary["avg_retrieved_snippets_on_retrieval"] = round(
        summary["total_retrieved_snippets"] / max(summary["retrieval_count"], 1), 3
    )
    return summary


def save_batch_summary(output_path: Path, summary: dict[str, Any]) -> Path:
    """保存批量测试 summary。"""
    summary_path = output_path.with_suffix(".summary.json")
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    return summary_path
