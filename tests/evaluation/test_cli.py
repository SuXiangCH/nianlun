from __future__ import annotations

import argparse
import asyncio
import logging
from io import StringIO
from pathlib import Path
from typing import TextIO, cast

import pytest

import nianlun.evaluation.cli as cli


class FakeModel:
    async def generate_structured_output(self, **_kwargs: object) -> object:
        raise AssertionError("the test must not invoke the judge model")


def test_configure_evaluation_log_output_writes_to_stdout(capsys) -> None:
    logger = logging.getLogger("nianlun.evaluation")
    handlers = list(logger.handlers)
    level = logger.level
    propagate = logger.propagate
    try:
        logger.handlers.clear()
        cli.configure_evaluation_log_output()
        cli.configure_evaluation_log_output()

        logging.getLogger("nianlun.evaluation.batch").info("evaluation progress")

        captured = capsys.readouterr()
        assert captured.out == "evaluation progress\n"
        assert captured.err == ""
        assert (
            sum(
                getattr(handler, "_nianlun_cli_evaluation_handler", False)
                for handler in logger.handlers
            )
            == 1
        )
    finally:
        for handler in logger.handlers:
            if getattr(handler, "_nianlun_cli_evaluation_handler", False):
                cast(logging.StreamHandler[TextIO], handler).close()
        logger.handlers.clear()
        logger.handlers.extend(handlers)
        logger.setLevel(level)
        logger.propagate = propagate


def test_configure_evaluation_log_output_keeps_existing_handler(capsys) -> None:
    logger = logging.getLogger("nianlun.evaluation")
    handlers = list(logger.handlers)
    level = logger.level
    propagate = logger.propagate
    existing = logging.StreamHandler(StringIO())
    try:
        logger.handlers.clear()
        logger.addHandler(existing)

        cli.configure_evaluation_log_output()
        logger.info("evaluation progress")

        captured = capsys.readouterr()
        assert captured.out == "evaluation progress\n"
        assert (
            sum(
                getattr(handler, "_nianlun_cli_evaluation_handler", False)
                for handler in logger.handlers
            )
            == 1
        )
    finally:
        for handler in logger.handlers:
            if getattr(handler, "_nianlun_cli_evaluation_handler", False):
                cast(logging.StreamHandler[TextIO], handler).close()
        logger.handlers.clear()
        logger.handlers.extend(handlers)
        logger.setLevel(level)
        logger.propagate = propagate


@pytest.mark.parametrize(
    ("thinking_override", "expected_call"),
    [
        (None, {"model": "judge-model", "temperature": 0.0}),
        (True, {"model": "judge-model", "temperature": 0.0}),
        (
            False,
            {
                "model": "judge-model",
                "temperature": 0.0,
                "enable_thinking": False,
            },
        ),
    ],
)
def test_cli_only_passes_explicit_thinking_disable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    thinking_override: bool | None,
    expected_call: dict[str, object],
) -> None:
    build_calls: list[dict[str, object]] = []

    def fake_build_llm(model: str, **kwargs: object) -> FakeModel:
        build_calls.append({"model": model, **kwargs})
        return FakeModel()

    async def fake_evaluate_jsonl(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(cli, "get_enable_thinking", lambda: thinking_override)
    monkeypatch.setattr(cli, "build_llm", fake_build_llm)
    monkeypatch.setattr(cli, "evaluate_jsonl", fake_evaluate_jsonl)

    asyncio.run(
        cli.run(
            argparse.Namespace(
                input=tmp_path / "input.jsonl",
                output=tmp_path / "output.jsonl",
                excel_output=None,
                judge_model="judge-model",
                workers=1,
                adapter="none",
                max_context_chars=12_000,
                max_context_items=50,
            )
        )
    )

    assert build_calls == [expected_call]


def test_cli_exports_excel_when_requested(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    exported: list[dict[str, object]] = []

    def fake_build_llm(_model: str, **_kwargs: object) -> FakeModel:
        return FakeModel()

    async def fake_evaluate_jsonl(*_args: object, **_kwargs: object) -> None:
        return None

    def fake_export_excel(**kwargs: object) -> None:
        exported.append(kwargs)

    input_path = tmp_path / "input.jsonl"
    output_path = tmp_path / "output.jsonl"
    excel_path = tmp_path / "report.xlsx"
    monkeypatch.setattr(cli, "get_enable_thinking", lambda: None)
    monkeypatch.setattr(cli, "build_llm", fake_build_llm)
    monkeypatch.setattr(cli, "evaluate_jsonl", fake_evaluate_jsonl)
    monkeypatch.setattr(cli, "export_excel", fake_export_excel)

    asyncio.run(
        cli.run(
            argparse.Namespace(
                input=input_path,
                output=output_path,
                excel_output=excel_path,
                judge_model="judge-model",
                workers=1,
                adapter="nianlun",
                max_context_chars=12_000,
                max_context_items=50,
            )
        )
    )

    assert exported == [
        {
            "input_path": input_path,
            "results_path": output_path,
            "output_path": excel_path,
            "adapter": cli.adapt_nianlun_result,
        }
    ]
