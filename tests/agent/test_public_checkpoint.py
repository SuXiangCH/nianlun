from types import SimpleNamespace

import pytest

from nianlun.agent.lead_agent import factory


def test_public_v1_preserves_full_external_history_at_both_stages():
    from tests.agent.test_retrieval_answer import _runtime

    runtime, answer_model, retrieval = _runtime()
    messages = [{"role": "system", "content": "Use English"}]
    for index in range(17):
        messages.extend(
            [
                {"role": "user", "content": f"question-{index}"},
                {"role": "assistant", "content": f"answer-{index}"},
            ]
        )
    messages.append({"role": "user", "content": "What was the revenue?"})
    events = list(runtime.iter_public_events(messages, thread_id="external"))
    assert retrieval.inputs[0]["messages"][:-1] == messages[:-1]
    answer_input = answer_model.inputs[0]
    assert answer_input[1:-1] == messages[:-1]
    assert (
        "".join(
            event["data"]["delta"] for event in events if event["type"] == "message"
        )
        == events[-1]["data"]["answer"]
    )


def test_public_v0_hides_candidate_text_and_passes_history():
    from langchain_core.messages import AIMessage, AIMessageChunk
    from nianlun.agent.lead_agent.runtime import AgentRuntime
    from tests.agent.test_retrieval_answer import _KnowledgeBase

    captured = {}

    class Graph:
        def stream(self, values, **kwargs):
            captured.update(values)
            assert kwargs["context"]["input_messages"] == values["messages"]
            yield {
                "type": "messages",
                "data": (
                    AIMessageChunk(content="private candidate"),
                    {"langgraph_node": "model"},
                ),
            }
            yield {
                "type": "values",
                "data": {"messages": [AIMessage(content="final answer")]},
            }

    runtime = AgentRuntime(
        agent=Graph(),
        model="fake",
        effective_url="fake",
        tool_logging=False,
        kb=_KnowledgeBase(),
    )
    messages = [
        {"role": "user", "content": "previous"},
        {"role": "assistant", "content": "earlier"},
        {"role": "user", "content": "follow up"},
    ]
    events = list(runtime.iter_public_events(messages, thread_id="external-v0"))
    assert captured["messages"] == messages
    assert [
        event["data"]["delta"] for event in events if event["type"] == "message"
    ] == ["final answer"]


def test_v1_success_does_not_mark_callers_cancellation_event():
    import threading
    from tests.agent.test_retrieval_answer import _runtime

    runtime, _, _ = _runtime()
    cancelled = threading.Event()
    events = list(
        runtime.iter_events(
            "请检索营收。", thread_id="success", cancellation_event=cancelled
        )
    )
    assert events[-1]["type"] == "done"
    assert not cancelled.is_set()


@pytest.mark.parametrize("version", ["v0", "v1"])
def test_direct_greeting_has_a_checkpoint(version):
    from langgraph.graph import END, START, MessagesState, StateGraph
    from langgraph.checkpoint.memory import MemorySaver
    from nianlun.agent.lead_agent.runtime import AgentRuntime
    from tests.agent.test_retrieval_answer import _runtime, _KnowledgeBase

    if version == "v1":
        runtime, _, _ = _runtime()
    else:
        builder = StateGraph(MessagesState)
        builder.add_node("model", lambda state: {})
        builder.add_edge(START, "model")
        builder.add_edge("model", END)
        runtime = AgentRuntime(
            agent=builder.compile(checkpointer=MemorySaver()),
            model="fake",
            effective_url="fake",
            tool_logging=False,
            kb=_KnowledgeBase(),
        )
    assert list(runtime.iter_events("你好", thread_id="greeting"))[-1]["type"] == "done"
    assert runtime.agent.get_state({"configurable": {"thread_id": "greeting"}}).values


