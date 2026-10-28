from __future__ import annotations

import json
from typing import Any

import pytest

from nianlun.agent.lead_agent.factory import AgentRuntimeFactory
from nianlun.agent.prompt_registry import CURRENT_PROMPT_BUNDLE, LEGACY_PROMPT_BUNDLE


class KnowledgeBase:
    has_fts = True
    has_vector = False


@pytest.mark.parametrize("version", ["v0", "v1"])
@pytest.mark.parametrize("enabled", [False, True])
def test_runtime_assembles_the_resolved_bundle_and_isolates_requests(
    monkeypatch: pytest.MonkeyPatch, version: str, enabled: bool
) -> None:
    import nianlun.agent.lead_agent.factory as factory

    assembled: dict[str, Any] = {}
    outer: dict[str, Any] = {}
    monkeypatch.setattr(factory, "build_chat_model", lambda **_: object())
    monkeypatch.setattr(
        factory, "build_agent", lambda *_, **kw: assembled.update(kw) or "inner"
    )
    monkeypatch.setattr(
        factory,
        "build_retrieval_answer_graph",
        lambda **kw: outer.update(kw) or "outer",
    )
    runtime = AgentRuntimeFactory(
        knowledge_base=KnowledgeBase(),
        model="fake",
        api_key="fake",
        allow_env_fallback=False,
        agent_version=version,
        prompt_bundle=CURRENT_PROMPT_BUNDLE,
        image_capability=enabled,
    ).create()
    names = [tool.name for tool in assembled["tools"]]
    middleware = [type(item).__name__ for item in assembled["middleware"]]
    assert ("select_answer_images" in names) is enabled
    assert ("AnswerImageSelectionMiddleware" in middleware) is enabled
    if enabled:
        assert (
            middleware.index("RetrievalLoopGuardMiddleware")
            < middleware.index("AnswerImageSelectionMiddleware")
            < middleware.index("RetrievalDeduplicationMiddleware")
        )
    expected = (
        CURRENT_PROMPT_BUNDLE.build_system_prompt
        if version == "v0"
        else CURRENT_PROMPT_BUNDLE.build_retrieval_system_prompt
    )(KnowledgeBase(), image_capability=enabled)
    assert assembled["system_prompt"] == expected
    if version == "v1":
        assert outer["prompt_bundle"] is CURRENT_PROMPT_BUNDLE
        assert outer["image_capability"] is enabled
    collector, _ = runtime.new_request_context()
    other, _ = runtime.new_request_context()
    assert collector.tool_schema_version == 5
    assert collector.image_context.enabled is enabled
    assert other.image_context is not collector.image_context


def test_legacy_bundle_refuses_image_capability_before_building_model() -> None:
    with pytest.raises(ValueError, match="v4"):
        AgentRuntimeFactory(
            prompt_bundle=LEGACY_PROMPT_BUNDLE,
            image_capability=True,
        )


def test_v5_answer_input_projects_private_source_before_budgeting() -> None:
    from nianlun.agent.retrieval_answer.input_messages import build_answer_input

    snippet = {
        "citation_id": 1,
        "doc_id": "doc",
        "doc_name": "Document",
        "line_spec": "1",
        "node_id": "node",
        "title": "Title",
        "line_num": 1,
        "text": "![title](private/secret.png)",
        "model_text": "![title](nianlun-image:img_01)",
        "char_offset": 0,
        "char_limit": 100,
        "total_chars": 27,
        "text_truncated": False,
        "images": [{"asset_relpath": "private/secret.png", "sha256": "a" * 64}],
        "model_images": [
            {
                "ref_id": "img_01",
                "caption": "title",
                "width": 1,
                "height": 1,
                "status": "available",
                "citation_ids": [1],
            }
        ],
    }
    messages, selected, omitted = build_answer_input(
        "Show image",
        [snippet],
        context_window_tokens=16000,
        conversation_context=[],
        input_messages=None,
        prompt_bundle=CURRENT_PROMPT_BUNDLE,
        image_capability=True,
    )
    serialized = json.dumps(messages)
    assert "private/secret.png" not in serialized
    assert "sha256" not in serialized
    assert "img_01" in serialized
    assert selected == [1]
    assert omitted == []


