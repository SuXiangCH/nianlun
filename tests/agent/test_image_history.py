import json

import pytest
from pydantic import ValidationError

from nianlun.agent.image_history import (
    HistoryImage,
    HistoryImageSource,
    HistoryTurn,
    TrustedHistory,
    fit_history,
    scrub_image_targets,
    scrub_messages,
)
from nianlun.agent.token_estimation import estimate_tokens


def source(index=1):
    return HistoryImageSource(
        knowledge_base_id="kb",
        doc_id=f"doc-{index}",
        generation=1,
        content_version=2,
        image_id=f"image-{index}",
        node_id="node",
        char_offset=0,
        char_limit=100,
    )


def turn(index, *, caption="Diagram", answer="", images=True):
    return HistoryTurn(
        message_id=f"message-{index}",
        question=f"question-{index}",
        answer=answer,
        answer_output_mode="images" if images else "text",
        images=(
            HistoryImage(
                order=2, caption=caption, status="available", sources=(source(index),)
            ),
        )
        if images
        else (),
        image_count=3 if images else 0,
        images_truncated=images,
    )


def test_projection_is_strict_and_has_no_private_storage_fields():
    with pytest.raises(ValidationError):
        HistoryImageSource(**source().model_dump(), blob_id="private")
    with pytest.raises(ValidationError):
        HistoryImage(order=1, caption="x", status="available", sources=(source(),) * 5)
    with pytest.raises(ValidationError):
        HistoryImage(order=1, caption="x" * 257, status="available", sources=())


def test_fit_retains_last_three_complete_turns_and_original_image_orders():
    history = fit_history(TrustedHistory(turns=tuple(turn(i) for i in range(5))))
    assert [item.message_id for item in history.turns] == [
        "message-2",
        "message-3",
        "message-4",
    ]
    assert history.turns[0].images[0].order == 2
    assert history.turns[0].image_count == 3
    messages = history.messages()
    assert [message["role"] for message in messages] == ["user", "assistant"] * 3
    assert "image-4" in messages[-1]["content"]
    assert "blob" not in json.dumps(messages)


def test_fit_drops_whole_turns_for_image_and_total_budgets():
    history = TrustedHistory(turns=tuple(turn(i, caption="图" * 256) for i in range(4)))
    result = fit_history(history)
    assert estimate_tokens(result.image_messages()) <= 1024
    assert all(
        item.question.endswith(item.message_id.split("-")[-1]) for item in result.turns
    )
    result = fit_history(history, token_budget=80)
    assert result.turns == ()


def test_latest_large_image_turn_keeps_bounded_prefix_and_total_count():
    image_turn = turn(1).model_copy(
        update={
            "images": tuple(
                HistoryImage(
                    order=i, caption="Diagram", status="available", sources=(source(i),)
                )
                for i in range(1, 21)
            ),
            "image_count": 20,
            "images_truncated": False,
        }
    )
    history = fit_history(TrustedHistory(turns=(image_turn,)))
    assert len(history.turns) == 1
    projected = history.turns[0]
    assert 0 < len(projected.images) < 20
    assert projected.image_count == 20 and projected.images_truncated
    assert [image.order for image in projected.images] == list(
        range(1, len(projected.images) + 1)
    )
    assert estimate_tokens(history.image_messages()) <= 1024


@pytest.mark.parametrize(
    "target",
    [
        "https://private.test/image?token=secret",
        "data:image/png;base64," + "A" * 30000,
        "data:image/svg+xml;base64," + "B" * 30000,
        "javascript:secret",
    ],
    ids=["https", "png-data", "svg-data", "javascript"],
)
def test_scrub_removes_markdown_targets_before_budgeting(target):
    original = [{"role": "assistant", "content": f"Before ![diagram]({target}) after"}]
    clean = scrub_messages(original)
    assert target not in clean[0]["content"]
    assert "diagram" in clean[0]["content"]
    assert "Before" in clean[0]["content"] and "after" in clean[0]["content"]
    assert original[0]["content"].endswith(") after")
    assert estimate_tokens(clean) < 100