def test_v0_attaches_request_cancellation_middleware(monkeypatch):
    from nianlun.agent.middleware import RequestCancellationMiddleware

    captured = {}
    kb = SimpleNamespace(has_fts=True, has_vector=False)
    monkeypatch.setattr(factory, "build_chat_model", lambda **kw: object())
    monkeypatch.setattr(factory, "build_tools", lambda **kw: [])
    monkeypatch.setattr(factory, "build_system_prompt", lambda kb: "prompt")
    monkeypatch.setattr(factory, "estimate_agent_context_overhead", lambda *a: 0)
    monkeypatch.setattr(
        factory,
        "build_agent",
        lambda *a, **kw: captured.setdefault("middleware", kw["middleware"]),
    )
    factory.AgentRuntimeFactory(
        knowledge_base=kb,
        model="m",
        api_key="fake",
        allow_env_fallback=False,
        agent_version="v0",
    ).create()
    assert any(
        isinstance(middleware, RequestCancellationMiddleware)
        for middleware in captured["middleware"]
    )


def test_v0_run_stops_when_cancellation_event_is_set(monkeypatch):
    import threading

    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.messages import AIMessage
    from langgraph.checkpoint.memory import MemorySaver

    from nianlun.agent.lead_agent import runner as runner_module
    from nianlun.agent.lead_agent.agent import build_agent
    from nianlun.agent.lead_agent.runtime import AgentRuntime
    from nianlun.agent.middleware import (
        AgentRequestCancelled,
        RequestCancellationMiddleware,
    )
    from tests.agent.test_retrieval_answer import _KnowledgeBase

    # Skip the LLM router so the run reaches the graph (and its middlewares).
    monkeypatch.setattr(
        runner_module,
        "_route_with_checkpoint",
        lambda *_args, **_kwargs: {
            "route": "agent",
            "route_source": "test",
            "route_reason": "test",
        },
    )
    model = GenericFakeChatModel(messages=iter([AIMessage(content="should not run")]))
    agent = build_agent(
        model,
        tools=[],
        system_prompt="test",
        context_schema=dict,
        checkpointer=MemorySaver(),
        middleware=[RequestCancellationMiddleware()],
    )
    runtime = AgentRuntime(
        agent=agent,
        model="fake",
        effective_url="fake",
        tool_logging=False,
        kb=_KnowledgeBase(),
        agent_version="v0",
    )
    cancelled = threading.Event()
    cancelled.set()
    with pytest.raises(AgentRequestCancelled):
        list(
            runtime.iter_events(
                "q", thread_id="cancel-v0", cancellation_event=cancelled
            )
        )


@pytest.mark.parametrize("version", ["v0", "v1"])
def test_release_parameters_and_checkpoint_are_injected(monkeypatch, version):
    calls = {}
    checkpoint = object()
    kb = SimpleNamespace(has_fts=True, has_vector=False)
    monkeypatch.setattr(
        factory,
        "build_chat_model",
        lambda **kw: calls.setdefault("model", kw) or object(),
    )
    monkeypatch.setattr(factory, "build_tools", lambda **kw: [])
    monkeypatch.setattr(factory, "build_system_prompt", lambda kb: "prompt")
    monkeypatch.setattr(factory, "build_retrieval_system_prompt", lambda kb: "prompt")
    monkeypatch.setattr(factory, "estimate_agent_context_overhead", lambda *a: 0)
    monkeypatch.setattr(
        factory, "build_agent", lambda *a, **kw: calls.setdefault("inner", kw)
    )
    monkeypatch.setattr(
        factory,
        "build_retrieval_answer_graph",
        lambda **kw: calls.setdefault("outer", kw),
    )
    monkeypatch.setattr(
        factory,
        "get_openai_temperature",
        lambda: pytest.fail("environment temperature read"),
    )
    monkeypatch.setattr(
        factory, "get_enable_thinking", lambda: pytest.fail("environment thinking read")
    )
    factory.AgentRuntimeFactory(
        knowledge_base=kb,
        model="m",
        api_key="fake",
        allow_env_fallback=False,
        agent_version=version,
        temperature=0.2,
        enable_thinking=None,
        checkpointer=checkpoint,
    ).create()
    assert calls["model"]["temperature"] == 0.2
    assert calls["model"]["enable_thinking"] is None
    if version == "v0":
        assert calls["inner"]["checkpointer"] is checkpoint
    else:
        assert calls["inner"]["checkpointer"] is None
        assert calls["outer"]["checkpointer"] is checkpoint
