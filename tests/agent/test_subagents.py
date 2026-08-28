from __future__ import annotations

import json
import threading

from langchain_core.messages import AIMessage

from nianlun.agent.subagents.config import DeepSearchConfig
from nianlun.agent.subagents.executor import DeepSearchRunner
from nianlun.agent.subagents.prompt import build_deep_search_system_prompt
from nianlun.agent.subagents.result import (
    DeepSearchResult,
    Evidence,
    result_from_agent_output,
)
from nianlun.agent.subagents.tools import build_deep_search_tools


class RecordingAgent:
    def __init__(self, output, *, entered=None, release=None):
        self.output = output
        self.entered = entered
        self.release = release
        self.calls = []

    def invoke(self, payload, **kwargs):
        self.calls.append((payload, kwargs))
        if self.entered is not None:
            self.entered.set()
        if self.release is not None:
            self.release.wait(timeout=2)
        return self.output


def test_runner_is_lazy_and_does_not_pass_parent_thread_to_child():
    created = []
    agent = RecordingAgent(
        {
            "answer": "研究结论",
            "evidence": [{"doc_id": "doc-1", "text": "正文"}],
        }
    )

    def factory():
        created.append(True)
        return agent

    runner = DeepSearchRunner(factory)
    assert runner.agent_created is False
    execution = runner.run(
        "研究任务",
        request_id="run-1",
        context={
            "thread_id": "parent-thread",
            "configurable": {"thread_id": "parent-thread"},
        },
    )

    assert runner.agent_created is True
    assert created == [True]
    assert execution.subagent_run_id != "run-1"
    assert execution.parent_request_id == "run-1"
    assert execution.result.status == "completed"
    payload, kwargs = agent.calls[0]
    assert payload["messages"][1].content == "研究任务"
    assert kwargs["config"]["recursion_limit"] == 10
    assert kwargs["config"]["timeout"] > 0
    assert "thread_id" not in kwargs["config"]
    assert "thread_id" not in kwargs["context"]
    assert "configurable" not in kwargs["context"]
    assert kwargs["context"]["subagent_run_id"] == execution.subagent_run_id


def test_runner_reuses_the_stateless_child_agent_graph():
    created = []
    agent = RecordingAgent({"answer": "ok"})

    def factory():
        created.append(True)
        return agent

    runner = DeepSearchRunner(factory)
    runner.run("one")
    runner.run("two")

    assert created == [True]
    assert len(agent.calls) == 2


def test_runner_context_factory_supplies_isolated_runtime_dependencies():
    agent = RecordingAgent({"answer": "ok"})
    contexts = []
    runner = DeepSearchRunner(
        lambda: agent,
        context_factory=lambda: {
            "knowledge_base": "kb",
            "retrieval_collector": object(),
        },
    )

    runner.run("task", context={"request_value": "one"})
    contexts.append(agent.calls[-1][1]["context"])

    assert contexts[0]["knowledge_base"] == "kb"
    assert contexts[0]["request_value"] == "one"
    assert contexts[0]["subagent_run_id"]


def test_deep_search_prompt_uses_outline_only_for_evidence_gaps():
    prompt = build_deep_search_system_prompt()

    assert "直接读取最相关节点的正文" in prompt
    assert "正文证据不足" in prompt
    assert "再使用 get_structure_outline" in prompt
    assert "只对存在证据缺口的文档读取目录" in prompt


def test_context_factory_failure_releases_the_concurrency_slot():
    runner = DeepSearchRunner(
        lambda: RecordingAgent({"answer": "ok"}),
        config=DeepSearchConfig(max_concurrent=1),
        context_factory=lambda: (_ for _ in ()).throw(RuntimeError("bad context")),
    )

    failed = runner.run("task")
    assert failed.result.error_code == "context_error"

    runner._context_factory = None
    recovered = runner.run("task")
    assert recovered.result.status == "completed"


def test_runner_returns_structured_errors_for_invalid_and_busy_tasks():
    runner = DeepSearchRunner(lambda: RecordingAgent({"answer": "unused"}))
    invalid = runner.run(" ")
    assert invalid.result.to_dict() == {
        "status": "failed",
        "error": {
            "code": "invalid_task",
            "message": "Deep-search task must be a non-empty string.",
        },
    }

    entered = threading.Event()
    release = threading.Event()
    runner = DeepSearchRunner(
        lambda: RecordingAgent({"answer": "slow"}, entered=entered, release=release),
        config=DeepSearchConfig(max_concurrent=1, timeout_seconds=2),
    )
    first_result = []
    thread = threading.Thread(target=lambda: first_result.append(runner.run("first")))
    thread.start()
    assert entered.wait(timeout=1)

    busy = runner.run("second")
    assert busy.result.error_code == "busy"
    release.set()
    thread.join(timeout=2)
    assert first_result[0].result.status == "completed"


