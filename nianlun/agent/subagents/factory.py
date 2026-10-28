"""Optional LangGraph factory for the isolated deep-search Agent."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from langchain.agents import create_agent
from nianlun.agent.contracts import AnswerImagePolicy

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
    tool_schema_version: int = 4,
    image_capability: bool = False,
) -> Any:
    """Compile a one-shot child Agent with no checkpointer or recursive tool."""
    resolved_prompt = (
        build_deep_search_system_prompt() if system_prompt is None else system_prompt
    )
    return create_agent(
        model=llm,
        tools=build_deep_search_tools(
            include_vector=include_vector,
            tool_schema_version=tool_schema_version,
            image_capability=image_capability,
        ),
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
    tool_schema_version: int = 4,
    image_capability: bool = False,
    image_policy: AnswerImagePolicy | None = None,
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
            tool_schema_version=tool_schema_version,
            image_capability=image_capability,
        )

    return DeepSearchRunner(
        factory,
        config=config,
        system_prompt=runner_prompt,
        context_factory=context_factory,
        tool_schema_version=tool_schema_version,
        image_capability=image_capability,
        image_policy=image_policy,
    )


__all__ = ["build_deep_search_agent", "create_deep_search_runner"]
