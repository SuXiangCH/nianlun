"""Fit caller-owned history without changing the published Agent prompts."""

from nianlun.agent.contracts import RetrievedSnippet
from nianlun.agent.retrieval_answer.prompt import (
    DEFAULT_CONTEXT_WINDOW_TOKENS,
    build_answer_messages,
    build_retrieval_messages,
)
from nianlun.agent.token_estimation import estimate_tokens


def validate_public_messages(
    messages: list[dict[str, str]], *, agent_version: str, context_window_tokens: int
) -> None:
    if agent_version == "v1":
        build_retrieval_input(
            messages[-1]["content"],
            context_window_tokens=context_window_tokens,
            conversation_context=[],
            input_messages=messages,
        )
        build_answer_input(
            messages[-1]["content"],
            [],
            context_window_tokens=context_window_tokens,
            conversation_context=[],
            input_messages=messages,
        )
    elif estimate_tokens(messages) > context_window_tokens:
        raise ValueError("模型上下文不足以容纳完整请求历史。")


def _remaining_budget(
    context_window_tokens: int | None, history: list[dict[str, str]]
) -> int:
    limit = context_window_tokens or DEFAULT_CONTEXT_WINDOW_TOKENS
    # Per-message estimates give a conservative upper bound for the full history.
    remaining = limit - sum(estimate_tokens([message]) for message in history)
    if remaining <= 0:
        raise ValueError("模型上下文不足以容纳完整请求历史。")
    return remaining


def build_retrieval_input(
    question: str,
    *,
    context_window_tokens: int | None,
    conversation_context: list[dict[str, str]],
    input_messages: list[dict[str, str]] | None,
) -> list[dict[str, str]]:
    if input_messages is None:
        return build_retrieval_messages(
            question,
            context_window_tokens=context_window_tokens,
            conversation_context=conversation_context,
        )
    history = input_messages[:-1]
    messages = build_retrieval_messages(
        question,
        context_window_tokens=_remaining_budget(context_window_tokens, history),
    )
    return [*history, *messages]


def build_answer_input(
    question: str,
    snippets: list[RetrievedSnippet],
    *,
    context_window_tokens: int | None,
    conversation_context: list[dict[str, str]],
    input_messages: list[dict[str, str]] | None,
) -> tuple[list[dict[str, str]], list[int], list[int]]:
    if input_messages is None:
        return build_answer_messages(
            question,
            snippets,
            context_window_tokens=context_window_tokens,
            conversation_context=conversation_context,
        )
    history = input_messages[:-1]
    messages, selected, omitted = build_answer_messages(
        question,
        snippets,
        context_window_tokens=_remaining_budget(context_window_tokens, history),
    )
    return [messages[0], *history, *messages[1:]], selected, omitted
