"""模型可调用的问题澄清工具声明。

这个工具只负责向模型暴露稳定的参数 schema。真正的等待和控制流由
``ClarificationMiddleware`` 接管；工具函数体不会在已接入该 middleware 的
Agent 中执行。
"""

from __future__ import annotations

from typing import Literal

from langchain.tools import tool


ClarificationType = Literal[
    "missing_info",
    "ambiguous_requirement",
    "approach_choice",
    "risk_confirmation",
    "suggestion",
]


@tool("ask_clarification", return_direct=True, parse_docstring=True)
def ask_clarification_tool(
    question: str,
    clarification_type: ClarificationType,
    context: str | None = None,
    options: list[str] | None = None,
) -> str:
    """在无法可靠继续时向用户提出一个澄清问题。

    该工具只用于缺少决定性信息、需求存在多种解释、需要用户选择方案，
    或执行高风险操作前请求确认。知识库已由应用绑定，不能用此工具询问
    用户选择知识库。

    Args:
        question: 必须具体、可直接回答的问题。一次只提出一个问题。
        clarification_type: 澄清类型：missing_info、ambiguous_requirement、
            approach_choice、risk_confirmation 或 suggestion。
        context: 可选的背景信息，帮助用户理解为什么需要澄清。
        options: 可选的有限选项列表，适用于方案选择或确认场景。
    """
    # The middleware intercepts this call before the function body is reached.
    return "Clarification request is handled by the clarification middleware."


__all__ = ["ClarificationType", "ask_clarification_tool"]