def test_runner_timeout_keeps_child_slot_until_worker_finishes():
    entered = threading.Event()
    release = threading.Event()
    runner = DeepSearchRunner(
        lambda: RecordingAgent({"answer": "late"}, entered=entered, release=release),
        config=DeepSearchConfig(max_concurrent=1, timeout_seconds=0.03),
    )

    timed_out = runner.run("slow")
    assert timed_out.result.error_code == "timeout"
    assert timed_out.status_events[-1]["event"] == "deep_search_failed"

    still_busy = runner.run("second")
    assert still_busy.result.error_code == "busy"
    release.set()


def test_result_protocol_is_bounded_and_keeps_source_fields():
    config = DeepSearchConfig(
        max_result_chars=8_000,
        max_answer_chars=1_000,
        max_evidence_items=16,
        max_evidence_text_chars=600,
    )
    result = DeepSearchResult(
        status="completed",
        answer="a" * 5_000,
        evidence=tuple(
            Evidence(
                doc_id=f"doc-{index}",
                node_id="node-1",
                line_num=12,
                char_offset=4_000,
                char_limit=4_000,
                total_chars=12_000,
                text_truncated=True,
                text="正文" * 1_000,
            )
            for index in range(20)
        ),
        open_questions=("q" * 500,) * 10,
        search_summary="s" * 2_000,
    )

    payload = result.to_dict(config)
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    assert len(encoded) <= config.max_result_chars
    assert len(payload["answer"]) <= config.max_answer_chars
    assert len(payload["evidence"]) <= config.max_evidence_items
    assert all(
        len(item["text"]) <= config.max_evidence_text_chars
        for item in payload["evidence"]
    )
    assert payload["evidence"][0]["doc_id"] == "doc-0"
    assert payload["evidence"][0]["text_truncated"] is True


def test_result_parser_accepts_structured_and_langgraph_message_outputs():
    structured = result_from_agent_output(
        {"answer": "结论", "evidence": [{"doc_id": "doc-1", "text": "证据"}]}
    )
    assert structured.answer == "结论"
    assert structured.evidence[0].doc_id == "doc-1"

    message_result = result_from_agent_output({"messages": [AIMessage(content="回答")]})
    assert message_result.status == "completed"
    assert message_result.answer == "回答"

    json_result = result_from_agent_output(
        json.dumps(
            {
                "answer": "JSON 结论",
                "evidence": [{"doc_id": "doc-2", "text": "JSON 证据"}],
            },
            ensure_ascii=False,
        )
    )
    assert json_result.answer == "JSON 结论"
    assert json_result.evidence[0].doc_id == "doc-2"


def test_execution_serialization_uses_runner_limits_and_unique_child_ids():
    config = DeepSearchConfig(max_result_chars=500, max_answer_chars=200)
    runner = DeepSearchRunner(
        lambda: RecordingAgent({"answer": "a" * 2_000}),
        config=config,
    )

    first = runner.run("task", parent_request_id="parent-1")
    second = runner.run("task", parent_request_id="parent-1")

    assert first.subagent_run_id != second.subagent_run_id
    assert first.parent_request_id == second.parent_request_id == "parent-1"
    encoded = json.dumps(first.to_dict(), ensure_ascii=False, separators=(",", ":"))
    assert len(encoded) <= config.max_result_chars
    assert len(first.to_dict()["answer"]) <= config.max_answer_chars


def test_deep_search_tools_are_read_only_and_allowlisted():
    names = [tool.name for tool in build_deep_search_tools(include_vector=True)]
    assert names == [
        "search_document_nodes",
        "find_semantic_documents",
        "get_structure_outline",
        "get_line_content",
    ]
    assert "deep_search" not in names
    assert "get_document" not in names


def test_runner_can_be_cancelled_before_child_creation():
    created = []
    cancel_event = threading.Event()
    cancel_event.set()
    runner = DeepSearchRunner(lambda: created.append(True))

    execution = runner.run("cancelled", cancel_event=cancel_event)

    assert execution.result.error_code == "cancelled"
    assert created == []
    assert runner.agent_created is False
