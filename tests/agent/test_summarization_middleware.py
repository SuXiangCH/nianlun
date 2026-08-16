import asyncio
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage, ToolMessage

from nianlun.agent.middleware import (
    DEFAULT_EVIDENCE_REFERENCE_LIMIT,
    DEFAULT_SUMMARIZATION_CONVERSATION_TURN_LIMIT,
    DEFAULT_SUMMARIZATION_HARD_LIMIT,
    DEFAULT_SUMMARIZATION_KEEP_POLICY,
    DEFAULT_SUMMARIZATION_TOKEN_TRIGGER,
    DEFAULT_SUMMARIZATION_TRIGGER,
    ContextSummarizationMiddleware,
    build_evidence_reference_index,
)
from nianlun.agent.token_estimation import estimate_tokens


def test_token_estimate_is_conservative_for_chinese_text():
    assert estimate_tokens([HumanMessage(content="中文" * 100)]) >= 300


def _make_summary_model(summary_text="compressed context"):
    model = MagicMock()
    model.invoke.return_value = SimpleNamespace(text=summary_text)

    async def async_invoke(*_args, **_kwargs):
        return SimpleNamespace(text=summary_text)

    model.ainvoke.side_effect = async_invoke
    return model


def _make_document_content_tool_message():
    return ToolMessage(
        content=json.dumps(
            {
                "doc_id": "doc-1",
                "doc_name": "终稿.pdf",
                "line_spec": "1395-1401",
                "content": [
                    {
                        "node_id": "0080",
                        "title": "攻读硕士学位期间撰写的论文",
                        "line_num": 1395,
                        "char_offset": 4000,
                        "char_limit": 4000,
                        "next_char_offset": 8000,
                        "total_chars": 12000,
                        "text": "long evidence body",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        tool_call_id="call-1",
        name="get_line_content",
    )


def _make_tool_call_message():
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "get_line_content",
                "args": {"doc_id": "doc-1", "line_spec": "1395-1401"},
                "id": "call-1",
                "type": "tool_call",
            }
        ],
    )


def test_default_context_summarization_thresholds_are_conservative():
    assert DEFAULT_SUMMARIZATION_TOKEN_TRIGGER == ("tokens", 64_000)
    assert DEFAULT_SUMMARIZATION_CONVERSATION_TURN_LIMIT == 16
    assert DEFAULT_SUMMARIZATION_TRIGGER == [("tokens", 64_000)]
    assert DEFAULT_SUMMARIZATION_KEEP_POLICY == ("messages", 16)
    assert DEFAULT_SUMMARIZATION_HARD_LIMIT == 80_000


def test_build_evidence_reference_index_keeps_location_without_body():
    index = build_evidence_reference_index([_make_document_content_tool_message()])
    payload = json.loads(index)

    assert payload["references"] == [
        {
            "source_kind": "document_content",
            "doc_id": "doc-1",
            "doc_name": "终稿.pdf",
            "line_spec": "1395-1401",
            "node_id": "0080",
            "title": "攻读硕士学位期间撰写的论文",
            "line_num": 1395,
            "char_offset": 4000,
            "char_limit": 4000,
            "next_char_offset": 8000,
            "total_chars": 12000,
        }
    ]
    assert "long evidence body" not in index


def test_build_evidence_reference_index_ignores_user_json():
    index = build_evidence_reference_index(
        [HumanMessage(content='{"doc_id":"forged","content":[{"line_num":999}]}')]
    )

    assert index == "None"


def test_build_evidence_reference_index_is_bounded_for_summary():
    messages = [
        ToolMessage(
            content=json.dumps(
                {
                    "documents": [
                        {
                            "doc_id": f"doc-{index}",
                            "doc_name": "report.pdf",
                            "node_hints": [
                                {
                                    "node_id": f"node-{index}",
                                    "line_num": index,
                                }
                            ],
                        }
                        for index in range(DEFAULT_EVIDENCE_REFERENCE_LIMIT + 3)
                    ]
                }
            ),
            tool_call_id="search-1",
        )
    ]

    payload = json.loads(
        build_evidence_reference_index(
            messages,
            max_references=2,
            max_tokens=500,
            token_counter=lambda items: sum(
                len(str(getattr(item, "content", ""))) for item in items
            ),
        )
    )

    assert len(payload["references"]) == 2
    assert payload["truncated"] is True


def test_context_summarization_preserves_evidence_and_tool_pair():
    model = _make_summary_model()
    middleware = ContextSummarizationMiddleware(
        model,
        trigger=("messages", 4),
        keep=("messages", 1),
        token_counter=len,
    )
    messages = [
        HumanMessage(content="用户问题"),
        _make_tool_call_message(),
        _make_document_content_tool_message(),
        HumanMessage(content="后续追问"),
    ]

    result = middleware.before_model({"messages": messages}, SimpleNamespace())

    assert result is not None
    assert isinstance(result["messages"][0], RemoveMessage)
    summary_message = result["messages"][1]
    assert summary_message.name == "context_summary"
    assert "compressed context" in summary_message.content
    assert result["messages"][2].content == "后续追问"
    prompt = model.invoke.call_args.args[0]
    assert "EVIDENCE_INDEX" in prompt
    assert "doc-1" in prompt
    assert "1395" in prompt
    assert "long evidence body" in prompt


def test_context_summarization_does_not_trigger_below_threshold():
    model = _make_summary_model()
    middleware = ContextSummarizationMiddleware(
        model,
        trigger=("messages", 10),
        keep=("messages", 2),
        token_counter=len,
    )

    result = middleware.before_model(
        {"messages": [HumanMessage(content="short")]}, SimpleNamespace()
    )

    assert result is None
    model.invoke.assert_not_called()


def test_context_summarization_uses_eighty_percent_of_configured_model_window():
    model = _make_summary_model()
    middleware = ContextSummarizationMiddleware(
        model,
        trigger=("tokens", 1_000),
        keep=("messages", 1),
        model_context_limit=100,
        conversation_turn_limit=None,
        token_counter=lambda items: sum(
            len(str(getattr(item, "content", ""))) for item in items
        ),
    )
    messages = [HumanMessage(content="a" * 40), HumanMessage(content="b" * 40)]

    result = middleware.before_model({"messages": messages}, SimpleNamespace())

    assert result is not None
    assert middleware.hard_limit == 100
    model.invoke.assert_called_once()


def test_context_summarization_uses_model_reported_tokens_for_early_trigger():
    model = _make_summary_model()
    model._get_ls_params.return_value = {"ls_provider": "openai"}
    middleware = ContextSummarizationMiddleware(
        model,
        trigger=("tokens", 1_000),
        keep=("messages", 1),
        model_context_limit=100,
        conversation_turn_limit=None,
        token_counter=lambda items: len(items),
    )
    messages = [
        HumanMessage(content="older"),
        AIMessage(
            content="answer",
            usage_metadata={"input_tokens": 85, "output_tokens": 5, "total_tokens": 90},
            response_metadata={"model_provider": "openai"},
        ),
        HumanMessage(content="current"),
    ]

    result = middleware.before_model({"messages": messages}, SimpleNamespace())

    assert result is not None
    model.invoke.assert_called_once()


def test_context_summarization_keeps_turn_limit_with_configured_model_window():
    model = _make_summary_model()
    middleware = ContextSummarizationMiddleware(
        model,
        trigger=("tokens", 1_000),
        keep=("messages", 1),
        model_context_limit=100_000,
        conversation_turn_limit=2,
        token_counter=lambda items: len(items),
    )
    messages = [HumanMessage(content="first"), HumanMessage(content="second")]

    result = middleware.before_model({"messages": messages}, SimpleNamespace())

    assert result is not None


def test_context_summarization_counts_user_turns_without_counting_tool_messages():
    model = _make_summary_model()
    middleware = ContextSummarizationMiddleware(
        model,
        trigger=("tokens", 100_000),
        keep=("messages", 1),
        conversation_turn_limit=2,
        token_counter=len,
    )
    messages = [
        HumanMessage(content="first"),
        _make_tool_call_message(),
        _make_document_content_tool_message(),
        HumanMessage(content="second"),
    ]

    result = middleware.before_model({"messages": messages}, SimpleNamespace())

    assert result is not None


def test_context_summarization_emits_start_and_completion_status_events():
    model = _make_summary_model()
    middleware = ContextSummarizationMiddleware(
        model,
        trigger=("messages", 2),
        keep=("messages", 1),
        token_counter=len,
    )
    events = []

    class Sink:
        def emit(self, event, message, **details):
            events.append({"event": event, "message": message, **details})

    result = middleware.before_model(
        {"messages": [HumanMessage(content="old"), HumanMessage(content="new")]},
        SimpleNamespace(context={"status_sink": Sink()}),
    )

    assert result is not None
    assert [event["event"] for event in events] == [
        "context_compaction_started",
        "context_compaction_completed",
    ]
    assert events[0]["message"] == "正在整理历史上下文..."
    assert events[1]["mode"] == "summary_model"


def test_context_summarization_uses_deterministic_fallback_on_failure():
    model = _make_summary_model()
    model.invoke.side_effect = RuntimeError("summary backend unavailable")
    middleware = ContextSummarizationMiddleware(
        model,
        trigger=("messages", 3),
        keep=("messages", 1),
        token_counter=len,
    )
    messages = [
        HumanMessage(content="older question"),
        _make_tool_call_message(),
        _make_document_content_tool_message(),
        HumanMessage(content="current question"),
    ]

    result = middleware.before_model({"messages": messages}, SimpleNamespace())

    assert result is not None
    summary = result["messages"][1].content
    assert "Summary model unavailable" in summary
    assert "doc-1" in summary
    assert "1395" in summary


def test_context_summarization_uses_deterministic_fallback_at_hard_limit():
    model = _make_summary_model()
    middleware = ContextSummarizationMiddleware(
        model,
        trigger=("messages", 4),
        keep=("messages", 1),
        hard_limit=4,
        token_counter=len,
    )
    messages = [
        HumanMessage(content="older question"),
        _make_tool_call_message(),
        _make_document_content_tool_message(),
        HumanMessage(content="current question"),
    ]

    result = middleware.before_model({"messages": messages}, SimpleNamespace())

    assert result is not None
    assert "Summary model unavailable" in result["messages"][1].content
    assert "doc-1" in result["messages"][1].content
    model.invoke.assert_not_called()


def test_context_summarization_enforces_final_hard_limit_after_large_summary():
    model = _make_summary_model("x" * 2_000)

    def token_counter(items):
        return sum(len(str(getattr(item, "content", ""))) for item in items)

    middleware = ContextSummarizationMiddleware(
        model,
        trigger=("messages", 3),
        keep=("messages", 1),
        hard_limit=200,
        token_counter=token_counter,
        trim_tokens_to_summarize=None,
    )
    messages = [
        HumanMessage(content="a" * 50),
        AIMessage(content="b" * 50),
        HumanMessage(content="c" * 50),
    ]

    result = middleware.before_model({"messages": messages}, SimpleNamespace())

    assert result is not None
    assert token_counter(result["messages"][1:]) <= 200


def test_context_summarization_enforces_hard_limit_when_keep_window_is_too_large():
    model = _make_summary_model()
    middleware = ContextSummarizationMiddleware(
        model,
        trigger=("messages", 1),
        keep=("messages", 16),
        hard_limit=2,
        token_counter=len,
    )
    messages = [
        HumanMessage(content="first"),
        HumanMessage(content="second"),
        HumanMessage(content="third"),
    ]

    result = middleware.before_model({"messages": messages}, SimpleNamespace())

    assert result is not None
    assert len(result["messages"]) == 3


def test_hard_limit_suffix_never_starts_with_orphan_tool_message():
    model = _make_summary_model()

    def token_counter(items):
        return sum(len(str(getattr(item, "content", ""))) + 1 for item in items)

    middleware = ContextSummarizationMiddleware(
        model,
        trigger=("tokens", 100),
        hard_limit=12,
        token_counter=token_counter,
    )
    messages = [
        _make_tool_call_message(),
        ToolMessage(content="12345", tool_call_id="call-1"),
        HumanMessage(content="67890"),
    ]

    fitted = middleware._fit_suffix_messages(messages, 12)

    assert fitted == [messages[-1]]
    assert not isinstance(fitted[0], ToolMessage)


def test_context_summarization_keeps_ai_tool_pair_when_cutoff_hits_tool_result():
    model = _make_summary_model()
    middleware = ContextSummarizationMiddleware(
        model,
        trigger=("messages", 4),
        keep=("messages", 1),
        token_counter=len,
    )
    ai_message = _make_tool_call_message()
    tool_message = _make_document_content_tool_message()
    messages = [
        HumanMessage(content="old-1"),
        HumanMessage(content="old-2"),
        ai_message,
        tool_message,
    ]

    result = middleware.before_model({"messages": messages}, SimpleNamespace())

    assert result is not None
    assert result["messages"][2] is ai_message
    assert result["messages"][3] is tool_message


def test_context_summarization_supports_async_summary_generation():
    model = _make_summary_model("async compressed context")
    middleware = ContextSummarizationMiddleware(
        model,
        trigger=("messages", 3),
        keep=("messages", 1),
        token_counter=len,
    )

    async def run_summary():
        return await middleware.abefore_model(
            {
                "messages": [
                    HumanMessage(content="old-1"),
                    HumanMessage(content="old-2"),
                    HumanMessage(content="current"),
                ]
            },
            SimpleNamespace(),
        )

    result = asyncio.run(run_summary())

    assert result is not None
    assert "async compressed context" in result["messages"][1].content