def test_v5_text_only_answer_masks_image_directory() -> None:
    from nianlun.agent.retrieval_answer.input_messages import build_answer_input

    snippet = {
        "citation_id": 1,
        "text": "secret/path",
        "model_text": "[图片引用不可用]",
        "model_images": [],
        "images": [{"sha256": "a" * 64}],
    }
    messages, _, _ = build_answer_input(
        "Explain",
        [snippet],
        context_window_tokens=16000,
        conversation_context=[],
        input_messages=None,
        prompt_bundle=CURRENT_PROMPT_BUNDLE,
        image_capability=False,
    )
    assert "secret/path" not in json.dumps(messages)
    assert "sha256" not in json.dumps(messages)


@pytest.mark.parametrize("history_mode", ["none", "conversation", "public", "trusted"])
@pytest.mark.parametrize("enabled", [False, True])
def test_answer_image_budget_preserves_a_single_initial_system_message(
    history_mode: str, enabled: bool
) -> None:
    from nianlun.agent.contracts import AnswerImagePolicy, RetrievedSnippet
    from nianlun.agent.image_history import HistoryTurn, TrustedHistory
    from nianlun.agent.retrieval_answer.input_messages import build_answer_input
    from nianlun.agent.prompts.v5.retrieval_answer import ANSWER_OUTPUT_RESERVE_TOKENS
    from nianlun.agent.token_estimation import estimate_tokens

    history = [
        {"role": "user", "content": "Previous question"},
        {"role": "assistant", "content": "Previous answer"},
    ]
    question = "Explain the evidence"
    snippet: RetrievedSnippet = {
        "citation_id": 1,
        "doc_id": "doc",
        "doc_name": "Document",
        "line_spec": "1",
        "node_id": "node",
        "title": "Title",
        "line_num": 1,
        "text": "Evidence to preserve",
        "char_offset": 0,
        "char_limit": 20,
        "total_chars": 20,
        "text_truncated": False,
    }
    kwargs = {
        "context_window_tokens": 16000,
        "conversation_context": history if history_mode == "conversation" else [],
        "input_messages": [*history, {"role": "user", "content": question}]
        if history_mode == "public"
        else None,
        "trusted_history": TrustedHistory(
            turns=(
                HistoryTurn(
                    message_id="previous",
                    question=history[0]["content"],
                    answer=history[1]["content"],
                ),
            )
        )
        if history_mode == "trusted"
        else None,
        "prompt_bundle": CURRENT_PROMPT_BUNDLE,
        "image_capability": enabled,
    }
    original, selected, omitted = build_answer_input(question, [snippet], **kwargs)
    policy = AnswerImagePolicy(max_answer_images=3)
    messages, actual_selected, actual_omitted = build_answer_input(
        question, [snippet], image_policy=policy, **kwargs
    )

    system_positions = [
        i for i, message in enumerate(messages) if message["role"] == "system"
    ]
    assert system_positions == [0]
    assert messages[1:] == original[1:]
    assert (actual_selected, actual_omitted) == (selected, omitted) == ([1], [])
    if enabled:
        assert messages[0]["content"] == (
            original[0]["content"] + "\n\n" + policy.model_message()["content"]
        )
    else:
        assert messages == original
    assert estimate_tokens(messages) + ANSWER_OUTPUT_RESERVE_TOKENS <= 16000
    repeated, _, _ = build_answer_input(question, [snippet], **kwargs)
    assert repeated == original


