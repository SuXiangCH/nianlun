"""Nianlun Agent 工厂。

本模块只负责把已经准备好的模型、工具和 middleware 组装为 LangGraph Agent。
应用配置与基础设施构造由 :mod:`nianlun.agent.lead_agent.factory` 负责。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from langchain.agents import create_agent

from nianlun.agent.token_estimation import estimate_tokens


def estimate_agent_context_overhead(system_prompt: str, tools: list) -> int:
    """估算不会出现在 state messages 中的 system/tool schema token 开销。"""
    try:
        from langchain_core.messages import HumanMessage

        schema_messages = [HumanMessage(content=system_prompt)]
        for tool in tools:
            schema_messages.append(
                HumanMessage(
                    content=json.dumps(
                        {
                            "name": getattr(tool, "name", ""),
                            "description": getattr(tool, "description", ""),
                            "args": getattr(tool, "args", {}),
                        },
                        ensure_ascii=False,
                        default=str,
                    )
                )
            )
        # Provider-specific serialization is not visible to the middleware.
        return estimate_tokens(schema_messages) + 1_024
    except Exception:
        return 0


def build_agent(
    llm,
    *,
    tools: list,
    system_prompt: str,
    checkpointer=None,
    context_schema: type = dict,
    name: str = "pageindex-agent",
    middleware: Sequence[Any] | None = None,
):
    """组装 Agent，不绑定具体应用状态或知识库。"""
    return create_agent(
        model=llm,
        tools=tools,
        system_prompt=system_prompt,
        checkpointer=checkpointer,
        context_schema=context_schema,
        name=name,
        middleware=tuple(middleware or ()),
    )


__all__ = [
    "build_agent",
    "estimate_agent_context_overhead",
]
