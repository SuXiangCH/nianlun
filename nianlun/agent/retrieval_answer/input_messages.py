"""Fit caller-owned history without changing the published Agent prompts."""

from nianlun.agent.contracts import AnswerImagePolicy, RetrievedSnippet
from typing import cast

from nianlun.agent.prompt_registry import LEGACY_PROMPT_BUNDLE, PromptBundle
from nianlun.agent.prompts.v4.retrieval_answer import DEFAULT_CONTEXT_WINDOW_TOKENS
from nianlun.agent.token_estimation import estimate_tokens
from nianlun.agent.image_history import TrustedHistory, fit_history, scrub_messages


def validate_public_messages(
    messages: list[dict[str, str]], *, agent_version: str, context_window_tokens: int
) -> None:
    messages = scrub_messages(messages)
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
    prompt_bundle: PromptBundle = LEGACY_PROMPT_BUNDLE,
    trusted_history: TrustedHistory | None = None,
) -> list[dict[str, str]]:
    if trusted_history is not None:
        base = prompt_bundle.build_retrieval_messages(
            question, context_window_tokens=context_window_tokens
        )
        from nianlun.agent.prompts.v4.retrieval_answer import (
            RETRIEVAL_OUTPUT_RESERVE_TOKENS,
        )

        history = fit_history(
            trusted_history,
            token_budget=(
                (context_window_tokens or DEFAULT_CONTEXT_WINDOW_TOKENS)
                - RETRIEVAL_OUTPUT_RESERVE_TOKENS
                - estimate_tokens(base)
            ),
        ).messages()
        return [*history, *base]
    if input_messages is None:
        return prompt_bundle.build_retrieval_messages(
            question,
            context_window_tokens=context_window_tokens,
            conversation_context=conversation_context,
        )
    history = scrub_messages(input_messages)[:-1]
    messages = prompt_bundle.build_retrieval_messages(
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
    prompt_bundle: PromptBundle = LEGACY_PROMPT_BUNDLE,
    image_capability: bool = False,
    trusted_history: TrustedHistory | None = None,
    image_policy: AnswerImagePolicy | None = None,
) -> tuple[list[dict[str, str]], list[int], list[int]]:
    if image_capability and image_policy is not None:
        budget = image_policy.model_message()
        remaining = (
            context_window_tokens or DEFAULT_CONTEXT_WINDOW_TOKENS
        ) - estimate_tokens([budget])
        if remaining <= 0:
            raise ValueError("模型上下文不足以容纳图片资源预算。")
        messages, selected, omitted = build_answer_input(
            question,
            snippets,
            context_window_tokens=remaining,
            conversation_context=conversation_context,
            input_messages=input_messages,
            prompt_bundle=prompt_bundle,
            image_capability=image_capability,
            trusted_history=trusted_history,
        )
        # Some OpenAI-compatible providers only accept one initial system message.
        system = {
            **messages[0],
            "content": messages[0]["content"] + "\n\n" + budget["content"],
        }
        return [system, *messages[1:]], selected, omitted
    if prompt_bundle.supports_images:
        snippets = [
            _model_snippet(snippet, image_capability=image_capability)
            for snippet in snippets
        ]
    if trusted_history is not None:
        base, selected, omitted = prompt_bundle.build_answer_messages(
            question,
            snippets,
            context_window_tokens=context_window_tokens,
            image_capability=image_capability,
        )
        from nianlun.agent.prompts.v4.retrieval_answer import (
            ANSWER_OUTPUT_RESERVE_TOKENS,
        )

        history = fit_history(
            trusted_history,
            token_budget=(
                (context_window_tokens or DEFAULT_CONTEXT_WINDOW_TOKENS)
                - ANSWER_OUTPUT_RESERVE_TOKENS
                - estimate_tokens(base)
            ),
        ).messages()
        return [base[0], *history, *base[1:]], selected, omitted
    if input_messages is None:
        return prompt_bundle.build_answer_messages(
            question,
            snippets,
            context_window_tokens=context_window_tokens,
            conversation_context=conversation_context,
            image_capability=image_capability,
        )
    history = scrub_messages(input_messages)[:-1]
    messages, selected, omitted = prompt_bundle.build_answer_messages(
        question,
        snippets,
        context_window_tokens=_remaining_budget(context_window_tokens, history),
        image_capability=image_capability,
    )
    return [messages[0], *history, *messages[1:]], selected, omitted


def _model_snippet(
    snippet: RetrievedSnippet, *, image_capability: bool
) -> RetrievedSnippet:
    """Budget and serialize the same public evidence, never private source metadata."""
    item = {
        field: snippet[field]
        for field in (
            "citation_id",
            "doc_id",
            "doc_name",
            "line_spec",
            "node_id",
            "title",
            "line_num",
            "char_offset",
            "char_limit",
            "total_chars",
            "text_truncated",
        )
        if field in snippet
    }
    item["text"] = snippet.get("model_text", snippet["text"])
    if image_capability:
        item["images"] = [
            {
                field: image[field]
                for field in (
                    "ref_id",
                    "caption",
                    "width",
                    "height",
                    "status",
                    "citation_ids",
                    "source_start",
                    "source_end",
                    "text_start",
                    "text_end",
                )
                if field in image
            }
            for image in snippet.get("model_images", [])
        ]
    return cast(RetrievedSnippet, item)