def test_scrub_reference_images_and_html_without_fetching():
    clean = scrub_image_targets(
        '![caption][pic]\n\n[pic]: https://secret/x\n\n<img src="data:image/png;base64,AAAA" alt="other">'
    )
    assert "https://secret" not in clean and "base64" not in clean
    assert "caption" in clean and "other" in clean


@pytest.mark.parametrize(
    "hidden_image",
    [
        "![caption](data:image/png;base64,SEVDUkVU)",
        "![caption][pic]\n\n[pic]: https://private.test/inner?token=secret",
        '<img src="https://private.test/middle">\n![caption](https://private.test/inner)',
    ],
)
def test_history_scrub_removes_images_exposed_by_html_replacement(hidden_image):
    content = '<img src="https://private.test/outer">\n' + hidden_image
    history = TrustedHistory(
        turns=(HistoryTurn(message_id="previous", question=content, answer=content),)
    )
    for message in history.messages():
        clean = message["content"]
        assert "private.test" not in clean
        assert "base64" not in clean
        assert "[Historical image: caption]" in clean
        assert scrub_image_targets(clean) == clean
    from nianlun.agent.retrieval_answer.input_messages import (
        build_answer_input,
        build_retrieval_input,
    )

    kwargs = dict(
        context_window_tokens=8192,
        conversation_context=[],
        input_messages=None,
        trusted_history=history,
    )
    retrieval = build_retrieval_input("Continue", **kwargs)
    answer, _, _ = build_answer_input("Continue", [], **kwargs)
    for messages in (retrieval, answer):
        assert all("private.test" not in message["content"] for message in messages)
        assert all("base64" not in message["content"] for message in messages)


@pytest.mark.parametrize(
    "content",
    [
        'What URL does [Nianlun][project] point to?\n\n[project]: https://example.com/nianlun "Nianlun project"\n',
        '[project][] and [project]\n\n[project]: <https://example.com/a%20b>\n  "Project title"\n',
        '> [Nianlun][project]\r\n>\r\n> [project]: https://example.com/nianlun "Project"\r\n',
        "`![code][project]`\n\n$![math][project]$\n\n$$\n![display][project]\n$$\n\n[project]: https://example.com/nianlun\n",
        '[unused]: https://example.com/unused "Unused definition"\n',
    ],
    ids=[
        "ordinary-link",
        "collapsed-shortcut-multiline",
        "quote-crlf",
        "code-math",
        "unused",
    ],
)
def test_scrub_preserves_text_only_reference_markdown(content):
    assert scrub_image_targets(content) == content


def test_scrub_keeps_shared_reference_link_url_and_title():
    content = (
        "[Nianlun][shared]\n\n![diagram][shared]\n\n"
        '[shared]: https://example.com/nianlun "Project title"\n'
    )
    clean = scrub_image_targets(content)
    assert clean == content.replace("![diagram][shared]", "[Historical image: diagram]")
    assert scrub_image_targets(clean) == clean


def test_scrub_removes_only_image_definitions_in_nested_blocks_and_duplicate_labels():
    content = (
        "> [ordinary][project]\r\n>\r\n"
        "> ![diagram][pic]\r\n>\r\n"
        '> [project]: https://example.com/project "Project"\r\n'
        '> [pic]: https://private.test/first "Secret"\r\n'
        "> [PIC]: https://private.test/duplicate\r\n\r\n"
        "[unused]: https://example.com/unused\r\n"
    )
    clean = scrub_image_targets(content)
    assert "private.test" not in clean and "Secret" not in clean
    assert '> [project]: https://example.com/project "Project"\r\n' in clean
    assert "[unused]: https://example.com/unused\r\n" in clean
    assert "[Historical image: diagram]" in clean


