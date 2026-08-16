"""检索/生成运行时子包（LangChain 1.x + Nianlun 知识库 Agent）。

本 ``__init__`` 刻意**不 eager 导入** runtime / knowledgebase，避免
``import nianlun.agent``（或经 ``nianlun.models.llm`` 间接导入）时加载工作区和整条运行时链。
需要时直接从子模块导入::

    from nianlun.agent.lead_agent.factory import AgentRuntimeFactory
    from nianlun.agent.lead_agent.runtime import run_agent
    from nianlun.knowledgebase import KnowledgeBase
    from nianlun.knowledgebase.config import WORKSPACE_DIR
    from nianlun.agent.lead_agent.prompt import SYSTEM_PROMPT

子模块：

- :mod:`nianlun.config`               模型默认值与环境变量
- :mod:`nianlun.knowledgebase`        KnowledgeBase：注册表加载 + 检索操作
- :mod:`nianlun.agent.lead_agent.routing` 轻量意图路由（寒暄/感谢等直接回复）
- :mod:`nianlun.agent.lead_agent.agent` 纯 Agent graph 组装
- :mod:`nianlun.agent.lead_agent.factory` Agent graph 与应用依赖 composition root
- :mod:`nianlun.agent.lead_agent.prompt` 系统提示词模板与按检索模式构建逻辑
- :mod:`nianlun.agent.lead_agent.runtime` AgentRuntime facade
- :mod:`nianlun.agent.lead_agent.runner` RetrievalCollector / 单轮执行 / 流式输出
- :mod:`nianlun.agent.tools` LangChain 模块级工具与隐藏 ToolRuntime
- :mod:`nianlun.agent.middleware` 工具错误、悬空调用、会话摘要和问题澄清 middleware
- :mod:`nianlun.agent.lead_agent` 主 Agent 的核心实现
- :mod:`nianlun.agent.batch`          批量测试：并发、重试、增量写出
- :mod:`nianlun.agent.cli`            命令行入口（交互 / 批量）

命令行::

    python -m nianlun.agent.cli                          # 交互模式（流式）
    python -m nianlun.agent.cli --no-stream              # 交互模式（非流式）
    python -m nianlun.agent.cli --batch-file train.json --workers 4 --quiet-tools
"""

from __future__ import annotations
