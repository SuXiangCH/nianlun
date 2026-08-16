"""应用级 Agent runtime facade。

本模块只暴露一个已组装 Agent 的应用级运行对象；单次请求执行细节位于
:mod:`nianlun.agent.lead_agent.runner`，依赖组装位于
:mod:`nianlun.agent.lead_agent.factory`。
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from nianlun.agent.contracts import AgentRequestContext, KnowledgeBasePort
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

    def __init__(
        self,
        *,
        agent: Any,
        model: str,
        effective_url: str,
        tool_logging: bool,
        kb: KnowledgeBasePort,
    ) -> None:
        self.runner = AgentRunner(
            agent=agent,
            context_factory=AgentRequestContextFactory(
                knowledge_base=kb,
                tool_logging=tool_logging,
            ),
        )
        self.model = model
        self.effective_url = effective_url

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
    ) -> dict[str, Any]:
        return self.runner.invoke(
            user_query,
            thread_id=thread_id,
            clarification_enabled=clarification_enabled,
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
    ) -> Iterator[dict[str, Any]]:
        return self.runner.iter_events(
            user_query,
            thread_id=thread_id,
            clarification_enabled=clarification_enabled,
        )


def run_agent(
    runtime: AgentRuntime,
    user_query: str,
    thread_id: str = "default",
    *,
    clarification_enabled: bool = False,
) -> dict[str, Any]:
    return runtime.invoke(
        user_query,
        thread_id=thread_id,
        clarification_enabled=clarification_enabled,
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
) -> Iterator[dict[str, Any]]:
    yield from runtime.iter_events(
        user_query,
        thread_id=thread_id,
        clarification_enabled=clarification_enabled,
    )


__all__ = [
    "AgentRuntime",
    "iter_agent_stream_events",
    "run_agent",
    "run_agent_streaming",
]