@pytest.mark.parametrize("agent_version", ["v0", "v1"])
@pytest.mark.parametrize("stream", [False, True])
def test_explicit_pure_images_suppresses_draft_and_skips_answer_model(
    agent_version, stream
):
    from langchain_core.messages import AIMessage
    from nianlun.agent.lead_agent.runtime import AgentRuntime
    from nianlun.agent.retrieval_answer.graph import build_retrieval_answer_graph

    class Retrieval:
        def invoke(self, _, *, context, **kwargs):
            collector = context["retrieval_collector"]
            collector.add_line_content_result(
                json.dumps(
                    {
                        "doc_id": "doc",
                        "doc_name": "Doc",
                        "line_spec": "1",
                        "content": [
                            {
                                "node_id": "node",
                                "line_num": 1,
                                "text": "![title](secret.png)",
                                "images": [
                                    {
                                        "image_id": "img_" + "a" * 24,
                                        "generation": 1,
                                        "knowledge_base_id": "kb",
                                        "content_version": 1,
                                        "asset_relpath": "secret.png",
                                        "sha256": "a" * 64,
                                        "size_bytes": 10,
                                        "media_type": "image/png",
                                        "width": 1,
                                        "height": 1,
                                        "alt_text": "title",
                                        "source_start": 0,
                                        "source_end": 20,
                                    }
                                ],
                                "image_spans": [
                                    {
                                        "source_start": 0,
                                        "source_end": 20,
                                        "kind": "markdown",
                                    }
                                ],
                            }
                        ],
                    }
                )
            )
            state = collector.image_context
            state.freeze_tool_batch([{"name": "select_answer_images", "id": "select"}])
            state.select_answer_images("select", ["img_01"])
            return {"messages": [AIMessage(content="candidate must not become answer")]}

        def stream(self, value, **kwargs):
            result = self.invoke(value, **kwargs)
            yield {
                "type": "messages",
                "data": (result["messages"][-1], {"langgraph_node": "model"}),
            }
            yield {"type": "values", "data": result}

    class Answer:
        def invoke(self, *_):
            pytest.fail("Pure-image results must not call the answer model")

    graph = Retrieval()
    if agent_version == "v1":
        graph = build_retrieval_answer_graph(
            retrieval_agent=graph,
            answer_model=Answer(),
            recursion_limit=24,
            context_window_tokens=16000,
            prompt_bundle=CURRENT_PROMPT_BUNDLE,
            image_capability=True,
        )
    runtime = AgentRuntime(
        agent=graph,
        model="fake",
        effective_url="https://fake.test",
        tool_logging=False,
        kb=KnowledgeBase(),
        agent_version=agent_version,
        tool_schema_version=5,
        image_capability=True,
    )
    if stream:
        events = list(runtime.iter_events("Show the original diagram"))
        assert not [event for event in events if event["type"] == "message"]
        result = events[-1]["data"]
    else:
        result = runtime.invoke("Show the original diagram")
    assert result["answer"] == ""
    assert result["answer_output_mode"] == "images"
    assert result["answer_input_citation_ids"] == []
    assert result["display_image_citation_ids"] == [1]
    assert [image["ref_id"] for image in result["selected_answer_images"]] == ["img_01"]


@pytest.mark.parametrize("image_capability", [False, True])
@pytest.mark.parametrize("include_image", [False, True])
def test_v0_streams_text_before_generation_completes(
    image_capability: bool, include_image: bool
) -> None:
    from langchain_core.messages import AIMessage, AIMessageChunk
    from nianlun.agent.lead_agent.runtime import AgentRuntime

    parts = ["Here is", " the evidence."]
    if include_image:
        parts.extend(["\n\n![Diagram](nianlun-image:", "img_01)"])
    completed = False

    class Graph:
        def stream(self, *args, **kwargs):
            nonlocal completed
            for part in parts:
                yield {
                    "type": "messages",
                    "data": (
                        AIMessageChunk(content=part),
                        {"langgraph_node": "model"},
                    ),
                }
            completed = True
            yield {
                "type": "values",
                "data": {"messages": [AIMessage(content="".join(parts))]},
            }

    runtime = AgentRuntime(
        agent=Graph(),
        model="fake",
        effective_url="https://fake.test",
        tool_logging=False,
        kb=KnowledgeBase(),
        agent_version="v0",
        tool_schema_version=5,
        image_capability=image_capability,
    )
    events = runtime.iter_events("Explain the evidence")
    try:
        for part in parts:
            assert next(events) == {
                "type": "message",
                "data": {"delta": part, "phase": "candidate", "round": 1},
            }
            assert not completed
        remaining = list(events)
    finally:
        events.close()
    assert remaining[-1]["type"] == "done"
    assert remaining[-1]["data"]["answer"] == "".join(parts)
