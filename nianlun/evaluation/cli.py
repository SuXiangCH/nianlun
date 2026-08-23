"""Command line entry point for generic JSONL evaluation."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path
from typing import TextIO, cast

from nianlun.config import get_enable_thinking
from nianlun.models.llm import build_llm

from nianlun.evaluation.adapters import adapt_nianlun_result
from nianlun.evaluation.batch import evaluate_jsonl
from nianlun.evaluation.orchestration.pipeline import EvaluationConfig, RagEvaluator
from nianlun.evaluation.reporting.excel import export_excel

logger = logging.getLogger(__name__)


def configure_evaluation_log_output() -> None:
    """Expose evaluation logs as plain-text CLI output without duplicates."""
    evaluation_logger = logging.getLogger("nianlun.evaluation")
    handler = next(
        (
            candidate
            for candidate in evaluation_logger.handlers
            if getattr(candidate, "_nianlun_cli_evaluation_handler", False)
        ),
        None,
    )
    if handler is None:
        handler = logging.StreamHandler(sys.stdout)
        handler._nianlun_cli_evaluation_handler = True  # type: ignore[attr-defined]
        handler.setFormatter(logging.Formatter("%(message)s"))
        evaluation_logger.addHandler(handler)
    else:
        # stdout may be replaced by a test runner or embedding CLI between calls.
        cast(logging.StreamHandler[TextIO], handler).setStream(sys.stdout)
    evaluation_logger.setLevel(logging.INFO)
    evaluation_logger.propagate = False


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Evaluate normalized RAG results from JSONL"
    )
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--excel-output",
        type=Path,
        help="Optional concise Excel report path for the latest JSONL results.",
    )
    parser.add_argument("--judge-model", required=True)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--adapter", choices=("none", "nianlun"), default="none")
    parser.add_argument("--max-context-chars", type=int, default=12_000)
    parser.add_argument("--max-context-items", type=int, default=50)
    return parser


async def run(args: argparse.Namespace) -> None:
    logger.info(
        "evaluation.cli.started judge_model=%s workers=%d adapter=%s",
        args.judge_model,
        args.workers,
        args.adapter,
    )
    logger.info(
        "evaluation.cli.paths input=%s output=%s excel_output=%s",
        args.input,
        args.output,
        args.excel_output or "none",
    )
    thinking_override = get_enable_thinking()
    if thinking_override is False:
        judge = build_llm(
            args.judge_model,
            temperature=0.0,
            enable_thinking=False,
        )
    else:
        judge = build_llm(args.judge_model, temperature=0.0)
    evaluator = RagEvaluator(
        judge=judge,
        config=EvaluationConfig(
            max_context_chars=args.max_context_chars,
            max_context_items=args.max_context_items,
        ),
    )
    adapter = adapt_nianlun_result if args.adapter == "nianlun" else None
    await evaluate_jsonl(
        evaluator,
        input_path=args.input,
        output_path=args.output,
        workers=args.workers,
        adapter=adapter,
    )
    if args.excel_output is not None:
        export_excel(
            input_path=args.input,
            results_path=args.output,
            output_path=args.excel_output,
            adapter=adapter,
        )
        logger.info("evaluation.excel.completed output=%s", args.excel_output)
    logger.info("evaluation.cli.completed output=%s", args.output)


def main() -> None:
    configure_evaluation_log_output()
    args = build_parser().parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