def test_scrub_retains_first_shared_definition_without_promoting_duplicate():
    content = (
        "[ordinary][shared]\n\n![diagram][shared]\n\n"
        '[shared]: https://example.com/first "First title"\n'
        '[SHARED]: https://example.com/duplicate "Duplicate title"\n'
    )
    assert scrub_image_targets(content) == content.replace(
        "![diagram][shared]", "[Historical image: diagram]"
    )


def test_scrub_preserves_reference_links_in_current_and_historical_messages():
    content = (
        'See [Nianlun][project].\n\n[project]: https://example.com/nianlun "Project"\n'
    )
    public = [{"role": "user", "content": content}]
    assert scrub_messages(public) == public
    history = TrustedHistory(
        turns=(HistoryTurn(message_id="text", question=content, answer=content),)
    )
    assert [message["content"] for message in history.messages()] == [content, content]


def test_scrub_does_not_classify_code_or_math_as_reference_images():
    content = (
        "`![code][literal]` $![math][literal]$\n\n"
        "![diagram][pic]\n\n"
        '[literal]: https://example.com/literal "Literal title"\n'
        "[pic]: https://private.test/image\n"
    )
    clean = scrub_image_targets(content)
    assert "`![code][literal]` $![math][literal]$" in clean
    assert '[literal]: https://example.com/literal "Literal title"\n' in clean
    assert "private.test" not in clean


def test_scrub_removes_reference_targets_nested_in_image_caption():
    clean = scrub_image_targets(
        "![![inner][pic]](https://private.test/outer)\n\n"
        "[pic]: https://private.test/inner\n"
    )
    assert "private.test" not in clean
    assert "inner" in clean


def test_scrub_maps_mixed_html_and_duplicate_definitions_with_crlf():
    content = (
        '<div><img src="https://private.test/html-block" alt="block"></div>\r\n\r\n'
        "> [ordinary][shared]\r\n>\r\n"
        '> ![diagram][pic] and <img src="https://private.test/html-inline" alt="inline">\r\n>\r\n'
        '> [shared]: https://example.com/first "First title"\r\n'
        '> [SHARED]: https://example.com/duplicate "Duplicate title"\r\n'
        "> [pic]: https://private.test/first-image\r\n"
        "> [PIC]: https://private.test/duplicate-image\r\n\r\n"
        "[unused]: https://example.com/unused\r\n"
    )
    clean = scrub_image_targets(content)
    assert "private.test" not in clean
    assert '> [shared]: https://example.com/first "First title"\r\n' in clean
    assert '> [SHARED]: https://example.com/duplicate "Duplicate title"\r\n' in clean
    assert "[unused]: https://example.com/unused\r\n" in clean
    assert clean.count("[Historical image:") == 3
    assert scrub_image_targets(clean) == clean


@pytest.mark.parametrize("version", ["v0", "v1"])
@pytest.mark.parametrize("public", [False, True], ids=["trusted-history", "public"])
def test_runtime_preserves_reference_links_without_authorizing_images(version, public):
    from langchain_core.messages import AIMessage
    from nianlun.agent.lead_agent.runtime import AgentRuntime
    from tests.agent.test_retrieval_answer import _KnowledgeBase, _runtime

    captured = {}

    class Graph:
        def stream(self, values, **kwargs):
            captured.update(values)
            collector = kwargs["context"]["retrieval_collector"]
            assert not collector.image_context.candidates
            yield {"type": "values", "data": {"messages": [AIMessage(content="Done")]}}

    if version == "v1":
        runtime, _, retrieval = _runtime()
    else:
        runtime = AgentRuntime(
            agent=Graph(),
            model="fake",
            effective_url="fake",
            tool_logging=False,
            kb=_KnowledgeBase(),
        )
    content = (
        "[Nianlun][shared]\n\n![diagram][shared]\n\n![private][pic]\n\n"
        '[shared]: https://example.com/nianlun "Project title"\n'
        "[pic]: https://private.test/image\n"
    )
    if public:
        list(
            runtime.iter_public_events(
                [{"role": "user", "content": content}], thread_id="reference-links"
            )
        )
    else:
        history = TrustedHistory(
            turns=(
                HistoryTurn(
                    message_id="links",
                    question=content,
                    answer=content,
                ),
            )
        )
        runtime.invoke(
            "Explain those links", thread_id="reference-links", trusted_history=history
        )
    messages = (
        retrieval.inputs[0]["messages"] if version == "v1" else captured["messages"]
    )
    text = json.dumps(messages)
    assert "https://example.com/nianlun" in text and "Project title" in text
    assert "[Nianlun][shared]" in text
    assert "private.test" not in text and "![diagram]" not in text


