"""Nianlun 的主文档问答 Agent。

核心 Agent 的组装、提示词和运行时集中在这个子包中；共享的 middleware
与工具位于 :mod:`nianlun.agent.middleware` 和 :mod:`nianlun.agent.tools`。
交互式 CLI 与批量评测入口保留在 :mod:`nianlun.agent` 包的上一层。
"""

from __future__ import annotations
