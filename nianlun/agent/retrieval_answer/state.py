"""v1 外层 LangGraph 的最小状态。"""

from __future__ import annotations

from typing import Annotated, Any, NotRequired, Required, TypedDict

from nianlun.agent.contracts import RetrievedSnippet


def append_bounded_conversation(
    current: list[dict[str, str]], update: list[dict[str, str]]
) -> list[dict[str, str]]:
    return (current + update)[-6:]


class RetrievalAnswerGraphState(TypedDict, total=False):
    question: Required[str]
    run_id: Required[str]
    conversation_context: Annotated[list[dict[str, str]], append_bounded_conversation]
    retrieved_snippets: list[RetrievedSnippet]
    answer_input_citation_ids: list[int]
    omitted_citation_ids: list[int]
    answer: str
    clarification: NotRequired[dict[str, Any] | None]
    retrieval_usage: NotRequired[dict[str, int] | None]
    answer_usage: NotRequired[dict[str, int] | None]


__all__ = ["RetrievalAnswerGraphState"]