def test_v1_inputs_receive_pure_image_history_without_changing_query():
    from nianlun.agent.retrieval_answer.input_messages import (
        build_retrieval_input,
        build_answer_input,
    )

    history = TrustedHistory(turns=(turn(1),))
    kwargs = dict(
        context_window_tokens=8192,
        conversation_context=[],
        input_messages=None,
        trusted_history=history,
    )
    retrieval = build_retrieval_input("Show the second image", **kwargs)
    answer, _, _ = build_answer_input("Show the second image", [], **kwargs)
    for messages in (retrieval, answer):
        text = json.dumps(messages)
        assert "message-1" in text and "image-1" in text
        assert "Show the second image" in text
        assert "not authorization" in text


def test_public_history_is_scrubbed_before_validation():
    from nianlun.agent.retrieval_answer.input_messages import validate_public_messages

    validate_public_messages(
        [
            {
                "role": "assistant",
                "content": "![diagram](data:image/png;base64," + "A" * 40000 + ")",
            },
            {"role": "user", "content": "Explain it"},
        ],
        agent_version="v0",
        context_window_tokens=1000,
    )


@pytest.mark.parametrize("version", ["v0", "v1"])
@pytest.mark.parametrize("stream", [False, True])
def test_runtime_explicit_history_reaches_model_without_authorizing_images(
    version, stream
):
    from langchain_core.messages import AIMessage
    from nianlun.agent.lead_agent.runtime import AgentRuntime
    from tests.agent.test_retrieval_answer import _KnowledgeBase, _runtime

    captured = {}

    class Graph:
        def stream(self, values, **kwargs):
            captured.update(values)
            collector = kwargs["context"]["retrieval_collector"]
            assert not collector.image_context.candidates
            yield {"type": "values", "data": {"messages": [AIMessage(content="Done")]}}

    if version == "v1":
        runtime, _, retrieval = _runtime()
    else:
        runtime = AgentRuntime(
            agent=Graph(),
            model="fake",
            effective_url="fake",
            tool_logging=False,
            kb=_KnowledgeBase(),
        )
    history = TrustedHistory(turns=(turn(1),))
    if stream:
        result = list(
            runtime.iter_events(
                "Show that image", thread_id="history", trusted_history=history
            )
        )[-1]["data"]
    else:
        result = runtime.invoke(
            "Show that image", thread_id="history", trusted_history=history
        )
    messages = (
        retrieval.inputs[0]["messages"] if version == "v1" else captured["messages"]
    )
    assert "message-1" in json.dumps(messages)
    assert result["answer"]


def test_public_runtime_scrubs_images_without_mutating_or_inventing_history():
    from tests.agent.test_retrieval_answer import _runtime

    runtime, _, retrieval = _runtime()
    messages = [
        {
            "role": "assistant",
            "content": "![caption](data:image/png;base64," + "A" * 30000 + ")",
        },
        {"role": "user", "content": "Explain that diagram"},
    ]
    list(runtime.iter_public_events(messages, thread_id="caller-history"))
    text = json.dumps(retrieval.inputs)
    assert "base64" not in text
    assert "caption" in text
    assert "message-1" not in text


