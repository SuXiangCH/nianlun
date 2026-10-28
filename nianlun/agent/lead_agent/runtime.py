"""应用级 Agent runtime facade。

本模块只暴露一个已组装 Agent 的应用级运行对象；单次请求执行细节位于
:mod:`nianlun.agent.lead_agent.runner`，依赖组装位于
:mod:`nianlun.agent.lead_agent.factory`。
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
import threading
from typing import Any

from nianlun.agent.contracts import AgentRequestContext, AgentVersion, KnowledgeBasePort
from nianlun.agent.image_context import AnswerImagePolicy
from nianlun.agent.image_history import TrustedHistory
from nianlun.agent.lead_agent.runner import (
    AgentRequestContextFactory,
    AgentRunner,
    AgentStatusSink,
    RetrievalCollector,
)


@dataclass(init=False)
class AgentRuntime:
    """应用级 facade：持有可复用 graph 和应用绑定的请求依赖。"""

    runner: AgentRunner
    model: str
    effective_url: str
    agent_version: AgentVersion

    def __init__(
        self,
        *,
        agent: Any,
        model: str,
        effective_url: str,
        tool_logging: bool,
        kb: KnowledgeBasePort,
        recursion_limit: int = 192,
        agent_version: AgentVersion = "v0",
        tool_schema_version: int = 4,
        image_capability: bool = False,
        image_policy: AnswerImagePolicy = AnswerImagePolicy(),
        context_window_tokens: int | None = None,
        context_overhead_tokens: int = 0,
    ) -> None:
        self.runner = AgentRunner(
            agent=agent,
            context_factory=AgentRequestContextFactory(
                knowledge_base=kb,
                tool_logging=tool_logging,
                tool_schema_version=tool_schema_version,
                image_capability=image_capability,
                image_policy=image_policy,
            ),
            recursion_limit=recursion_limit,
            agent_version=agent_version,
            context_window_tokens=context_window_tokens,
            context_overhead_tokens=context_overhead_tokens,
        )
        self.model = model
        self.effective_url = effective_url
        self.agent_version = agent_version

    @property
    def agent(self) -> Any:
        return self.runner.agent

    @property
    def kb(self) -> KnowledgeBasePort:
        return self.runner.context_factory.knowledge_base

    @property
    def tool_logging(self) -> bool:
        return self.runner.context_factory.tool_logging

    def new_request_context(
        self,
        status_sink: AgentStatusSink | None = None,
        *,
        clarification_enabled: bool = False,
    ) -> tuple[RetrievalCollector, AgentRequestContext]:
        return self.runner.new_request_context(
            status_sink,
            clarification_enabled=clarification_enabled,
        )

    def invoke(
        self,
        user_query: str,
        thread_id: str = "default",
        *,
        clarification_enabled: bool = False,
        trusted_history: TrustedHistory | None = None,
    ) -> dict[str, Any]:
        return self.runner.invoke(
            user_query,
            thread_id=thread_id,
            clarification_enabled=clarification_enabled,
            trusted_history=trusted_history,
        )

    def stream_to_stdout(
        self, user_query: str, thread_id: str = "default"
    ) -> dict[str, Any]:
        return self.runner.stream_to_stdout(user_query, thread_id=thread_id)

    def iter_events(
        self,
        user_query: str,
        thread_id: str = "default",
        *,
        clarification_enabled: bool = False,
        cancellation_event: threading.Event | None = None,
        trusted_history: TrustedHistory | None = None,
    ) -> Iterator[dict[str, Any]]:
        return self.runner.iter_events(
            user_query,
            thread_id=thread_id,
            clarification_enabled=clarification_enabled,
            cancellation_event=cancellation_event,
            trusted_history=trusted_history,
        )

    def iter_public_events(
        self,
        messages: list[dict[str, str]],
        thread_id: str,
        *,
        clarification_enabled: bool = False,
        cancellation_event: threading.Event | None = None,
    ) -> Iterator[dict[str, Any]]:
        return self.runner.iter_public_events(
            messages,
            thread_id=thread_id,
            clarification_enabled=clarification_enabled,
            cancellation_event=cancellation_event,
        )


def run_agent(
    runtime: AgentRuntime,
    user_query: str,
    thread_id: str = "default",
    *,
    clarification_enabled: bool = False,
    trusted_history: TrustedHistory | None = None,
) -> dict[str, Any]:
    return runtime.invoke(
        user_query,
        thread_id=thread_id,
        clarification_enabled=clarification_enabled,
        **({"trusted_history": trusted_history} if trusted_history is not None else {}),
    )


def run_agent_streaming(
    runtime: AgentRuntime,
    user_query: str,
    thread_id: str = "default",
) -> dict[str, Any]:
    return runtime.stream_to_stdout(user_query, thread_id=thread_id)


def iter_agent_stream_events(
    runtime: AgentRuntime,
    user_query: str,
    thread_id: str = "default",
    *,
    clarification_enabled: bool = False,
    cancellation_event: threading.Event | None = None,
    trusted_history: TrustedHistory | None = None,
) -> Iterator[dict[str, Any]]:
    yield from runtime.iter_events(
        user_query,
        thread_id=thread_id,
        clarification_enabled=clarification_enabled,
        cancellation_event=cancellation_event,
        **({"trusted_history": trusted_history} if trusted_history is not None else {}),
    )


__all__ = [
    "AgentRuntime",
    "iter_agent_stream_events",
    "run_agent",
    "run_agent_streaming",
]
