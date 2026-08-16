import asyncio
import json
from types import SimpleNamespace

from langchain.agents.middleware import ToolCallRequest
from langchain_core.messages import HumanMessage, ToolMessage
from langgraph.graph import END
from langgraph.types import Command

from nianlun.agent.middleware import (
    CLARIFICATION_EVENT_DUPLICATE,
    CLARIFICATION_EVENT_REQUESTED,
    CLARIFICATION_STATUS_WAITING,
    CLARIFICATION_TOOL_NAME,
    ClarificationMiddleware,
)
from nianlun.agent.tools import ask_clarification_tool, build_tools


class StatusSink:
    def __init__(self):
        self.events = []

    def emit(self, event, message, **details):
        self.events.append({"event": event, "message": message, **details})


def _request(
    *,
    name=CLARIFICATION_TOOL_NAME,
    call_id="call-clarify-1",
    args=None,
    messages=None,
    sink=None,
    clarification_enabled=True,
):
    context = {"clarification_enabled": clarification_enabled}
    if sink is not None:
        context["status_sink"] = sink
    runtime = SimpleNamespace(context=context)
    return ToolCallRequest(
        tool_call={
            "name": name,
            "args": args
            or {
                "question": "比较哪两个文档？",
                "clarification_type": "missing_info",
            },
            "id": call_id,
            "type": "tool_call",
        },
        tool=None,
        state={"messages": messages or []},
        runtime=runtime,
    )


def _command_message(result):
    assert isinstance(result, Command)
    assert result.goto == END
    message = result.update["messages"][0]
    assert isinstance(message, ToolMessage)
    return message


def test_clarification_tool_has_fixed_schema_and_is_opt_in_for_tool_building():
    assert ask_clarification_tool.name == CLARIFICATION_TOOL_NAME
    assert list(ask_clarification_tool.tool_call_schema.model_fields) == [
        "question",
        "clarification_type",
        "context",
        "options",
    ]
    assert CLARIFICATION_TOOL_NAME not in [tool.name for tool in build_tools()]
    assert CLARIFICATION_TOOL_NAME in [
        tool.name for tool in build_tools(include_clarification=True)
    ]


def test_disabled_request_returns_model_visible_error_without_interrupting():
    result = ClarificationMiddleware().wrap_tool_call(
        _request(clarification_enabled=False),
        lambda _request: (_ for _ in ()).throw(AssertionError("must not run")),
    )

    assert isinstance(result, ToolMessage)
    assert result.status == "error"
    assert json.loads(result.content)["error"]["code"] == "clarification_disabled"


def test_clarification_intercepts_call_and_returns_waiting_command():
    sink = StatusSink()
    request = _request(
        sink=sink,
        args={
            "question": "比较哪两个文档？",
            "clarification_type": "approach_choice",
            "context": "当前问题没有明确比较对象。",
            "options": ["终稿.pdf", "涂慧.pdf"],
        },
    )
    handler_called = False

    def handler(_request):
        nonlocal handler_called
        handler_called = True
        return ToolMessage(content="should not run", tool_call_id="call-clarify-1")

    message = _command_message(
        ClarificationMiddleware().wrap_tool_call(request, handler)
    )

    assert handler_called is False
    assert message.name == CLARIFICATION_TOOL_NAME
    assert message.tool_call_id == "call-clarify-1"
    assert message.id == "clarification:call-clarify-1"
    assert message.additional_kwargs["status"] == CLARIFICATION_STATUS_WAITING
    clarification = message.additional_kwargs["clarification"]
    assert clarification["question"] == "比较哪两个文档？"
    assert clarification["options"] == ["终稿.pdf", "涂慧.pdf"]
    assert "1. 终稿.pdf" in message.content
    assert sink.events[0]["event"] == CLARIFICATION_EVENT_REQUESTED
    assert sink.events[0]["status"] == CLARIFICATION_STATUS_WAITING


def test_non_clarification_tool_call_is_transparent():
    request = _request(name="search_document_nodes")
    expected = ToolMessage(content="ok", tool_call_id="call-clarify-1")
    assert (
        ClarificationMiddleware().wrap_tool_call(request, lambda _request: expected)
        is expected
    )


def test_async_clarification_interception_does_not_execute_handler():
    async def handler(_request):
        raise AssertionError("clarification handler must not execute")

    result = asyncio.run(ClarificationMiddleware().awrap_tool_call(_request(), handler))
    assert isinstance(result, Command)
    assert result.goto == END


def test_json_string_options_are_normalized_without_character_iteration():
    message = _command_message(
        ClarificationMiddleware().wrap_tool_call(
            _request(
                args={
                    "question": "请选择环境。",
                    "clarification_type": "approach_choice",
                    "options": json.dumps(["测试", "生产"]),
                }
            ),
            lambda _request: None,
        )
    )
    assert "1. 测试" in message.content
    assert "2. 生产" in message.content
    assert "3." not in message.content


def test_repeated_same_tool_call_uses_stable_message_id():
    middleware = ClarificationMiddleware()
    first = _command_message(
        middleware.wrap_tool_call(_request(), lambda _request: None)
    )
    second = _command_message(
        middleware.wrap_tool_call(
            _request(
                messages=[
                    # The state contains the prior waiting message after a checkpoint.
                    first
                ]
            ),
            lambda _request: None,
        )
    )

    assert second.id == first.id
    assert second.tool_call_id == first.tool_call_id
    assert second.additional_kwargs["clarification"]["duplicate"] is False


def test_repeated_question_with_new_tool_call_is_marked_duplicate():
    sink = StatusSink()
    middleware = ClarificationMiddleware()
    first = _command_message(
        middleware.wrap_tool_call(_request(sink=sink), lambda _request: None)
    )
    second = _command_message(
        middleware.wrap_tool_call(
            _request(
                call_id="call-clarify-2",
                messages=[first],
                sink=sink,
            ),
            lambda _request: None,
        )
    )

    assert second.tool_call_id == "call-clarify-2"
    assert second.additional_kwargs["clarification"]["duplicate"] is True
    assert sink.events[-1]["event"] == CLARIFICATION_EVENT_DUPLICATE


def test_same_question_is_allowed_after_user_resumes_thread():
    middleware = ClarificationMiddleware()
    first = _command_message(
        middleware.wrap_tool_call(_request(), lambda _request: None)
    )
    resumed_state = [first, HumanMessage(content="比较终稿和涂慧论文")]
    second = _command_message(
        middleware.wrap_tool_call(
            _request(call_id="call-clarify-2", messages=resumed_state),
            lambda _request: None,
        )
    )

    assert second.additional_kwargs["clarification"]["duplicate"] is False


def test_invalid_clarification_arguments_return_model_visible_error():
    result = ClarificationMiddleware().wrap_tool_call(
        _request(args={"question": "", "clarification_type": "missing_info"}),
        lambda _request: (_ for _ in ()).throw(AssertionError("must not run")),
    )

    assert isinstance(result, ToolMessage)
    assert result.status == "error"
    payload = json.loads(result.content)
    assert payload["error"]["code"] == "invalid_argument"
    assert payload["error"]["field"] == "question"


def test_invalid_option_count_is_rejected():
    result = ClarificationMiddleware(max_options=1).wrap_tool_call(
        _request(
            args={
                "question": "请选择。",
                "clarification_type": "approach_choice",
                "options": ["A", "B"],
            }
        ),
        lambda _request: None,
    )
    assert isinstance(result, ToolMessage)
    assert json.loads(result.content)["error"]["field"] == "options"