def test_scrub_removes_targets_nested_in_image_caption():
    clean = scrub_image_targets(
        "![![nested](https://secret/nested)](https://secret/outer)"
    )
    assert "https://secret" not in clean


def test_v0_committed_history_replaces_checkpoint_failed_turns():
    from langchain_core.messages import AIMessage, HumanMessage
    from langgraph.checkpoint.memory import MemorySaver
    from langgraph.graph import START, END, StateGraph, MessagesState
    from nianlun.agent.lead_agent.runtime import AgentRuntime
    from tests.agent.test_retrieval_answer import _KnowledgeBase

    seen = []
    builder = StateGraph(MessagesState)

    def answer(state):
        seen.append([message.content for message in state["messages"]])
        return {"messages": [AIMessage(content="Done")]}

    builder.add_node("model", answer)
    builder.add_edge(START, "model")
    builder.add_edge("model", END)
    graph = builder.compile(checkpointer=MemorySaver())
    graph.invoke(
        {"messages": [HumanMessage(content="Uncommitted failed image selection")]},
        {"configurable": {"thread_id": "reused"}},
    )
    runtime = AgentRuntime(
        agent=graph,
        model="fake",
        effective_url="fake",
        tool_logging=False,
        kb=_KnowledgeBase(),
    )
    runtime.invoke(
        "Show second image",
        thread_id="reused",
        trusted_history=TrustedHistory(turns=(turn(1),)),
    )
    assert "Uncommitted" not in json.dumps(seen[-1])
    assert "message-1" in json.dumps(seen[-1])


@pytest.mark.parametrize("asynchronous", [False, True])
def test_image_history_budget_drops_whole_turns_without_summarizing_or_losing_current_tools(
    asynchronous,
):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    from langchain_core.messages import (
        AIMessage,
        HumanMessage,
        ToolMessage,
        convert_to_messages,
    )
    from nianlun.agent.middleware import ContextSummarizationMiddleware

    history = TrustedHistory(turns=(turn(1), turn(2)))
    historical = convert_to_messages(history.messages())
    current = [
        HumanMessage(content="Current query"),
        AIMessage(
            content="", tool_calls=[{"name": "read", "args": {}, "id": "read-1"}]
        ),
        ToolMessage(content="Current evidence", tool_call_id="read-1"),
    ]
    model = MagicMock()
    middleware = ContextSummarizationMiddleware(
        model,
        model_context_limit=150,
        token_counter=lambda messages: sum(
            40 if getattr(message, "name", "") else 10 for message in messages
        ),
    )
    runtime = SimpleNamespace(context={"trusted_history": history})
    state = {"messages": [*historical, *current]}
    update = (
        asyncio.run(middleware.abefore_model(state, runtime))
        if asynchronous
        else middleware.before_model(state, runtime)
    )
    assert update is not None
    remaining = update["messages"][1:]
    assert [message.content for message in remaining[-3:]] == [
        message.content for message in current
    ]
    assert "message-1" not in json.dumps([message.content for message in remaining])
    assert "message-2" in json.dumps([message.content for message in remaining])
    assert [item.message_id for item in runtime.context["trusted_history"].turns] == [
        "message-2"
    ]
    model.invoke.assert_not_called()
    model.ainvoke.assert_not_called()


