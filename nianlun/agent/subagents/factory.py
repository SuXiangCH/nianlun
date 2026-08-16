"""Optional LangGraph factory for the isolated deep-search Agent."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from langchain.agents import create_agent

from nianlun.agent.subagents.config import DeepSearchConfig
from nianlun.agent.subagents.executor import DeepSearchRunner
from nianlun.agent.subagents.prompt import build_deep_search_system_prompt
from nianlun.agent.subagents.result import DeepSearchOutput
from nianlun.agent.subagents.tools import build_deep_search_tools


def build_deep_search_agent(
    llm: Any,
    *,
    include_vector: bool = False,
    system_prompt: str | None = None,
) -> Any:
    """Compile a one-shot child Agent with no checkpointer or recursive tool."""
    resolved_prompt = (
        build_deep_search_system_prompt()
        if system_prompt is None
        else system_prompt
    )
    return create_agent(
        model=llm,
        tools=build_deep_search_tools(include_vector=include_vector),
        system_prompt=resolved_prompt or None,
        response_format=DeepSearchOutput,
        checkpointer=None,
        name="nianlun-deep-search",
    )


def create_deep_search_runner(
    llm: Any,
    *,
    include_vector: bool = False,
    config: DeepSearchConfig | None = None,
    system_prompt: str | None = None,
    context_factory: Callable[[], Mapping[str, Any]] | None = None,
) -> DeepSearchRunner:
    """Create a runner whose child Agent is compiled only on first use."""
    runner_prompt = system_prompt or build_deep_search_system_prompt()

    def factory() -> Any:
        return build_deep_search_agent(
            llm,
            include_vector=include_vector,
            # The runner supplies the prompt as an input message so the
            # compiled graph remains reusable for every isolated task.
            system_prompt="",
        )

    return DeepSearchRunner(
        factory,
        config=config,
        system_prompt=runner_prompt,
        context_factory=context_factory,
    )


__all__ = ["build_deep_search_agent", "create_deep_search_runner"]
