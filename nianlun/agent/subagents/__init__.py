"""Standalone deep-search subagent components.

This package is intentionally not imported by the main Agent flow yet. It can
be integrated later through the request-level ``agent_mode`` gate.
"""

from nianlun.agent.subagents.config import DeepSearchConfig
from nianlun.agent.subagents.executor import (
    DeepSearchExecution,
    DeepSearchRunner,
)
from nianlun.agent.subagents.factory import (
    build_deep_search_agent,
    create_deep_search_runner,
)
from nianlun.agent.subagents.result import (
    DeepSearchOutput,
    DeepSearchResult,
    Evidence,
    EvidenceOutput,
    bound_result,
    result_from_agent_output,
)
from nianlun.agent.subagents.tools import (
    DEEP_SEARCH_TOOL_NAMES,
    build_deep_search_tools,
)

__all__ = [
    "DEEP_SEARCH_TOOL_NAMES",
    "DeepSearchConfig",
    "DeepSearchExecution",
    "DeepSearchOutput",
    "DeepSearchResult",
    "DeepSearchRunner",
    "Evidence",
    "EvidenceOutput",
    "bound_result",
    "build_deep_search_agent",
    "build_deep_search_tools",
    "create_deep_search_runner",
    "result_from_agent_output",
]