@pytest.mark.parametrize("asynchronous", [False, True])
def test_input_budget_keeps_latest_text_turn_after_old_image_turn_is_pruned(
    asynchronous,
):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from langchain_core.messages import convert_to_messages

    from nianlun.agent.middleware import ContextSummarizationMiddleware
    from nianlun.agent.prompt_registry import CURRENT_PROMPT_BUNDLE
    from nianlun.agent.retrieval_answer.input_messages import (
        build_answer_input,
        build_retrieval_input,
    )

    old = HistoryTurn(
        message_id="old-image",
        question="word " * 500,
        answer="long content " * 1200,
        images=(
            HistoryImage(order=1, caption="Image", status="available", sources=()),
        ),
        image_count=1,
    )
    latest = HistoryTurn(
        message_id="latest-text",
        question="Describe first option",
        answer="The first option is option A",
    )
    history = TrustedHistory(turns=(old, latest))
    kwargs = dict(
        context_window_tokens=8192,
        conversation_context=[],
        input_messages=None,
        prompt_bundle=CURRENT_PROMPT_BUNDLE,
    )
    retrieval = build_retrieval_input(
        "Explain option A", trusted_history=history, **kwargs
    )
    assert old.answer not in json.dumps(retrieval)
    assert latest.answer in json.dumps(retrieval)
    assert estimate_tokens(retrieval) <= 8192 - 4096

    model = MagicMock()
    middleware = ContextSummarizationMiddleware(model, model_context_limit=8192)
    runtime = SimpleNamespace(context={"trusted_history": history})
    state = {"messages": convert_to_messages(retrieval)}
    update = (
        asyncio.run(middleware.abefore_model(state, runtime))
        if asynchronous
        else middleware.before_model(state, runtime)
    )
    assert update is None
    assert runtime.context["trusted_history"].turns == (latest,)
    answer, _, _ = build_answer_input(
        "Explain option A",
        [],
        trusted_history=runtime.context["trusted_history"],
        **kwargs,
    )
    assert latest.answer in json.dumps(answer)
    assert old.answer not in json.dumps(answer)
    assert estimate_tokens(answer) <= 8192 - 4096
    model.invoke.assert_not_called()
    model.ainvoke.assert_not_called()


def test_fit_keeps_turn_group_names_after_only_text_history_survives():
    latest = turn(2, answer="The first option is option A", images=False)
    history = fit_history(
        TrustedHistory(turns=(turn(1, answer="long content " * 1200), latest)),
        token_budget=100,
    )
    assert history.turns == (latest,)
    assert {message.get("name") for message in history.messages()} == {
        "history_message-2"
    }
    assert fit_history(history, token_budget=100).messages() == history.messages()


def test_pure_text_history_keeps_legacy_unnamed_messages_after_fitting():
    history = TrustedHistory(turns=(turn(1, answer="Text answer", images=False),))
    assert fit_history(history).messages() == [
        {"role": "user", "content": "question-1"},
        {"role": "assistant", "content": "Text answer"},
    ]


@pytest.mark.parametrize("asynchronous", [False, True])
def test_pruned_image_history_still_drops_whole_text_turn_at_hard_limit(asynchronous):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from langchain_core.messages import HumanMessage, convert_to_messages

    from nianlun.agent.middleware import ContextSummarizationMiddleware

    latest = turn(2, answer="The first option is option A", images=False)
    history = fit_history(
        TrustedHistory(turns=(turn(1, answer="long content " * 1200), latest)),
        token_budget=100,
    )
    current = HumanMessage(content="Current request")
    model = MagicMock()
    middleware = ContextSummarizationMiddleware(
        model, model_context_limit=estimate_tokens([current]) + 1
    )
    runtime = SimpleNamespace(context={"trusted_history": history})
    state = {"messages": [*convert_to_messages(history.messages()), current]}
    update = (
        asyncio.run(middleware.abefore_model(state, runtime))
        if asynchronous
        else middleware.before_model(state, runtime)
    )
    assert update is not None
    assert update["messages"][1:] == [current]
    assert runtime.context["trusted_history"].turns == ()

    # A later retrieval step must still reject oversized current evidence.
    state = {"messages": [HumanMessage(content="Current evidence " * 100)]}
    with pytest.raises(ValueError, match="Current request evidence exceeds"):
        if asynchronous:
            asyncio.run(middleware.abefore_model(state, runtime))
        else:
            middleware.before_model(state, runtime)
    model.invoke.assert_not_called()
    model.ainvoke.assert_not_called()
